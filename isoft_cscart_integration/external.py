# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Pull stock from other ERPNext instances and merge it into the qty the sync
pushes to CS-Cart.

Each external source is another Frappe/ERPNext site. We read its Bin quantities
over the standard REST resource API using token auth (api_key:api_secret), sum
actual_qty per item_code, and merge per the source's Merge Mode.

A remote that is unreachable or misconfigured is skipped (logged) — it never
breaks the sync or silently zeroes out local stock.
"""

import json

import frappe
import requests
from frappe.utils import flt

PAGE = 2000
TIMEOUT = 30


def _source_config(src):
	"""Normalise a child-row Document (or dict) to a plain config with a
	decrypted secret."""
	if hasattr(src, "get_password"):
		secret = src.get_password("api_secret") if src.get("api_secret") else None
		get = src.get
	else:
		secret = src.get("api_secret")
		get = src.get
	return {
		"source_name": get("source_name"),
		"source_type": get("source_type") or "ERPNext",
		"base_url": (get("base_url") or "").strip().rstrip("/"),
		"api_key": (get("api_key") or "").strip(),
		"api_secret": secret,
		"warehouses": [w.strip() for w in (get("warehouses") or "").replace(",", "\n").splitlines() if w.strip()],
		"merge_mode": get("merge_mode") or "Add on top",
		"verify_ssl": bool(get("verify_ssl")),
	}


def fetch_stylus_rows(cfg):
	"""Return [{code, designation, brand, family, price, stock, is_active}] from
	the Stylus external API (ProductCode == ERP item_code)."""
	if not (cfg["base_url"] and cfg["api_key"] and cfg["api_secret"]):
		raise ValueError("Stylus source needs base URL, login and password.")
	resp = requests.get(
		cfg["base_url"] + "/api/StylusExternalClients/GetProductsInfo?code=",
		auth=(cfg["api_key"], cfg["api_secret"]),
		headers={"Accept": "application/json"},
		timeout=90, verify=cfg["verify_ssl"],
	)
	if resp.status_code != 200:
		raise ValueError("Stylus returned HTTP {0}: {1}".format(resp.status_code, (resp.text or "")[:160]))
	data = resp.json()
	if not isinstance(data, list):
		raise ValueError("Unexpected Stylus response (not a list).")
	out = []
	for r in data:
		code = (r.get("ProductCode") or "").strip()
		if not code:
			continue
		out.append({
			"code": code,
			"designation": r.get("ProductDesignation"),
			"brand": (r.get("Marca") or {}).get("Brand"),
			"family": (r.get("Familias") or {}).get("Family"),
			"price": flt(r.get("ProductPrice")),
			"stock": flt(r.get("QuantityAvailable")),
			"is_active": 1 if r.get("IsActive") else 0,
		})
	return out


def fetch_source_qty(cfg):
	"""Return {item_code: qty} for the source. ERPNext sums its Bin; Stylus
	reads QuantityAvailable per ProductCode.

	`cfg` is a normalised dict from _source_config.
	"""
	if cfg.get("source_type") == "Stylus":
		return {r["code"]: r["stock"] for r in fetch_stylus_rows(cfg)}

	if not (cfg["base_url"] and cfg["api_key"] and cfg["api_secret"]):
		raise ValueError("External source needs base URL, API key and secret.")

	session = requests.Session()
	session.headers.update({
		"Authorization": "token {0}:{1}".format(cfg["api_key"], cfg["api_secret"]),
		"Accept": "application/json",
	})

	filters = None
	if cfg["warehouses"]:
		filters = json.dumps([["warehouse", "in", cfg["warehouses"]]])

	out = {}
	start = 0
	url = cfg["base_url"] + "/api/resource/Bin"
	while True:
		params = {
			"fields": json.dumps(["item_code", "actual_qty"]),
			"limit_start": start,
			"limit_page_length": PAGE,
		}
		if filters:
			params["filters"] = filters
		resp = session.get(url, params=params, timeout=TIMEOUT, verify=cfg["verify_ssl"])
		if resp.status_code != 200:
			raise ValueError("Remote {0} returned HTTP {1}: {2}".format(
				cfg["base_url"], resp.status_code, (resp.text or "")[:160]))
		try:
			rows = (resp.json() or {}).get("data") or []
		except ValueError:
			raise ValueError("Remote {0} returned a non-JSON body.".format(cfg["base_url"]))
		if not rows:
			break
		for r in rows:
			code = r.get("item_code")
			if code:
				out[code] = flt(out.get(code, 0)) + flt(r.get("actual_qty"))
		if len(rows) < PAGE:
			break
		start += PAGE
	return out


def enabled_sources(settings):
	return [s for s in (settings.get("external_sources") or []) if s.get("enabled")]


CACHE_TTL = 7 * 24 * 3600
FRESH_TTL = 300  # serve a recently fetched result without hitting the remote again


def _cache_key(name):
	return "cscart_extqty:" + (name or "")


def _fresh_key(name):
	return "cscart_extqty_fresh:" + (name or "")


def _cache_set(name, qtys):
	try:
		frappe.cache().set_value(_cache_key(name), qtys, expires_in_sec=CACHE_TTL)
	except Exception:
		pass


def _cache_get(name):
	try:
		return frappe.cache().get_value(_cache_key(name))
	except Exception:
		return None


def _fresh_set(name, qtys):
	try:
		frappe.cache().set_value(_fresh_key(name), qtys, expires_in_sec=FRESH_TTL)
	except Exception:
		pass


def _fresh_get(name):
	try:
		# expires=True so an expired key is treated as a miss (not memoised stale).
		return frappe.cache().get_value(_fresh_key(name), expires=True)
	except Exception:
		return None


def source_qty(src, use_last_known=False):
	"""Return (qtys, error). A result fetched within FRESH_TTL seconds is reused
	without hitting the remote (so repeated searches/refreshes and the two reads
	within one sync are cheap). On success caches the result; on failure returns
	the last cached values when use_last_known, else an empty dict + the error."""
	name = src.get("source_name")
	fresh = _fresh_get(name)
	if fresh is not None:
		return fresh, None
	try:
		qtys = fetch_source_qty(_source_config(src))
		_cache_set(name, qtys)
		_fresh_set(name, qtys)
		return qtys, None
	except Exception as e:
		frappe.log_error(
			"External stock source '{0}' failed: {1}".format(name, e),
			"CSCart external stock",
		)
		if use_last_known:
			cached = _cache_get(name)
			if cached is not None:
				return cached, None
		return {}, str(e)


def _lines(value):
	return [v.strip() for v in (value or "").replace(",", "\n").splitlines() if v.strip()]


def source_scope(src, items_meta=None):
	"""Return a predicate(item_code)->bool limiting where the source applies,
	or None if the source has no include/exclude filters. "Only" lists restrict
	(AND across dimensions); "Except" lists remove (any match drops the item).
	Brand/group filters need `items_meta` (item_code -> {brand, item_group})."""
	items = set(_lines(src.get("scope_items")))
	brands = set(_lines(src.get("scope_brands")))
	groups = _lines(src.get("scope_item_groups"))
	x_items = set(_lines(src.get("exclude_items")))
	x_brands = set(_lines(src.get("exclude_brands")))
	x_groups = _lines(src.get("exclude_item_groups"))
	if not (items or brands or groups or x_items or x_brands or x_groups):
		return None

	expand = None
	if (groups or x_groups) and items_meta:
		from isoft_cscart_integration import erp
		expand = erp.expand_item_groups
	group_set = set(expand(groups)) if (groups and expand) else set()
	x_group_set = set(expand(x_groups)) if (x_groups and expand) else set()

	def ok(code):
		meta = (items_meta or {}).get(code) or {}
		brand, grp = meta.get("brand"), meta.get("item_group")
		# include ("Only ...") — AND across whatever is set
		if items and code not in items:
			return False
		if brands and items_meta and brand not in brands:
			return False
		if groups and items_meta and grp not in group_set:
			return False
		# exclude ("Except ...") — any match removes
		if code in x_items:
			return False
		if x_brands and items_meta and brand in x_brands:
			return False
		if x_groups and items_meta and grp in x_group_set:
			return False
		return True

	return ok


def per_source_qty(settings, items_meta=None):
	"""Return [{name, qtys, error}] for each enabled source (for display),
	with each source's qty limited to its scope."""
	use_last = (settings.get("external_fail_mode") == "Use last known values")
	out = []
	for src in enabled_sources(settings):
		qtys, err = source_qty(src, use_last)
		scope = source_scope(src, items_meta)
		if scope:
			qtys = {c: q for c, q in qtys.items() if scope(c)}
		out.append({"name": src.get("source_name") or "External", "qtys": qtys, "error": err})
	return out


def merge_external(settings, base, failures=None, items_meta=None):
	"""Merge every enabled external source into `base` (item_code -> raw qty).

	Each source only contributes for items within its scope (item / brand /
	item group). A source that fails is recorded in `failures` and dropped;
	with fail mode "Use last known values" it falls back to cached numbers.
	"""
	sources = enabled_sources(settings)
	if not sources:
		return base

	use_last = (settings.get("external_fail_mode") == "Use last known values")
	merged = dict(base)
	for src in sources:
		ext, err = source_qty(src, use_last)
		if err:
			if failures is not None:
				failures.append(src.get("source_name") or "External")
			continue
		scope = source_scope(src, items_meta)
		mode = src.get("merge_mode") or "Add on top"
		for code, qty in ext.items():
			if scope and not scope(code):
				continue
			if mode.startswith("Add"):
				merged[code] = flt(merged.get(code, 0)) + flt(qty)
			elif mode.startswith("Fill"):
				if flt(merged.get(code, 0)) == 0:
					merged[code] = flt(qty)
			elif mode.startswith("Replace"):
				merged[code] = flt(qty)
	return merged
