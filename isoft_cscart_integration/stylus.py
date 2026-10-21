# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Stylus stock: hourly snapshot of the Stylus catalogue + retention.

Stock also flows into the CS-Cart sync through the normal External Stock Sources
machinery (a source with Source Type = Stylus); this module only keeps the
history so it can be viewed and trended."""

import frappe
from frappe.utils import flt, getdate, now_datetime

from isoft_cscart_integration import external


def _stylus_cfg():
	"""First enabled Stylus external source as a normalised cfg, or None."""
	s = frappe.get_single("CSCart Integration Settings")
	for src in (s.external_sources or []):
		if src.enabled and (src.source_type or "ERPNext") == "Stylus":
			return external._source_config(src)
	return None


def snapshot_stylus():
	"""Hourly job: store a Stylus catalogue snapshot, then keep every snapshot
	from today and collapse earlier days to the latest one each."""
	cfg = _stylus_cfg()
	if not cfg:
		return
	try:
		rows = external.fetch_stylus_rows(cfg)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "CSCart Stylus snapshot failed")
		return

	# Prices from the previous snapshot (before we insert the new one), so we can
	# log every code whose price changed.
	prev = latest_snapshot()
	prev_prices = {}
	if prev:
		for it in frappe.get_all("CSCart Stylus Snapshot Item", filters={"parent": prev["name"]},
			fields=["code", "price"], limit_page_length=0):
			prev_prices[it.code] = flt(it.price)

	frappe.get_doc({
		"doctype": "CSCart Stylus Snapshot",
		"taken_on": now_datetime(),
		"item_count": len(rows),
		"items": [{
			"code": r["code"], "designation": r["designation"], "brand": r["brand"],
			"family": r["family"], "price": r["price"], "stock": r["stock"], "is_active": r["is_active"],
		} for r in rows],
	}).insert(ignore_permissions=True)

	if prev_prices:
		_log_price_changes(rows, prev_prices)
	_collapse_old_days()
	frappe.db.commit()


def _log_price_changes(rows, prev_prices):
	"""Insert a CSCart Stylus Price Log row for every code whose price differs
	from the previous snapshot (only codes that existed before — a brand-new code
	has no 'old' price to compare)."""
	stamp = now_datetime()
	for r in rows:
		old = prev_prices.get(r["code"])
		if old is None:
			continue
		new = flt(r["price"])
		if round(new - old, 2) == 0:
			continue
		frappe.get_doc({
			"doctype": "CSCart Stylus Price Log",
			"code": r["code"], "designation": r["designation"], "brand": r["brand"],
			"changed_on": stamp,
			"old_price": old, "new_price": new,
			"delta": round(new - old, 2),
			"pct": round((new - old) / old * 100, 1) if old else None,
		}).insert(ignore_permissions=True)


def _collapse_old_days():
	today = str(getdate())
	snaps = frappe.get_all("CSCart Stylus Snapshot", fields=["name", "creation"], order_by="creation asc")
	latest_by_day = {}
	for s in snaps:
		day = str(s.creation)[:10]
		if day != today:
			latest_by_day[day] = s.name  # asc -> last seen is the latest
	for s in snaps:
		day = str(s.creation)[:10]
		if day != today and s.name != latest_by_day.get(day):
			frappe.delete_doc("CSCart Stylus Snapshot", s.name,
				ignore_permissions=True, force=True, delete_permanently=True)


def latest_snapshot():
	rows = frappe.get_all("CSCart Stylus Snapshot", fields=["name", "taken_on"],
		order_by="creation desc", limit=1)
	return rows[0] if rows else None
