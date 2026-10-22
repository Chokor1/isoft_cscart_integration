# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Whitelisted API behind the ISOFT CS-Cart management page.

Everything is System Manager only; sync jobs are enqueued, never run inline
(except Test Connection and a bounded Preview)."""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime

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


MANAGER_ROLES = ("System Manager", "CSCart Manager")


def _guard():
	if not (set(MANAGER_ROLES) & set(frappe.get_roles())):
		frappe.throw(_("You are not permitted to use the CS-Cart console."), frappe.PermissionError)


def no_cache_console_page(response=None, request=None):
	"""after_request hook: tell the browser never to cache the /cscart page, so a
	new build of the console always loads (v13's www renderer sends no
	Cache-Control, which otherwise lets browsers keep a stale copy)."""
	try:
		if response is not None and request is not None and getattr(request, "path", "") == "/cscart":
			response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
			response.headers["Pragma"] = "no-cache"
	except Exception:
		pass


LINK_FIELDS = {
	"Item": "item_code", "Brand": "name", "Item Group": "name",
	"Warehouse": "name", "Price List": "name",
}


@frappe.whitelist()
def search_link(doctype, txt=None, limit=15):
	"""Autocomplete for the console's pickers, gated by the console role so a
	CSCart Manager needs no direct read permission on Item/Brand/etc."""
	_guard()
	field = LINK_FIELDS.get(doctype)
	if not field:
		frappe.throw(_("Not allowed"))
	filters = {}
	if txt:
		filters[field] = ["like", "%" + str(txt).strip() + "%"]
	return frappe.get_all(doctype, filters=filters, pluck=field, order_by=field,
		limit_page_length=cint(limit) or 15)


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
		"source_name": r.source_name, "source_type": r.source_type or "ERPNext", "enabled": r.enabled, "base_url": r.base_url,
		"api_key": r.api_key, "warehouses": r.warehouses, "merge_mode": r.merge_mode,
		"verify_ssl": r.verify_ssl, "secret_set": bool(r.api_secret),
		"scope_items": r.scope_items, "scope_brands": r.scope_brands,
		"scope_item_groups": r.scope_item_groups,
		"exclude_items": r.exclude_items, "exclude_brands": r.exclude_brands,
		"exclude_item_groups": r.exclude_item_groups,
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
		"source_type": row.get("source_type") or "ERPNext",
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


def _sy_search(rows, txt, keys):
	if not txt:
		return rows
	return [r for r in rows if any(txt in (r.get(k) or "").upper() for k in keys)]


def _day_bounds(from_date, to_date):
	"""Inclusive datetime bounds for a from/to date range (YYYY-MM-DD)."""
	from frappe.utils import getdate
	start = (str(getdate(from_date)) + " 00:00:00") if from_date else None
	end = (str(getdate(to_date)) + " 23:59:59") if to_date else None
	return start, end


@frappe.whitelist()
def get_stylus(view="catalogue", search=None, limit=1000, from_date=None, to_date=None):
	"""Stylus views off the latest snapshot:
	catalogue | vs_erp | not_in_erp | movement.
	Movement compares the state entering `from_date` against the state at `to_date`
	(defaults to the latest-vs-previous snapshot when no range is given)."""
	_guard()
	from isoft_cscart_integration import stylus
	snap = stylus.latest_snapshot()
	if not snap:
		return {"rows": [], "taken_on": None, "total": 0}
	name = snap["name"]
	search = (search or "").strip().upper()
	limit = cint(limit) or 1000

	if view == "movement":
		start, end = _day_bounds(from_date, to_date)
		# Compare the first vs the last snapshot inside the selected window, so a
		# single day (default = today) shows that day's intraday movement.
		if start and end:
			rng = {"taken_on": ["between", [start, end]]}
			base = frappe.get_all("CSCart Stylus Snapshot", filters=rng, order_by="taken_on asc", limit=1, fields=["name", "taken_on"])
			cur = frappe.get_all("CSCart Stylus Snapshot", filters=rng, order_by="taken_on desc", limit=1, fields=["name", "taken_on"])
		else:
			cur = frappe.get_all("CSCart Stylus Snapshot", order_by="taken_on desc", limit=1, fields=["name", "taken_on"])
			base = frappe.get_all("CSCart Stylus Snapshot", filters={"name": ["!=", cur[0].name]},
				order_by="taken_on desc", limit=1, fields=["name", "taken_on"]) if cur else []
		if not cur or not base or cur[0].name == base[0].name:
			return {"rows": [], "total": 0, "totals": {},
				"note": "Need at least two snapshots in this range — widen the dates."}
		rows = frappe.db.sql("""
			select a.code, a.designation,
			       b.stock as old_stock, a.stock as new_stock, (a.stock-b.stock) as delta,
			       b.price as old_price, a.price as new_price
			from `tabCSCart Stylus Snapshot Item` a
			join `tabCSCart Stylus Snapshot Item` b on b.code=a.code and b.parent=%(prev)s
			where a.parent=%(cur)s and (a.stock != b.stock or a.price != b.price)
			order by abs(a.stock-b.stock) desc, abs(a.price-b.price) desc
		""", {"cur": cur[0].name, "prev": base[0].name}, as_dict=True)
		rows = _sy_search(rows, search, ("code", "designation"))
		totals = {
			"changed": len(rows),
			"stock_up": sum(1 for r in rows if flt(r.delta) > 0),
			"stock_down": sum(1 for r in rows if flt(r.delta) < 0),
			"net_stock": round(sum(flt(r.delta) for r in rows), 2),
			"price_up": sum(1 for r in rows if flt(r.new_price) - flt(r.old_price) > 0),
			"price_down": sum(1 for r in rows if flt(r.new_price) - flt(r.old_price) < 0),
		}
		from frappe.utils import getdate
		return {"rows": rows[:limit], "from_on": str(base[0].taken_on), "to_on": str(cur[0].taken_on),
			"total": len(rows), "totals": totals,
			"range_from": str(getdate(base[0].taken_on)), "range_to": str(getdate(cur[0].taken_on))}

	items = frappe.get_all("CSCart Stylus Snapshot Item", filters={"parent": name},
		fields=["code", "designation", "brand", "family", "price", "stock", "is_active"],
		order_by="code asc", limit_page_length=0)

	if view in ("vs_erp", "not_in_erp"):
		s = frappe.get_single(SETTINGS_DT)
		erp_codes = set(frappe.get_all("Item", pluck="item_code"))
		local = erp.local_raw_quantities(s)
		prices = erp.erp_prices(s, {}) if s.price_list else {}
		rows = []
		for it in items:
			matched = it.code in erp_codes
			if view == "vs_erp" and not matched:
				continue
			if view == "not_in_erp" and matched:
				continue
			erp_qty = int(flt(local.get(it.code, 0))) if matched else None
			erp_price = prices.get(it.code) if matched else None
			# Δ = ERP − Stylus: positive when ERP holds more than Stylus.
			qty_diff = (erp_qty - it.stock) if erp_qty is not None else None
			qty_diff_pct = round(qty_diff / it.stock * 100, 1) if (qty_diff is not None and it.stock) else None
			price_diff_pct = round((erp_price - it.price) / it.price * 100, 1) if (erp_price and it.price) else None
			rows.append({
				"code": it.code, "designation": it.designation, "brand": it.brand,
				"stylus_stock": it.stock, "erp_qty": erp_qty,
				"qty_diff": qty_diff, "qty_diff_pct": qty_diff_pct,
				"stylus_price": it.price, "erp_price": erp_price,
				"price_diff": round(erp_price - it.price, 2) if erp_price else None,
				"price_diff_pct": price_diff_pct,
			})
		rows = _sy_search(rows, search, ("code", "designation", "brand"))
		return {"rows": rows[:limit], "taken_on": str(snap["taken_on"]), "total": len(rows)}

	rows = _sy_search([dict(it) for it in items], search, ("code", "designation", "brand"))
	return {"rows": rows[:limit], "taken_on": str(snap["taken_on"]), "total": len(rows)}


def _period_key(dt, period):
	from frappe.utils import getdate
	d = getdate(dt)
	if period == "month":
		return d.strftime("%Y-%m")
	if period == "week":
		iso = d.isocalendar()
		return "%d-W%02d" % (iso[0], iso[1])
	return d.strftime("%Y-%m-%d")


@frappe.whitelist()
def get_stylus_item_series(code, from_date=None, to_date=None, period="day"):
	"""One item's stock/price trajectory across snapshots, bucketed by period
	(day | week | month) within the date range — the last snapshot in each bucket,
	with the step-over-step deltas."""
	_guard()
	code = (code or "").strip()
	period = period if period in ("day", "week", "month") else "day"
	start, end = _day_bounds(from_date, to_date)
	cond, params = "", {"code": code}
	if start and end:
		cond = "and s.taken_on between %(s)s and %(e)s"
		params["s"], params["e"] = start, end
	rows = frappe.db.sql("""
		select s.taken_on as taken_on, i.stock as stock, i.price as price
		from `tabCSCart Stylus Snapshot Item` i
		join `tabCSCart Stylus Snapshot` s on s.name = i.parent
		where i.code = %(code)s {0}
		order by s.taken_on asc
	""".format(cond), params, as_dict=True)
	buckets = {}
	for r in rows:
		buckets[_period_key(r.taken_on, period)] = r  # ascending -> last in bucket wins
	series, prev = [], None
	for key in sorted(buckets):
		r = buckets[key]
		series.append({
			"period": key, "stock": flt(r.stock), "price": flt(r.price),
			"d_stock": round(flt(r.stock) - flt(prev.stock), 2) if prev else None,
			"d_price": round(flt(r.price) - flt(prev.price), 2) if prev else None,
		})
		prev = r
	from frappe.utils import getdate
	rf = str(getdate(rows[0].taken_on)) if rows else (from_date or "")
	rt = str(getdate(rows[-1].taken_on)) if rows else (to_date or "")
	return {"code": code, "period": period, "series": series, "range_from": rf, "range_to": rt}


@frappe.whitelist()
def snapshot_stylus_now():
	_guard()
	frappe.enqueue("isoft_cscart_integration.stylus.snapshot_stylus", queue="long",
		timeout=600, job_name="cscart_stylus_snapshot")
	return {"queued": True}


@frappe.whitelist()
def get_stylus_price_log(search=None, from_date=None, to_date=None, days=None, limit=100000):
	"""Persistent log of Stylus price changes (one row per code per snapshot where
	the price moved), filtered to a from/to date range (falls back to `days` back,
	then all history)."""
	_guard()
	from frappe.utils import getdate, today
	search = (search or "").strip()
	limit = cint(limit) or 100000
	# Default (no range given) = the latest day with changes through today, so it
	# opens non-empty. Today's data if any, else the most recent change day.
	if not from_date and not to_date and not days:
		latest = frappe.get_all("CSCart Stylus Price Log", fields=["changed_on"],
			order_by="changed_on desc", limit=1)
		if latest:
			from_date = str(getdate(latest[0].changed_on))
			to_date = today()
	filters = {}
	start, end = _day_bounds(from_date, to_date)
	if start and end:
		filters["changed_on"] = ["between", [start, end]]
	elif start:
		filters["changed_on"] = [">=", start]
	elif end:
		filters["changed_on"] = ["<=", end]
	elif days:
		filters["changed_on"] = [">=", add_days(now_datetime(), -cint(days))]
	if search:
		filters["code"] = ["like", "%" + search + "%"]
	rows = frappe.get_all("CSCart Stylus Price Log", filters=filters,
		fields=["code", "designation", "brand", "changed_on", "old_price", "new_price", "delta", "pct"],
		order_by="changed_on desc", limit_page_length=limit)
	totals = {
		"changes": len(rows),
		"up": sum(1 for r in rows if flt(r.delta) > 0),
		"down": sum(1 for r in rows if flt(r.delta) < 0),
		"items": len({r.code for r in rows}),
	}
	for r in rows:
		r["changed_on"] = str(r["changed_on"])
	total = len(rows)
	return {"rows": rows, "total": total, "totals": totals,
		"range_from": from_date or "", "range_to": to_date or ""}


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
def get_products(mode="all", search=None, limit=800, hide_disabled=0, hide_zero=0):
	"""Website products from the map with their ERP match + stock comparison.

	mode: all | matched | unmatched | changes
	hide_disabled: drop products disabled on the site (status D).
	hide_zero: drop rows whose ERP qty is 0 or has no ERP match.
	"""
	_guard()
	s = frappe.get_single(SETTINGS_DT)
	search = (search or "").strip().upper()
	limit = cint(limit) or 800
	hide_disabled = cint(hide_disabled)
	hide_zero = cint(hide_zero)

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
		site_amount = cint(r.cscart_amount)
		if matched and has_qty:
			local_qty = int(flt(local_raw.get(item_code, 0)))
			sources_row = [{"name": d["name"], "qty": int(flt(d["qtys"].get(item_code, 0)))} for d in src_data]
			total_qty = cint(total.get(item_code, 0))
			# Δ = ERP − Site: positive when ERP holds more than the site.
			qty_diff = total_qty - site_amount
			qty_diff_pct = round(qty_diff / site_amount * 100, 1) if site_amount else None
		else:
			local_qty, sources_row, total_qty, qty_diff, qty_diff_pct = None, [], None, None, None
		erp_rate = prices.get(item_code) if matched else None
		erp_price = sync.compute_site_price(s, erp_rate) if erp_rate else None
		site_price = flt(r.cscart_price)
		# Δ = ERP − Site, same convention as qty.
		price_diff = round(erp_price - site_price, 2) if erp_price is not None else None
		price_diff_pct = round((erp_price - site_price) / site_price * 100, 1) if (erp_price is not None and site_price) else None
		all_rows.append({
			"product_id": r.product_id, "sku": r.sku, "item_code": item_code if matched else None,
			"product_name": r.product_name, "site_status": r.cscart_status,
			"local_qty": local_qty, "sources": sources_row, "total_qty": total_qty, "erp_qty": total_qty,
			"site_qty": site_amount, "qty_diff": qty_diff, "qty_diff_pct": qty_diff_pct,
			"erp_price": erp_price, "site_price": site_price, "price_diff": price_diff, "price_diff_pct": price_diff_pct,
			"matched": matched,
		})

	def keep(row):
		if hide_disabled and row["site_status"] == "D":
			return False
		if hide_zero and not row["total_qty"]:
			return False
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
				"source_type": row.get("source_type") or "ERPNext",
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
				"exclude_items": row.get("exclude_items"),
				"exclude_brands": row.get("exclude_brands"),
				"exclude_item_groups": row.get("exclude_item_groups"),
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
