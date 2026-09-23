# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Standalone, full-screen CS-Cart console served at /cscart.

Rendered as a bare HTML document (no desk / no website chrome) so the launcher
can open it in a clean new tab. Auth is enforced here; the JSON API it calls
(isoft_cscart_integration.console.*) re-checks System Manager on every request.
"""

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/cscart"
		raise frappe.Redirect

	if not ({"System Manager", "CSCart Manager"} & set(frappe.get_roles())):
		frappe.throw(_("You are not permitted to access the CS-Cart console."), frappe.PermissionError)

	# get_csrf_token may generate + store a token, which needs a commit.
	context.csrf_token = frappe.sessions.get_csrf_token()
	frappe.db.commit()

	context.no_cache = 1
	context.user = frappe.session.user
	context.full_name = frappe.utils.get_fullname(frappe.session.user)

	theme = (frappe.db.get_value("User", frappe.session.user, "desk_theme") or "Light").lower()
	context.theme = theme if theme in ("light", "dark") else "light"

	# Use the site's own favicon (Website Settings), same as every other app.
	context.favicon = (
		frappe.db.get_single_value("Website Settings", "favicon")
		or "/assets/frappe/images/frappe-favicon.svg"
	)
	return context
