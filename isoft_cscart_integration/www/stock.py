# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Standalone stock lookup at /stock — separate from the CS-Cart console.

Open to System Manager, CSCart Manager and the read-only Stock Viewer role."""

import frappe
from frappe import _

from isoft_cscart_integration.stock_check import can_view

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/stock"
		raise frappe.Redirect

	if not can_view():
		frappe.throw(_("You are not permitted to check stock."), frappe.PermissionError)

	context.csrf_token = frappe.sessions.get_csrf_token()
	frappe.db.commit()

	context.no_cache = 1
	context.user = frappe.session.user
	context.full_name = frappe.utils.get_fullname(frappe.session.user)
	theme = (frappe.db.get_value("User", frappe.session.user, "desk_theme") or "Light").lower()
	context.theme = theme if theme in ("light", "dark") else "light"
	context.favicon = (
		frappe.db.get_single_value("Website Settings", "favicon")
		or "/assets/frappe/images/frappe-favicon.svg"
	)
	return context
