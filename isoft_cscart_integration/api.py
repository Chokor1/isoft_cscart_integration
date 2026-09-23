# Copyright (c) 2026, ITEC and contributors
# For license information, please see license.txt

"""Thin client for the CS-Cart REST API.

Auth is HTTP Basic: username = admin user's email, password = that user's API key.
Only the endpoints phase 1 needs are implemented: list, update (partial), create, ping.

A Cloudflare challenge returns an HTML body, not JSON, so every response is checked
for a JSON content type / parseable body before it is trusted.
"""

import time

import frappe
import requests
from frappe import _


class CSCartError(frappe.ValidationError):
	pass


DEFAULT_TIMEOUT = 30
LIST_PAGE_SIZE = 250
# 0, not "false": PHP treats the string "false" as truthy and loads heavy data.
LIST_LEAN_PARAMS = {
	"get_features": 0,
	"get_detailed": 0,
	"get_icon": 0,
	"get_additional": 0,
	"get_options": 0,
}


class CSCartClient:
	def __init__(self, store_url, api_user, api_key, verify_ssl=True, timeout=DEFAULT_TIMEOUT):
		if not (store_url and api_user and api_key):
			raise CSCartError(_("CS-Cart connection is not configured (store URL, user or key missing)."))
		self.base = store_url.strip().rstrip("/") + "/api/"
		self.verify_ssl = bool(verify_ssl)
		self.timeout = int(timeout or DEFAULT_TIMEOUT)
		self.session = requests.Session()
		self.session.auth = (api_user.strip(), api_key)
		self.session.headers.update(
			{
				"Content-Type": "application/json",
				"Accept": "application/json",
				"User-Agent": "ERPNext-CSCart-Sync/1.0",
			}
		)

	# ---- low level -------------------------------------------------------

	def _request(self, method, path, params=None, json_body=None, _retries=2):
		url = self.base + path.lstrip("/")
		last_exc = None
		for attempt in range(_retries + 1):
			try:
				resp = self.session.request(
					method,
					url,
					params=params,
					json=json_body,
					timeout=self.timeout,
					verify=self.verify_ssl,
				)
			except requests.RequestException as e:
				last_exc = e
				if attempt < _retries:
					time.sleep(1.5 * (attempt + 1))
					continue
				raise CSCartError(_("CS-Cart request failed: {0}").format(e))

			# Retry transient statuses.
			if resp.status_code in (429, 500, 502, 503, 504) and attempt < _retries:
				time.sleep(1.5 * (attempt + 1))
				continue

			return self._parse(resp)

		if last_exc:
			raise CSCartError(_("CS-Cart request failed: {0}").format(last_exc))

	def _parse(self, resp):
		ct = (resp.headers.get("Content-Type") or "").lower()
		if resp.status_code == 401:
			raise CSCartError(_("CS-Cart authentication failed (401). Check the API user and key."))
		if resp.status_code == 403:
			raise CSCartError(
				_(
					"CS-Cart returned 403 (forbidden / Cloudflare challenge). "
					"Ask the site owner to add a WAF skip rule for /api/* from this server's IP."
				)
			)
		if "application/json" not in ct and "text/json" not in ct:
			snippet = (resp.text or "")[:200].replace("\n", " ")
			raise CSCartError(
				_("CS-Cart returned a non-JSON response ({0}). First bytes: {1}").format(
					resp.status_code, snippet
				)
			)
		try:
			data = resp.json()
		except ValueError:
			raise CSCartError(_("CS-Cart returned an unparseable JSON body ({0}).").format(resp.status_code))

		if resp.status_code >= 400:
			msg = ""
			if isinstance(data, dict):
				msg = data.get("message") or data.get("error") or frappe.as_json(data)[:300]
			raise CSCartError(_("CS-Cart error {0}: {1}").format(resp.status_code, msg))
		return data

	# ---- endpoints -------------------------------------------------------

	def ping(self):
		"""Return the total product count, proving auth + reachability."""
		data = self._request(
			"GET",
			"products",
			params={"items_per_page": 1, "page": 1, **LIST_LEAN_PARAMS},
		)
		total = 0
		if isinstance(data, dict):
			total = int((data.get("params") or {}).get("total_items") or 0)
		return total

	def iter_products(self, status=None):
		"""Yield product dicts across all pages (lean payload)."""
		page = 1
		fetched = 0
		total = None
		while True:
			params = {
				"items_per_page": LIST_PAGE_SIZE,
				"page": page,
				"sort_by": "code",
				"sort_order": "asc",
				**LIST_LEAN_PARAMS,
			}
			if status:
				params["status"] = status
			data = self._request("GET", "products", params=params)
			products = (data or {}).get("products") or []
			if total is None:
				total = int((data.get("params") or {}).get("total_items") or 0)
			if not products:
				break
			for p in products:
				yield p
			fetched += len(products)
			if total and fetched >= total:
				break
			page += 1

	def update_product(self, product_id, payload):
		"""PUT a partial payload. Only the keys present are changed server-side."""
		if not payload:
			return None
		return self._request("PUT", "products/{0}".format(product_id), json_body=payload)

	def create_product(self, payload):
		return self._request("POST", "products", json_body=payload)


def get_client(settings=None):
	"""Build a client from CSCart Integration Settings."""
	settings = settings or frappe.get_single("CSCart Integration Settings")
	return CSCartClient(
		store_url=settings.store_url,
		api_user=settings.api_user,
		api_key=settings.get_password("api_key") if settings.api_key else None,
		verify_ssl=settings.verify_ssl,
		timeout=settings.request_timeout,
	)
