# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""ERPNext side of the sync: which items are eligible, their web-sellable
quantity, and their selling price. Everything here is read-only."""

import math

import frappe

# Fixed whitelist. User input never reaches the SQL expression.
QTY_BASIS_EXPR = {
	"Actual Qty": "actual_qty",
	"Actual Qty - Reserved Qty": "actual_qty - reserved_qty",
	"Projected Qty": "projected_qty",
}


def normalize_sku(value):
	if value is None:
		return None
	v = str(value).strip().upper()
	return v or None


# ---------------------------------------------------------------------------
# Warehouses / item groups: nested-set expansion
# ---------------------------------------------------------------------------

def expand_warehouses(names):
	"""Expand any group warehouses to all their leaf descendants."""
	if not names:
		return []
	leaves = set()
	for name in names:
		row = frappe.db.get_value("Warehouse", name, ["is_group", "lft", "rgt"], as_dict=True)
		if not row:
			continue
		if row.is_group:
			children = frappe.get_all(
				"Warehouse",
				filters={"lft": [">=", row.lft], "rgt": ["<=", row.rgt], "is_group": 0},
				pluck="name",
			)
			leaves.update(children)
		else:
			leaves.add(name)
	return list(leaves)


def expand_item_groups(names):
	"""Expand each item group to its whole subtree (public alias)."""
	return _expand_item_groups(names)


def _expand_item_groups(names):
	"""Expand each item group to its whole subtree."""
	if not names:
		return []
	out = set()
	for name in names:
		row = frappe.db.get_value("Item Group", name, ["lft", "rgt"], as_dict=True)
		if not row:
			continue
		subtree = frappe.get_all(
			"Item Group",
			filters={"lft": [">=", row.lft], "rgt": ["<=", row.rgt]},
			pluck="name",
		)
		out.update(subtree)
	return list(out)


# ---------------------------------------------------------------------------
# Eligible items
# ---------------------------------------------------------------------------

def eligible_items(settings):
	"""Return a dict: item_code -> {item_name, brand, item_group, disabled, sku, sku_normalized}.

	Honours the item / brand / item-group filters and skip_disabled_items.
	Only stock items without variants and with a non-empty SKU are returned.
	"""
	sku_field = settings.sku_field or "item_code"

	filters = {"is_stock_item": 1, "has_variants": 0}
	if settings.skip_disabled_items:
		filters["disabled"] = 0

	# --- item group filter ---
	group_names = [r.item_group for r in (settings.item_groups or []) if r.item_group]
	if group_names:
		expanded = _expand_item_groups(group_names)
		if settings.item_group_filter_type == "Include Only":
			filters["item_group"] = ["in", expanded]
		else:
			filters["item_group"] = ["not in", expanded]

	# --- brand filter ---
	brand_names = [r.brand for r in (settings.brands or []) if r.brand]
	if brand_names:
		if settings.brand_filter_type == "Include Only":
			filters["brand"] = ["in", brand_names]
		else:
			filters["brand"] = ["not in", brand_names]

	# --- explicit item filter ---
	item_names = [r.item_code for r in (settings.items or []) if r.item_code]
	if item_names:
		if settings.item_filter_type == "Include Only":
			filters["item_code"] = ["in", item_names]
		else:
			filters["item_code"] = ["not in", item_names]

	fields = ["item_code", "item_name", "brand", "item_group", "disabled", "stock_uom"]
	if sku_field not in fields:
		fields.append(sku_field)

	rows = frappe.get_all("Item", filters=filters, fields=fields, limit_page_length=0)

	out = {}
	for r in rows:
		sku = r.get(sku_field)
		if not sku:
			continue
		out[r.item_code] = {
			"item_code": r.item_code,
			"item_name": r.item_name,
			"brand": r.brand,
			"item_group": r.item_group,
			"disabled": r.disabled,
			"stock_uom": r.stock_uom,
			"sku": sku,
			"sku_normalized": normalize_sku(sku),
		}
	return out


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------

def local_raw_quantities(settings, item_codes=None):
	"""Local warehouse qty per item, raw sum (no safety stock, no external)."""
	warehouses = expand_warehouses(
		[r.warehouse for r in (settings.warehouses or []) if r.warehouse]
	)
	local = {}
	if not warehouses:
		return local
	expr = QTY_BASIS_EXPR.get(settings.qty_basis, QTY_BASIS_EXPR["Actual Qty - Reserved Qty"])
	conditions = ["warehouse in %(warehouses)s"]
	params = {"warehouses": tuple(warehouses)}
	if item_codes:
		conditions.append("item_code in %(items)s")
		params["items"] = tuple(item_codes)
	raw = frappe.db.sql(
		"""
		select item_code, sum({expr}) as qty
		from `tabBin`
		where {where}
		group by item_code
		""".format(expr=expr, where=" and ".join(conditions)),
		params,
		as_dict=True,
	)
	for row in raw:
		local[row.item_code] = row.qty or 0
	return local


def erp_quantities(settings, item_codes=None, failures=None, items_meta=None):
	"""Return item_code -> integer web-sellable qty (floored at 0).

	Local warehouse qty plus any enabled External Stock Sources, merged per each
	source's Merge Mode and per-source scope, with safety stock applied once to
	the combined total. Names of failed sources are appended to `failures`.
	"""
	from isoft_cscart_integration import external

	safety = float(settings.safety_stock or 0)
	local = local_raw_quantities(settings, item_codes)
	combined = external.merge_external(settings, local, failures, items_meta)

	out = {}
	for item_code, qty in combined.items():
		q = math.floor((qty or 0) - safety)
		out[item_code] = int(q) if q > 0 else 0
	return out


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

def erp_prices(settings, items_meta):
	"""Return item_code -> price_list_rate for the configured selling price list.

	items_meta is the dict from eligible_items (needed for stock_uom matching).
	Picks the row with no customer/batch, valid for today, latest valid_from.
	"""
	if not settings.price_list:
		return {}

	today = frappe.utils.nowdate()
	rows = frappe.get_all(
		"Item Price",
		filters={"price_list": settings.price_list, "selling": 1},
		fields=[
			"item_code",
			"price_list_rate",
			"uom",
			"customer",
			"batch_no",
			"valid_from",
			"valid_upto",
		],
		limit_page_length=0,
	)

	# best row per item
	best = {}
	for r in rows:
		if r.customer or r.batch_no:
			continue
		if r.valid_from and str(r.valid_from) > today:
			continue
		if r.valid_upto and str(r.valid_upto) < today:
			continue
		meta = items_meta.get(r.item_code)
		# uom must match the item's stock uom (or be blank)
		if r.uom and meta and meta.get("stock_uom") and r.uom != meta["stock_uom"]:
			continue
		cur = best.get(r.item_code)
		if cur is None or (r.valid_from or "") > (cur.get("valid_from") or ""):
			best[r.item_code] = {"rate": r.price_list_rate, "valid_from": r.valid_from or ""}

	return {code: v["rate"] for code, v in best.items()}
