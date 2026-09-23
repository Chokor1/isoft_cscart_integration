# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Sync engine: scheduling, product-map refresh, planning and execution.

Design notes
------------
* One way only: ERPNext -> CS-Cart. The store is never read as a source of truth
  except during refresh_product_map (to learn the last known site values).
* Partial PUTs: only the fields the app owns (amount / price / status) are ever
  sent, combined into one call per product.
* Everything is recomputed each run and compared with the map, because v13 Bin
  updates do not reliably bump `modified`, so timestamp-watching is unsafe.
"""

import json
import re
import time
from collections import defaultdict
from html import unescape

import frappe
from frappe.utils import cint, flt, now_datetime

from isoft_cscart_integration import api, erp


def clean_name(value, limit=120):
	"""CS-Cart names may contain HTML (e.g. <bdi style=...>). Strip tags and
	unescape entities so Frappe's own HTML-escaping on save can't overflow the
	Data column, then truncate with headroom."""
	s = re.sub(r"<[^>]+>", " ", value or "")
	s = unescape(s)
	s = re.sub(r"\s+", " ", s).strip()
	return s[:limit]

LOCK_KEY = "cscart_sync_running"
LOCK_TTL = 3600
CHANGES_CAP = 2000
COMMIT_EVERY = 100
MAX_CONSECUTIVE_FAILURES = 10

FREQ_MINUTES = {
	"Every 5 Minutes": 5,
	"Every 15 Minutes": 15,
	"Every 30 Minutes": 30,
	"Hourly": 60,
	"Every 6 Hours": 360,
	"Daily": 1440,
}


# ===========================================================================
# Locking
# ===========================================================================

def _acquire_lock():
	cache = frappe.cache()
	return bool(cache.set(cache.make_key(LOCK_KEY), 1, nx=True, ex=LOCK_TTL))


def _release_lock():
	cache = frappe.cache()
	cache.delete(cache.make_key(LOCK_KEY))


def is_running():
	cache = frappe.cache()
	return bool(cache.get(cache.make_key(LOCK_KEY)))


# ===========================================================================
# Scheduling
# ===========================================================================

def scheduled_sync():
	"""Runs every 5 minutes from hooks. When a run is due (per Sync Frequency),
	enqueue it. With "Refresh map on every run" on, each run is a Full cycle —
	re-read the store, pull external sources, diff, and push — which is the
	recommended self-correcting setup. Otherwise it's a light incremental that
	trusts the mirror."""
	settings = frappe.get_single("CSCart Integration Settings")
	if not settings.enabled:
		return
	if is_running():
		return

	now = now_datetime()
	freq = FREQ_MINUTES.get(settings.sync_frequency, 60)
	last = settings.last_attempt_on
	due = True
	if last:
		elapsed = (now - frappe.utils.get_datetime(last)).total_seconds() / 60.0
		due = elapsed >= (freq - 1)
	if due:
		_enqueue("Full" if settings.refresh_map_each_run else "Incremental")


def _enqueue(sync_type):
	frappe.enqueue(
		"isoft_cscart_integration.sync.run_sync",
		queue="long",
		timeout=LOCK_TTL,
		job_name="cscart_sync_{0}".format(sync_type.lower()),
		sync_type=sync_type,
	)


# ===========================================================================
# Product map refresh
# ===========================================================================

def refresh_product_map(log=None):
	"""Page through every product and upsert the map by product_id."""
	settings = frappe.get_single("CSCart Integration Settings")
	client = api.get_client(settings)

	existing = {
		r.name: r
		for r in frappe.get_all(
			"CSCart Product Map",
			fields=["name", "product_id", "sku_normalized", "item_code"],
			limit_page_length=0,
		)
	}
	seen = set()
	duplicates = defaultdict(list)

	for p in client.iter_products():
		pid = str(p.get("product_id"))
		if not pid:
			continue
		seen.add(pid)
		sku = p.get("product_code")
		sku_norm = erp.normalize_sku(sku)
		if sku_norm:
			duplicates[sku_norm].append(pid)

		values = {
			"sku": sku,
			"sku_normalized": sku_norm,
			"product_name": clean_name(p.get("product")),
			"cscart_status": p.get("status"),
			"cscart_amount": cint(p.get("amount")),
			"cscart_price": flt(p.get("price")),
		}
		if pid in existing:
			frappe.db.set_value("CSCart Product Map", pid, values, update_modified=False)
		else:
			doc = frappe.get_doc({"doctype": "CSCart Product Map", "product_id": pid, **values})
			doc.insert(ignore_permissions=True)

	# Delete map rows for products that no longer exist on the site.
	stale = [name for name in existing if name not in seen]
	for name in stale:
		frappe.delete_doc("CSCart Product Map", name, ignore_permissions=True, force=True)

	frappe.db.commit()

	# Link item_code by normalized SKU.
	link_map_to_items(settings)

	dup_skus = {k: v for k, v in duplicates.items() if len(v) > 1}
	if dup_skus and log:
		log.setdefault("_notes", []).append(
			"Duplicate SKUs on site: {0}".format(", ".join(list(dup_skus)[:20]))
		)
	return {"total": len(seen), "stale_removed": len(stale), "duplicate_skus": len(dup_skus)}


def link_map_to_items(settings=None):
	"""Set item_code on each map row whose normalized SKU matches an Item."""
	settings = settings or frappe.get_single("CSCart Integration Settings")
	items = erp.eligible_items(settings)
	norm_to_item = {}
	for code, meta in items.items():
		if meta["sku_normalized"]:
			norm_to_item[meta["sku_normalized"]] = code

	rows = frappe.get_all(
		"CSCart Product Map",
		fields=["name", "sku_normalized", "item_code"],
		limit_page_length=0,
	)
	linked = 0
	for r in rows:
		want = norm_to_item.get(r.sku_normalized)
		if want != r.item_code:
			frappe.db.set_value(
				"CSCart Product Map", r.name, "item_code", want, update_modified=False
			)
			linked += 1
	frappe.db.commit()
	return linked


# ===========================================================================
# Price helper
# ===========================================================================

def compute_site_price(settings, erp_rate):
	p = flt(erp_rate)
	if settings.erp_price_includes_vat:
		divisor = 1 + (flt(settings.vat_rate) / 100.0)
		if divisor:
			p = p / divisor
	return flt(p, cint(settings.price_rounding))


# ===========================================================================
# Planning
# ===========================================================================

def build_plan(settings):
	"""Return (plan, stats). plan is a list of actions:
	{action: update|create, product_id, item_code, sku, payload, kind:[...]}"""
	items = erp.eligible_items(settings)
	ext_failures = []
	qtys = erp.erp_quantities(settings, list(items.keys()), failures=ext_failures, items_meta=items) if settings.sync_stock or settings.create_products else {}
	prices = erp.erp_prices(settings, items) if settings.sync_price or settings.create_products else {}

	norm_to_item = {}
	for code, meta in items.items():
		if meta["sku_normalized"]:
			norm_to_item[meta["sku_normalized"]] = code

	map_rows = frappe.get_all(
		"CSCart Product Map",
		fields=["product_id", "sku_normalized", "item_code", "cscart_status", "cscart_amount", "cscart_price"],
		limit_page_length=0,
	)

	plan = []
	stats = {
		"stock_changes": 0,
		"price_changes": 0,
		"price_held": 0,
		"disabled": 0,
		"created": 0,
		"zero_outs": 0,
		"matched": 0,
	}
	mapped_norms = set()

	for row in map_rows:
		item_code = row.item_code or norm_to_item.get(row.sku_normalized)
		if not item_code or item_code not in items:
			continue
		if row.sku_normalized:
			mapped_norms.add(row.sku_normalized)
		meta = items[item_code]
		stats["matched"] += 1
		site_disabled = (row.cscart_status == "D")
		payload = {}
		kinds = []

		# --- stock ---
		if settings.sync_stock and not (site_disabled and not settings.update_disabled_products):
			erp_qty = cint(qtys.get(item_code, 0))
			if erp_qty != cint(row.cscart_amount):
				payload["amount"] = erp_qty
				kinds.append("stock")
				stats["stock_changes"] += 1
				if cint(row.cscart_amount) > 0 and erp_qty == 0:
					stats["zero_outs"] += 1

		# --- price ---
		if settings.sync_price and not (site_disabled and not settings.update_disabled_products):
			erp_rate = prices.get(item_code)
			has_price = erp_rate not in (None, 0, 0.0)
			if not (settings.never_send_zero_price and not has_price):
				if erp_rate is not None:
					p = compute_site_price(settings, erp_rate)
					if abs(p - flt(row.cscart_price)) >= 0.01:
						if _price_within_limit(settings, row, p):
							payload["price"] = p
							kinds.append("price")
							stats["price_changes"] += 1
						else:
							# change too large — skip it, don't write
							stats["price_held"] += 1

		# --- disable ---
		if settings.sync_disable and meta["disabled"] and row.cscart_status == "A":
			payload["status"] = "D"
			kinds.append("disable")
			stats["disabled"] += 1

		if payload:
			plan.append({
				"action": "update",
				"product_id": row.product_id,
				"item_code": item_code,
				"sku": meta["sku"],
				"payload": payload,
				"kind": kinds,
			})

	# --- create ---
	if settings.create_products:
		for code, meta in items.items():
			if meta["sku_normalized"] in mapped_norms:
				continue
			erp_rate = prices.get(code)
			if erp_rate in (None, 0, 0.0):
				continue
			p = compute_site_price(settings, erp_rate)
			amount = cint(qtys.get(code, 0))
			create_payload = {
				"product": (meta.get(settings.new_product_name_field) or meta["item_name"] or code),
				"product_code": meta["sku"],
				"price": p,
				"amount": amount,
				"status": "D",
				"company_id": 1,
				"tax_ids": [cint(settings.new_product_tax_id or 8)],
			}
			if settings.new_product_category_id:
				cat = cint(settings.new_product_category_id)
				create_payload["category_ids"] = [cat]
				create_payload["main_category"] = cat
			plan.append({
				"action": "create",
				"product_id": None,
				"item_code": code,
				"sku": meta["sku"],
				"payload": create_payload,
				"kind": ["create"],
			})
			stats["created"] += 1

	stats["unmatched_erp_items"] = len(items) - len(mapped_norms)
	stats["external_failures"] = ext_failures
	return plan, stats


def _price_within_limit(settings, row, new_price):
	"""True if the price change is within max_price_change_pct (else it is skipped)."""
	old = flt(row.cscart_price)
	if old <= 0:
		return True
	pct = abs((new_price - old) / old) * 100.0
	return pct <= flt(settings.max_price_change_pct)


# ===========================================================================
# Run
# ===========================================================================

def run_sync(sync_type="Incremental"):
	if not _acquire_lock():
		return {"skipped": "another run is in progress"}

	settings = frappe.get_single("CSCart Integration Settings")
	start = time.monotonic()
	log = frappe.get_doc({
		"doctype": "CSCart Sync Log",
		"sync_type": sync_type,
		"status": "Running",
		"started_on": now_datetime(),
	}).insert(ignore_permissions=True)
	frappe.db.commit()

	# stamp attempt timestamps
	stamp = {"last_attempt_on": now_datetime()}
	if sync_type == "Full":
		stamp["last_full_attempt_on"] = now_datetime()
	frappe.db.set_value("CSCart Integration Settings", None, stamp, update_modified=False)
	frappe.db.commit()

	notes = {}
	try:
		if sync_type == "Full" or not frappe.db.count("CSCart Product Map"):
			refresh_product_map(notes)

		plan, stats = build_plan(settings)

		# --- external source failure policy ---
		fails = stats.get("external_failures") or []
		if fails and (settings.external_fail_mode or "Skip the run") == "Skip the run":
			_finish(log, "Aborted", stats, start, plan_only=True,
				error="External source(s) unreachable: {0}. Run skipped (fail mode = Skip the run); nothing sent.".format(", ".join(fails)))
			return {"status": "Aborted", "log": log.name, "external_failures": fails}

		# --- guards ---
		if stats["zero_outs"] > cint(settings.max_zero_outs):
			_finish(log, "Aborted", stats, start, plan_only=True,
				error="Zero-outs {0} exceed max_zero_outs {1}. Nothing sent.".format(
					stats["zero_outs"], settings.max_zero_outs))
			return {"status": "Aborted", "log": log.name}

		if len(plan) > cint(settings.max_updates_per_run):
			_finish(log, "Aborted", stats, start, plan_only=True,
				error="Planned changes {0} exceed max_updates_per_run {1}. Nothing sent.".format(
					len(plan), settings.max_updates_per_run))
			return {"status": "Aborted", "log": log.name}

		# --- dry run ---
		if settings.dry_run:
			_finish(log, "Dry Run", stats, start, plan=plan)
			_advance_success_stamp(settings, sync_type)
			return {"status": "Dry Run", "log": log.name, "planned": len(plan)}

		# --- execute ---
		result = _execute(settings, plan, stats)
		final = "Failed" if result["failed"] and not result["ok"] else ("Partial" if result["failed"] else "Success")
		_finish(log, final, stats, start, plan=plan, error=result.get("error"))
		if final in ("Success", "Partial"):
			_advance_success_stamp(settings, sync_type)
		return {"status": final, "log": log.name, "sent": result["ok"], "failed": result["failed"]}

	except Exception as e:
		frappe.db.rollback()
		msg = frappe.get_traceback()
		frappe.log_error(msg, "CSCart Sync failed")
		try:
			log.reload()
			log.status = "Failed"
			log.finished_on = now_datetime()
			log.errors = msg[:5000]
			log.save(ignore_permissions=True)
			frappe.db.commit()
			_alert(log, "Failed")
		except Exception:
			pass
		return {"status": "Failed", "error": str(e)}
	finally:
		_release_lock()


def _execute(settings, plan, stats):
	client = api.get_client(settings)
	ok = 0
	failed = 0
	consecutive = 0
	errors = []

	for i, action in enumerate(plan):
		try:
			if action["action"] == "create":
				resp = client.create_product(action["payload"])
				new_pid = str((resp or {}).get("product_id") or "")
				if new_pid:
					_upsert_map_after_create(new_pid, action)
			else:
				client.update_product(action["product_id"], action["payload"])
				_update_map_after_send(action)
			ok += 1
			consecutive = 0
		except Exception as e:
			failed += 1
			consecutive += 1
			errmsg = "{0} ({1}): {2}".format(action["item_code"], action.get("product_id"), e)
			errors.append(errmsg)
			if action.get("product_id"):
				frappe.db.set_value("CSCart Product Map", action["product_id"], "last_error",
					str(e)[:500], update_modified=False)
			if consecutive >= MAX_CONSECUTIVE_FAILURES:
				errors.append("Circuit breaker: stopped after {0} consecutive failures.".format(consecutive))
				break

		if (i + 1) % COMMIT_EVERY == 0:
			frappe.db.commit()

	frappe.db.commit()
	return {"ok": ok, "failed": failed, "error": "\n".join(errors[:200]) if errors else None}


def _update_map_after_send(action):
	values = {"last_synced_on": now_datetime(), "last_error": None}
	pl = action["payload"]
	if "amount" in pl:
		values["cscart_amount"] = pl["amount"]
	if "price" in pl:
		values["cscart_price"] = pl["price"]
	if "status" in pl:
		values["cscart_status"] = pl["status"]
	frappe.db.set_value("CSCart Product Map", action["product_id"], values, update_modified=False)


def _upsert_map_after_create(product_id, action):
	pl = action["payload"]
	if frappe.db.exists("CSCart Product Map", product_id):
		return
	frappe.get_doc({
		"doctype": "CSCart Product Map",
		"product_id": product_id,
		"sku": pl.get("product_code"),
		"sku_normalized": erp.normalize_sku(pl.get("product_code")),
		"product_name": clean_name(pl.get("product")),
		"item_code": action["item_code"],
		"cscart_status": pl.get("status"),
		"cscart_amount": pl.get("amount"),
		"cscart_price": pl.get("price"),
		"last_synced_on": now_datetime(),
		"created_by_sync": 1,
	}).insert(ignore_permissions=True)


def _finish(log, status, stats, start, plan=None, plan_only=False, error=None):
	log.reload()
	log.status = status
	log.finished_on = now_datetime()
	log.duration_sec = round(time.monotonic() - start, 1)
	log.stock_changes = stats.get("stock_changes", 0)
	log.price_changes = stats.get("price_changes", 0)
	log.price_held = stats.get("price_held", 0)
	log.disabled = stats.get("disabled", 0)
	log.created = stats.get("created", 0)
	log.unmatched_erp_items = stats.get("unmatched_erp_items", 0)
	if plan is not None:
		log.planned_changes = len(plan)
		log.changes = json.dumps(_summarize_plan(plan), indent=2, default=str)
	if error:
		log.errors = error[:5000]
	log.save(ignore_permissions=True)
	frappe.db.commit()
	if status in ("Failed", "Aborted"):
		_alert(log, status)


def _alert(log, status):
	"""Email the configured recipient when a run fails or aborts."""
	email = (frappe.db.get_single_value("CSCart Integration Settings", "alert_email") or "").strip()
	if not email:
		return
	try:
		frappe.sendmail(
			recipients=[email],
			subject="[CS-Cart Sync] {0} — {1}".format(status, log.name),
			message=(
				"CS-Cart sync run <b>{0}</b> finished with status <b>{1}</b>.<br><br>"
				"Errors:<br><pre>{2}</pre>"
			).format(log.name, status, frappe.utils.escape_html((log.errors or "")[:2000])),
			now=True,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "CSCart alert email failed")


def _summarize_plan(plan):
	return [
		{
			"action": a["action"],
			"product_id": a.get("product_id"),
			"item_code": a["item_code"],
			"sku": a.get("sku"),
			"kind": a.get("kind"),
			"payload": a["payload"],
		}
		for a in plan[:CHANGES_CAP]
	]


def _advance_success_stamp(settings, sync_type):
	values = {"last_sync_on": now_datetime(), "last_status": "OK"}
	if sync_type == "Full":
		values["last_full_sync_on"] = now_datetime()
	frappe.db.set_value("CSCart Integration Settings", None, values, update_modified=False)
	frappe.db.commit()


# ===========================================================================
# Housekeeping
# ===========================================================================

def run_map_refresh():
	"""Standalone product-map refresh, wrapped in its own log + lock."""
	if not _acquire_lock():
		return {"skipped": "another run is in progress"}
	start = time.monotonic()
	log = frappe.get_doc({
		"doctype": "CSCart Sync Log",
		"sync_type": "Map Refresh",
		"status": "Running",
		"started_on": now_datetime(),
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	try:
		notes = {}
		result = refresh_product_map(notes)
		log.reload()
		log.status = "Success"
		log.finished_on = now_datetime()
		log.duration_sec = round(time.monotonic() - start, 1)
		log.changes = json.dumps(result, indent=2)
		log.save(ignore_permissions=True)
		frappe.db.commit()
		return {"status": "Success", **result}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "CSCart Map Refresh failed")
		log.reload()
		log.status = "Failed"
		log.finished_on = now_datetime()
		log.errors = frappe.get_traceback()[:5000]
		log.save(ignore_permissions=True)
		frappe.db.commit()
		return {"status": "Failed", "error": str(e)}
	finally:
		_release_lock()


def cleanup_logs():
	cutoff = frappe.utils.add_days(frappe.utils.nowdate(), -30)
	old = frappe.get_all("CSCart Sync Log", filters={"creation": ["<", cutoff]}, pluck="name")
	for name in old:
		frappe.delete_doc("CSCart Sync Log", name, ignore_permissions=True, force=True, delete_permanently=True)
	frappe.db.commit()
	return len(old)
