# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CSCartIntegrationSettings(Document):
	def validate(self):
		self.validate_price_sync()
		self.validate_price_list_currency()
		self.validate_sku_field()
		self.validate_store_url()

	def validate_price_sync(self):
		if self.sync_price and not self.price_list:
			frappe.throw(_("Select a Selling Price List before enabling Sync Price."))

	def validate_price_list_currency(self):
		if not self.price_list:
			return
		row = frappe.db.get_value(
			"Price List", self.price_list, ["currency", "selling"], as_dict=True
		)
		if not row:
			return
		if not row.selling:
			frappe.throw(_("Price List {0} is not a Selling price list.").format(self.price_list))
		if row.currency and row.currency != "AOA":
			frappe.msgprint(
				_("Warning: Price List {0} currency is {1}, not AOA. The CS-Cart store stores prices in AOA.").format(
					self.price_list, row.currency
				),
				indicator="orange",
				alert=True,
			)

	def validate_sku_field(self):
		field = self.sku_field or "item_code"
		meta = frappe.get_meta("Item")
		if field != "item_code" and not meta.has_field(field):
			frappe.throw(_("SKU Field {0} does not exist on Item.").format(field))

	def validate_store_url(self):
		if self.store_url:
			self.store_url = self.store_url.strip().rstrip("/")
			if not self.store_url.startswith(("http://", "https://")):
				frappe.throw(_("Store URL must start with http:// or https://"))
