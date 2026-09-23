# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Whitelisted API behind the ISOFT CS-Cart management page.

Everything is System Manager only; sync jobs are enqueued, never run inline
(except Test Connection and a bounded Preview)."""

import frappe
from frappe import _
from frappe.utils import cint, flt

from isoft_cscart_integration import api, erp, external, sync

SETTINGS_DT = "CSCart Integration Settings"

# Fields the page is allowed to write back to the Single.
WRITABLE_FIELDS = [
	"enabled", "dry_run",
	"store_url", "api_user", "verify_ssl", "request_timeout",
	"sync_frequency", "full_sync_daily", "full_sync_hour", "refresh_map_each_run", "alert_email", "external_fail_mode",
	"sync_stock", "sync_price", "sync_disable", "create_products",
	"qty_basis", "safety_stock",
	"price_list", "erp_price_includes_vat", "vat_rate", "price_rounding",
	"max_price_change_pct", "never_send_zero_price",
	"new_product_category_id", "new_product_tax_id", "new_product_name_field",
	"sku_field", "update_disabled_products", "max_zero_outs", "max_updates_per_run",
	"item_filter_type", "brand_filter_type", "item_group_filter_type", "skip_disabled_items",
]
CHILD_FIELDS = {
	"warehouses": ("CSCart Sync Warehouse", "warehouse"),
	"items": ("CSCart Sync Item", "item_code"),
	"brands": ("CSCart Sync Brand", "brand"),
	"item_groups": ("CSCart Sync Item Group", "item_group"),
}


def _guard():
	frappe.only_for("System Manager")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_state():
	_guard()
	s = frappe.get_single(SETTINGS_DT)
	settings = {f: s.get(f) for f in WRITABLE_FIELDS}
	settings["api_key_set"] = bool(s.api_key)
	for field, (_dt, link) in CHILD_FIELDS.items():
		settings[field] = [row.get(link) for row in (s.get(field) or [])]

	settings["external_sources"] = [{
		"source_name": r.source_name, "enabled": r.enabled, "base_url": r.base_url,
		"api_key": r.api_key, "warehouses": r.warehouses, "merge_mode": r.merge_mode,
		"verify_ssl": r.verify_ssl, "secret_set": bool(r.api_secret),
		"scope_items": r.scope_items, "scope_brands": r.scope_brands,
		"scope_item_groups": r.scope_item_groups,
	} for r in (s.external_sources or [])]

	settings["last_sync_on"] = s.last_sync_on
	settings["last_full_sync_on"] = s.last_full_sync_on
	settings["last_status"] = s.last_status

	return {
		"settings": settings,
		"dashboard": _dashboard(s),
		"running": sync.is_running(),
		"logs": _recent_logs(),
	}


def _dashboard(s):
	total_map = frappe.db.count("CSCart Product Map")
	matched = frappe.db.count("CSCart Product Map", {"item_code": ["!=", ""]})
	created = frappe.db.count("CSCart Product Map", {"created_by_sync": 1})
	return {
		"total_map": total_map,
		"matched": matched,
		"unmatched_site": total_map - matched,
		"created_by_sync": created,
		"warehouses_selected": len(s.get("warehouses") or []),
		"connected": bool(s.store_url and s.api_user and s.api_key),
	}


def _recent_logs(limit=15):
	return frappe.get_all(
		"CSCart Sync Log",
		fields=["name", "sync_type", "status", "started_on", "duration_sec",
			"stock_changes", "price_changes", "price_held", "disabled", "created", "failed", "planned_changes"],
		order_by="creation desc",
		limit=limit,
	)


def _price_holds(status=None, limit=100):
	filters = {}
	if status:
		filters["status"] = status
	return frappe.get_all(
		"CSCart Price Hold",
		filters=filters,
		fields=["name", "item_code", "product_id", "old_price", "new_price", "change_pct", "status", "modified"],
		order_by="modified desc",
		limit=limit,
	)


@frappe.whitelist()
def get_logs(limit=30):
	_guard()
	return _recent_logs(cint(limit) or 30)


@frappe.whitelist()
def get_log_detail(name):
	_guard()
	doc = frappe.get_doc("CSCart Sync Log", name)
	return {"name": doc.name, "status": doc.status, "sync_type": doc.sync_type,
		"changes": doc.changes, "errors": doc.errors,
		"started_on": doc.started_on, "finished_on": doc.finished_on}


@frappe.whitelist()
def get_price_holds(status=None):
	_guard()
	return _price_holds(status)


@frappe.whitelist()
def test_external_source(payload):
	"""Probe a remote ERPNext source and report how many items / total qty it holds."""
	_guard()
	row = frappe.parse_json(payload)
	s = frappe.get_single(SETTINGS_DT)

	secret = row.get("api_secret")
	if not secret and row.get("source_name"):
		for r in (s.external_sources or []):
			if r.source_name == row.get("source_name") and r.api_secret:
				secret = r.get_password("api_secret")
				break

	cfg = {
		"base_url": (row.get("base_url") or "").strip().rstrip("/"),
		"api_key": (row.get("api_key") or "").strip(),
		"api_secret": secret,
		"warehouses": [w.strip() for w in (row.get("warehouses") or "").replace(",", "\n").splitlines() if w.strip()],
		"verify_ssl": bool(row.get("verify_ssl")),
	}
	data = external.fetch_source_qty(cfg)
	return {
		"ok": True,
		"items": len(data),
		"total_qty": round(sum(data.values()), 2),
		"sample": list(data.items())[:5],
	}


@frappe.whitelist()
def get_warehouses():
	"""All active warehouses, for the stock-source picker table."""
	_guard()
	rows = frappe.get_all(
		"Warehouse",
		filters={"disabled": 0},
		fields=["name", "warehouse_name", "company", "is_group"],
		order_by="company asc, is_group asc, name asc",
		limit_page_length=0,
	)
	return rows


@frappe.whitelist()
def get_products(mode="all", search=None, limit=800):
	"""Website products from the map with their ERP match + stock comparison.

	mode: all | matched | unmatched | changes
	"""
	_guard()
	s = frappe.get_single(SETTINGS_DT)
	search = (search or "").strip().upper()
	limit = cint(limit) or 800

	items = erp.eligible_items(s)
	norm_to_item = {m["sku_normalized"]: c for c, m in items.items() if m["sku_normalized"]}
	item_keys = list(items.keys())

	local_raw = erp.local_raw_quantities(s, item_keys)
	src_data = external.per_source_qty(s, items)         # [{name, qtys}] scoped
	source_names = [d["name"] for d in src_data]
	has_qty = bool(s.get("warehouses")) or bool(src_data)
	total = erp.erp_quantities(s, item_keys, items_meta=items) if has_qty else {}
	prices = erp.erp_prices(s, items) if s.price_list else {}

	map_rows = frappe.get_all(
		"CSCart Product Map",
		fields=["product_id", "sku", "sku_normalized", "item_code", "product_name",
			"cscart_status", "cscart_amount", "cscart_price"],
		order_by="sku asc",
		limit_page_length=0,
	)

	all_rows = []
	matched_n = 0
	for r in map_rows:
		item_code = r.item_code or norm_to_item.get(r.sku_normalized)
		matched = bool(item_code and item_code in items)
		if matched:
			matched_n += 1
		if matched and has_qty:
			local_qty = int(flt(local_raw.get(item_code, 0)))
			sources_row = [{"name": d["name"], "qty": int(flt(d["qtys"].get(item_code, 0)))} for d in src_data]
			total_qty = cint(total.get(item_code, 0))
			# Δ = Site − ERP: negative when the site is short of ERP stock.
			qty_diff = cint(r.cscart_amount) - total_qty
		else:
			local_qty, sources_row, total_qty, qty_diff = None, [], None, None
		erp_rate = prices.get(item_code) if matched else None
		erp_price = sync.compute_site_price(s, erp_rate) if erp_rate else None
		site_price = flt(r.cscart_price)
		# Δ = Site − ERP, same convention as qty.
		price_diff = round(site_price - erp_price, 2) if erp_price is not None else None
		all_rows.append({
			"product_id": r.product_id, "sku": r.sku, "item_code": item_code if matched else None,
			"product_name": r.product_name, "site_status": r.cscart_status,
			"local_qty": local_qty, "sources": sources_row, "total_qty": total_qty, "erp_qty": total_qty,
			"site_qty": cint(r.cscart_amount), "qty_diff": qty_diff,
			"erp_price": erp_price, "site_price": site_price, "price_diff": price_diff,
			"matched": matched,
		})

	def keep(row):
		if mode == "matched" and not row["matched"]:
			return False
		if mode == "unmatched" and row["matched"]:
			return False
		if mode == "changes" and not row["qty_diff"]:
			return False
		if search and search not in (row["sku"] or "").upper() and search not in (row["item_code"] or "").upper():
			return False
		return True

	shown = [r for r in all_rows if keep(r)][:limit]
	return {
		"rows": shown,
		"source_names": source_names,
		"counts": {
			"total": len(all_rows),
			"matched": matched_n,
			"unmatched": len(all_rows) - matched_n,
			"has_warehouses": has_qty,
		},
	}


# ---------------------------------------------------------------------------
# Write settings
# ---------------------------------------------------------------------------

@frappe.whitelist()
def save_settings(payload):
	_guard()
	data = frappe.parse_json(payload)
	s = frappe.get_single(SETTINGS_DT)

	for f in WRITABLE_FIELDS:
		if f in data:
			s.set(f, data.get(f))

	if data.get("api_key"):
		s.api_key = data["api_key"]

	for field, (child_dt, link) in CHILD_FIELDS.items():
		if field in data:
			s.set(field, [])
			for value in (data.get(field) or []):
				if value:
					s.append(field, {link: value})

	if "external_sources" in data:
		# preserve existing secrets by source_name when the UI sends a blank one
		old_secrets = {}
		for r in (s.external_sources or []):
			if r.api_secret:
				try:
					old_secrets[r.source_name] = r.get_password("api_secret")
				except Exception:
					pass
		s.set("external_sources", [])
		for row in (data.get("external_sources") or []):
			if not (row.get("source_name") and row.get("base_url")):
				continue
			secret = row.get("api_secret") or old_secrets.get(row.get("source_name"))
			s.append("external_sources", {
				"source_name": row.get("source_name"),
				"enabled": 1 if row.get("enabled") else 0,
				"base_url": row.get("base_url"),
				"api_key": row.get("api_key"),
				"api_secret": secret,
				"warehouses": row.get("warehouses"),
				"merge_mode": row.get("merge_mode") or "Add on top",
				"verify_ssl": 1 if row.get("verify_ssl") else 0,
				"scope_items": row.get("scope_items"),
				"scope_brands": row.get("scope_brands"),
				"scope_item_groups": row.get("scope_item_groups"),
			})

	s.save(ignore_permissions=True)
	frappe.db.commit()
	return get_state()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

@frappe.whitelist()
def test_connection():
	_guard()
	s = frappe.get_single(SETTINGS_DT)
	client = api.get_client(s)
	total = client.ping()
	return {"ok": True, "total_products": total}


@frappe.whitelist()
def refresh_map():
	_guard()
	frappe.enqueue("isoft_cscart_integration.sync.run_map_refresh", queue="long", timeout=3600,
		job_name="cscart_map_refresh")
	return {"queued": True}


@frappe.whitelist()
def sync_now(full=0):
	_guard()
	sync_type = "Full" if cint(full) else "Incremental"
	frappe.enqueue("isoft_cscart_integration.sync.run_sync", queue="long", timeout=3600,
		job_name="cscart_sync_manual", sync_type=sync_type)
	return {"queued": True, "sync_type": sync_type}


@frappe.whitelist()
def set_price_hold_status(name, status):
	_guard()
	if status not in ("Pending", "Approved", "Rejected"):
		frappe.throw(_("Invalid status"))
	frappe.db.set_value("CSCart Price Hold", name, "status", status)
	frappe.db.commit()
	return {"ok": True}


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_preview(only_diff=1, search=None, limit=500):
	"""Bounded preview: what the next run would do, plus match gaps."""
	_guard()
	s = frappe.get_single(SETTINGS_DT)
	only_diff = cint(only_diff)
	search = (search or "").strip().upper()
	limit = cint(limit) or 500

	items = erp.eligible_items(s)
	qtys = erp.erp_quantities(s, list(items.keys()))
	prices = erp.erp_prices(s, items)
	norm_to_item = {m["sku_normalized"]: c for c, m in items.items() if m["sku_normalized"]}

	map_rows = frappe.get_all(
		"CSCart Product Map",
		fields=["product_id", "sku", "sku_normalized", "item_code", "product_name",
			"cscart_status", "cscart_amount", "cscart_price"],
		limit_page_length=0,
	)

	rows = []
	mapped_norms = set()
	for r in map_rows:
		item_code = r.item_code or norm_to_item.get(r.sku_normalized)
		matched = bool(item_code and item_code in items)
		if r.sku_normalized:
			mapped_norms.add(r.sku_normalized)
		erp_qty = cint(qtys.get(item_code, 0)) if matched else None
		erp_rate = prices.get(item_code) if matched else None
		erp_price = sync.compute_site_price(s, erp_rate) if erp_rate else None

		qty_diff = (erp_qty - cint(r.cscart_amount)) if matched else None
		price_diff_pct = None
		if matched and erp_price and flt(r.cscart_price) > 0:
			price_diff_pct = round((erp_price - flt(r.cscart_price)) / flt(r.cscart_price) * 100, 1)

		action = _preview_action(s, matched, erp_qty, r, erp_price)
		if only_diff and action == "-":
			continue
		if search and search not in (r.sku or "").upper() and search not in (item_code or "").upper():
			continue
		rows.append({
			"product_id": r.product_id, "sku": r.sku, "item_code": item_code,
			"product_name": r.product_name, "site_status": r.cscart_status,
			"erp_qty": erp_qty, "site_qty": cint(r.cscart_amount), "qty_diff": qty_diff,
			"erp_price": erp_price, "site_price": flt(r.cscart_price), "price_diff_pct": price_diff_pct,
			"action": action, "matched": matched,
		})
		if len(rows) >= limit:
			break

	erp_only = [
		{"item_code": c, "sku": m["sku"], "item_name": m["item_name"]}
		for c, m in items.items() if m["sku_normalized"] not in mapped_norms
	][:limit]
	site_only = [
		{"product_id": r.product_id, "sku": r.sku, "product_name": r.product_name}
		for r in map_rows if not (r.item_code or norm_to_item.get(r.sku_normalized))
	][:limit]

	return {
		"rows": rows,
		"erp_only": erp_only,
		"site_only": site_only,
		"counts": {
			"eligible_items": len(items),
			"map_rows": len(map_rows),
			"erp_only": len(erp_only),
			"site_only": len(site_only),
		},
	}


@frappe.whitelist()
def get_erp_only(only_stock=1, search=None, limit=800):
	"""ERP items that are NOT on the website (no matching product), so they can
	be reviewed/created. By default only those that currently have stock."""
	_guard()
	s = frappe.get_single(SETTINGS_DT)
	only_stock = cint(only_stock)
	search = (search or "").strip().upper()
	limit = cint(limit) or 800

	items = erp.eligible_items(s)
	qtys = erp.erp_quantities(s, list(items.keys()), items_meta=items)

	site_norms = set(frappe.get_all("CSCart Product Map", pluck="sku_normalized"))
	site_norms.discard(None)

	rows = []
	for code, m in items.items():
		if m["sku_normalized"] in site_norms:
			continue  # already on the website
		qty = cint(qtys.get(code, 0))
		if only_stock and qty <= 0:
			continue
		if search and search not in (code or "").upper() and search not in (m["sku"] or "").upper():
			continue
		rows.append({
			"item_code": code, "item_name": m["item_name"], "sku": m["sku"],
			"brand": m["brand"], "item_group": m["item_group"], "erp_qty": qty,
		})
	rows.sort(key=lambda r: -r["erp_qty"])
	return {"rows": rows[:limit], "total": len(rows)}


def _preview_action(s, matched, erp_qty, r, erp_price):
	if not matched:
		return "unmatched"
	parts = []
	if s.sync_stock and erp_qty is not None and erp_qty != cint(r.cscart_amount):
		parts.append("stock→{0}".format(erp_qty))
	if s.sync_price and erp_price and abs(erp_price - flt(r.cscart_price)) >= 0.01:
		parts.append("price→{0}".format(erp_price))
	return ", ".join(parts) if parts else "-"
