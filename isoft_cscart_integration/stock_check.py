# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Read-only stock lookup behind the /stock-check page.

Search an item and see where its website stock comes from: local warehouses,
each enabled External Stock Source (Stylus, Unicom, ... — whatever is
configured), and the merged total the sync pushes to the website. Uses the
same functions as the sync, so the numbers match what CS-Cart receives."""

import frappe
from frappe import _
from frappe.utils import cint, flt

from isoft_cscart_integration import erp, external

SETTINGS_DT = "CSCart Integration Settings"
ALLOWED_ROLES = {"System Manager", "CSCart Manager", "Stock Viewer"}


def can_view():
	return bool(ALLOWED_ROLES & set(frappe.get_roles()))


def _guard():
	if not can_view():
		frappe.throw(_("You are not permitted to check stock."), frappe.PermissionError)


@frappe.whitelist()
def search(query=None, limit=50):
	"""Items matching `query` (code, name or barcode) with a per-source stock
	breakdown. Returns {rows, sources}."""
	_guard()
	query = (query or "").strip()
	if len(query) < 2:
		return {"rows": [], "sources": [], "hint": "Type at least 2 characters."}
	limit = min(cint(limit) or 50, 200)
	like = "%" + query + "%"

	codes = frappe.db.sql_list("""
		select distinct i.name
		from `tabItem` i
		left join `tabItem Barcode` b on b.parent = i.name
		where i.disabled = 0 and i.is_stock_item = 1 and i.has_variants = 0
		  and (i.name like %(q)s or i.item_name like %(q)s or b.barcode like %(q)s)
		order by (i.name = %(exact)s) desc, i.name asc
		limit %(lim)s
	""", {"q": like, "exact": query, "lim": limit})
	if not codes:
		return {"rows": [], "sources": []}

	items = frappe.get_all("Item", filters={"name": ["in", codes]},
		fields=["name", "item_name", "brand", "item_group", "stock_uom"], limit_page_length=0)
	meta = {i.name: {"brand": i.brand, "item_group": i.item_group} for i in items}

	s = frappe.get_single(SETTINGS_DT)
	local = erp.local_raw_quantities(s, codes)
	src_data = external.per_source_qty(s, meta)
	website = erp.erp_quantities(s, codes, items_meta=meta)

	# What the website currently shows (the mirror from the last map refresh).
	sku_field = s.sku_field or "item_code"
	sku_of = {c: c for c in codes}
	if sku_field != "item_code":
		for r in frappe.get_all("Item", filters={"name": ["in", codes]}, fields=["name", sku_field]):
			sku_of[r.name] = r.get(sku_field) or r.name
	norms = {erp.normalize_sku(v): c for c, v in sku_of.items() if v}
	site = {}
	for m in frappe.get_all("CSCart Product Map", filters={"sku_normalized": ["in", list(norms)]},
			fields=["sku_normalized", "cscart_status", "cscart_amount"], limit_page_length=0):
		code = norms.get(m.sku_normalized)
		if code:
			site[code] = m

	order = {c: i for i, c in enumerate(codes)}
	rows = []
	for it in sorted(items, key=lambda x: order.get(x.name, 0)):
		c = it.name
		on_site = site.get(c)
		rows.append({
			"item_code": c,
			"item_name": it.item_name,
			"brand": it.brand,
			"uom": it.stock_uom,
			"local": flt(local.get(c, 0)),
			"sources": [flt(d["qtys"].get(c, 0)) for d in src_data],
			"website": cint(website.get(c, 0)),
			"on_site": bool(on_site),
			"site_status": on_site.cscart_status if on_site else None,
			"site_qty": cint(on_site.cscart_amount) if on_site else None,
		})
	return {
		"rows": rows,
		# Only say a source is down — no remote URLs or tracebacks to read-only users.
		"sources": [{"name": d["name"], "error": bool(d.get("error"))} for d in src_data],
	}
