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
		"base_url": (get("base_url") or "").strip().rstrip("/"),
		"api_key": (get("api_key") or "").strip(),
		"api_secret": secret,
		"warehouses": [w.strip() for w in (get("warehouses") or "").replace(",", "\n").splitlines() if w.strip()],
		"merge_mode": get("merge_mode") or "Add on top",
		"verify_ssl": bool(get("verify_ssl")),
	}


def fetch_source_qty(cfg):
	"""Return {item_code: qty} summed from the remote site's Bin.

	`cfg` is a normalised dict from _source_config (base_url, api_key, api_secret
	plaintext, warehouses list, verify_ssl).
	"""
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


def _cache_key(name):
	return "cscart_extqty:" + (name or "")


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


def source_qty(src, use_last_known=False):
	"""Return (qtys, error). On success caches the result. On failure returns the
	last cached values when use_last_known, else an empty dict + the error string."""
	name = src.get("source_name")
	try:
		qtys = fetch_source_qty(_source_config(src))
		_cache_set(name, qtys)
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
	or None if the source has no item/brand/group scope. Brand and item-group
	scope need `items_meta` (item_code -> {brand, item_group}); item-code scope
	works without it."""
	items = set(_lines(src.get("scope_items")))
	brands = set(_lines(src.get("scope_brands")))
	groups = _lines(src.get("scope_item_groups"))
	if not (items or brands or groups):
		return None

	group_set = set()
	if groups and items_meta:
		from isoft_cscart_integration import erp
		group_set = set(erp.expand_item_groups(groups))

	def ok(code):
		if items and code not in items:
			return False
		meta = (items_meta or {}).get(code) or {}
		if brands and items_meta and meta.get("brand") not in brands:
			return False
		if groups and items_meta and meta.get("item_group") not in group_set:
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
