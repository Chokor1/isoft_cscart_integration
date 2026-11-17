# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

import frappe

ROLE = "CSCart Manager"
# Read-only access to the /stock lookup page only (no console, no settings).
STOCK_ROLE = "Stock Viewer"


def after_install():
	ensure_role()


def ensure_role():
	created = False
	for name in (ROLE, STOCK_ROLE):
		if not frappe.db.exists("Role", name):
			frappe.get_doc({
				"doctype": "Role",
				"role_name": name,
				"desk_access": 1,
			}).insert(ignore_permissions=True)
			created = True
	if created:
		frappe.db.commit()
