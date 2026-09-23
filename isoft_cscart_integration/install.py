# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

import frappe

ROLE = "CSCart Manager"


def after_install():
	ensure_role()


def ensure_role():
	if not frappe.db.exists("Role", ROLE):
		frappe.get_doc({
			"doctype": "Role",
			"role_name": ROLE,
			"desk_access": 1,
		}).insert(ignore_permissions=True)
		frappe.db.commit()
