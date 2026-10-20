# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Stylus stock: hourly snapshot of the Stylus catalogue + retention.

Stock also flows into the CS-Cart sync through the normal External Stock Sources
machinery (a source with Source Type = Stylus); this module only keeps the
history so it can be viewed and trended."""

import frappe
from frappe.utils import getdate, now_datetime

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

	frappe.get_doc({
		"doctype": "CSCart Stylus Snapshot",
		"taken_on": now_datetime(),
		"item_count": len(rows),
		"items": [{
			"code": r["code"], "designation": r["designation"], "brand": r["brand"],
			"family": r["family"], "price": r["price"], "stock": r["stock"], "is_active": r["is_active"],
		} for r in rows],
	}).insert(ignore_permissions=True)

	_collapse_old_days()
	frappe.db.commit()


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
