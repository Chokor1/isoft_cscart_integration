// Copyright (c) 2026, ITEC and contributors
// For license information, please see license.txt
//
// ISOFT CS-Cart integration console. One page drives everything: connection,
// what to sync, the product map, a preview of the next run, the run logs and
// the price holds. Styled after ISOFT Invenza (Inter + slate/blue palette).
// The backend doctypes are storage only; users live here.

frappe.pages["isoft-cscart"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("ISOFT CS-Cart"),
		single_column: true,
	});
	frappe.require("/assets/frappe/css/font-awesome.min.css");
	new CSCartConsole(page, wrapper);
};

const API = "isoft_cscart_integration.console.";

class CSCartConsole {
	constructor(page, wrapper) {
		this.page = page;
		this.$body = $(wrapper).find(".layout-main-section");
		this.state = null;
		this.tab = "dashboard";
		this.inject_styles();
		this.render_shell();
		this.load_state();
	}

	// -------------------------------------------------------------------
	call(method, args, freeze_msg) {
		// frappe.call returns a jQuery Deferred (has .then but no .catch), so wrap
		// it in a native Promise for clean async/await-style chaining.
		return new Promise((resolve, reject) => {
			frappe.call({
				method: API + method,
				args: args || {},
				freeze: !!freeze_msg,
				freeze_message: freeze_msg,
				callback: (r) => resolve(r.message),
				error: (e) => {
					const msg = (e && e.responseJSON && e.responseJSON._server_messages)
						|| (e && e.message) || __("Request failed");
					frappe.msgprint({ title: __("Error"), message: msg, indicator: "red" });
					reject(e);
				},
			});
		});
	}

	// -------------------------------------------------------------------
	load_state() {
		this.call("get_state").then((m) => {
			this.state = m;
			this.render();
		});
	}

	// -------------------------------------------------------------------
	render_shell() {
		this.$body.html(`
			<div class="cs-root">
				<aside class="cs-side">
					<div class="cs-brand"><i class="fa fa-shopping-cart"></i><span>CS-Cart<br><small>Integration</small></span></div>
					<nav class="cs-nav"></nav>
					<div class="cs-side-foot"><span class="cs-dot"></span><span class="cs-conn-label">—</span></div>
				</aside>
				<main class="cs-main">
					<div class="cs-topbar">
						<div class="cs-pills"></div>
						<div class="cs-actions">
							<button class="cs-btn cs-btn-ghost" data-act="test"><i class="fa fa-plug"></i> ${__("Test Connection")}</button>
							<button class="cs-btn cs-btn-ghost" data-act="refresh"><i class="fa fa-refresh"></i> ${__("Refresh Map")}</button>
							<button class="cs-btn cs-btn-ghost" data-act="full"><i class="fa fa-database"></i> ${__("Full Sync")}</button>
							<button class="cs-btn cs-btn-primary" data-act="sync"><i class="fa fa-bolt"></i> ${__("Sync Now")}</button>
						</div>
					</div>
					<div class="cs-content"></div>
				</main>
			</div>
		`);

		const tabs = [
			["dashboard", "fa-th-large", __("Dashboard")],
			["settings", "fa-sliders", __("Settings")],
			["preview", "fa-table", __("Preview")],
			["logs", "fa-list-alt", __("Logs")],
			["holds", "fa-hand-paper-o", __("Price Holds")],
		];
		const $nav = this.$body.find(".cs-nav");
		tabs.forEach(([key, icon, label]) => {
			$(`<a class="cs-nav-link" data-tab="${key}"><i class="fa ${icon}"></i> ${label}</a>`)
				.appendTo($nav)
				.on("click", () => this.switch_tab(key));
		});

		this.$body.find(".cs-actions").on("click", ".cs-btn", (e) => {
			const act = $(e.currentTarget).data("act");
			if (act === "test") this.do_test();
			if (act === "refresh") this.do_refresh();
			if (act === "sync") this.do_sync(0);
			if (act === "full") this.do_sync(1);
		});
	}

	switch_tab(key) {
		this.tab = key;
		this.render();
	}

	render() {
		if (!this.state) return;
		this.$body.find(".cs-nav-link").removeClass("active");
		this.$body.find(`.cs-nav-link[data-tab="${this.tab}"]`).addClass("active");
		this.render_pills();
		this.render_conn_foot();
		const $c = this.$body.find(".cs-content");
		if (this.tab === "dashboard") this.render_dashboard($c);
		else if (this.tab === "settings") this.render_settings($c);
		else if (this.tab === "preview") this.render_preview($c);
		else if (this.tab === "logs") this.render_logs($c);
		else if (this.tab === "holds") this.render_holds($c);
	}

	// -------------------------------------------------------------------
	render_pills() {
		const s = this.state.settings;
		const d = this.state.dashboard;
		const pill = (on, label) => `<span class="cs-pill ${on ? "on" : "off"}">${label}</span>`;
		const mode = s.dry_run ? `<span class="cs-pill warn">${__("DRY RUN")}</span>` : `<span class="cs-pill live">${__("LIVE")}</span>`;
		this.$body.find(".cs-pills").html(`
			${s.enabled ? `<span class="cs-pill on">${__("Enabled")}</span>` : `<span class="cs-pill off">${__("Disabled")}</span>`}
			${mode}
			${pill(s.sync_stock, __("Stock"))}
			${pill(s.sync_price, __("Price"))}
			${d.pending_holds ? `<span class="cs-pill warn">${d.pending_holds} ${__("holds")}</span>` : ""}
			${this.state.running ? `<span class="cs-pill run"><i class="fa fa-spinner fa-spin"></i> ${__("Running")}</span>` : ""}
		`);
	}

	render_conn_foot() {
		const ok = this.state.dashboard.connected;
		this.$body.find(".cs-dot").css("background", ok ? "var(--cs-ok)" : "var(--cs-muted)");
		this.$body.find(".cs-conn-label").text(ok ? __("Configured") : __("Not configured"));
	}

	// ===================================================================
	// DASHBOARD
	// ===================================================================
	render_dashboard($c) {
		const d = this.state.dashboard;
		const s = this.state.settings;
		const card = (icon, val, label, tone) => `
			<div class="cs-card">
				<div class="cs-card-ic ${tone || ""}"><i class="fa ${icon}"></i></div>
				<div class="cs-card-body"><div class="cs-card-val">${val}</div><div class="cs-card-lbl">${label}</div></div>
			</div>`;
		$c.html(`
			<div class="cs-grid">
				${card("fa-cubes", frappe.utils.escape_html(String(d.total_map)), __("Products in Map"), "blue")}
				${card("fa-link", String(d.matched), __("Matched to Item"), "green")}
				${card("fa-unlink", String(d.unmatched_site), __("Site-only (no Item)"), "amber")}
				${card("fa-hand-paper-o", String(d.pending_holds), __("Pending Price Holds"), "red")}
				${card("fa-magic", String(d.created_by_sync), __("Created by Sync"), "slate")}
				${card("fa-clock-o", this.fmt_dt(s.last_sync_on), __("Last Sync"), "slate")}
			</div>
			<div class="cs-panel">
				<div class="cs-panel-h">${__("Getting started")}</div>
				<ol class="cs-steps">
					<li>${__("Fill in Connection under Settings and press Test Connection.")}</li>
					<li>${__("Press Refresh Map to pull the CS-Cart catalogue into the product map.")}</li>
					<li>${__("Open Preview to review the match rate and the changes the next run would make.")}</li>
					<li>${__("Keep Dry Run on for a day, then turn on Stock and go live on a few SKUs first.")}</li>
				</ol>
			</div>
		`);
	}

	// ===================================================================
	// SETTINGS
	// ===================================================================
	render_settings($c) {
		const s = this.state.settings;
		this.fields = {};
		$c.html(`<div class="cs-form"></div><div class="cs-form-foot"><button class="cs-btn cs-btn-primary" data-act="save"><i class="fa fa-save"></i> ${__("Save Settings")}</button></div>`);
		const $f = $c.find(".cs-form");

		const section = (title) => $(`<div class="cs-sec"><div class="cs-sec-h">${title}</div><div class="cs-sec-grid"></div></div>`).appendTo($f).find(".cs-sec-grid");

		this.add_control(section(__("General")), "enabled", "Check", __("Enabled (master switch)"), s.enabled);
		this.add_control(this.fields.enabled.parent, "dry_run", "Check", __("Dry Run (log only, no writes)"), s.dry_run);

		const conn = section(__("Connection"));
		this.add_control(conn, "store_url", "Data", __("Store URL"), s.store_url, "https://itec.co.ao");
		this.add_control(conn, "api_user", "Data", __("API User (admin email)"), s.api_user);
		this.add_control(conn, "api_key", "Password", __("API Key"), "", s.api_key_set ? __("•••••• (set — leave blank to keep)") : "");
		this.add_control(conn, "verify_ssl", "Check", __("Verify SSL"), s.verify_ssl);
		this.add_control(conn, "request_timeout", "Int", __("Request Timeout (s)"), s.request_timeout);

		const sch = section(__("Schedule"));
		this.add_control(sch, "sync_frequency", "Select", __("Sync Frequency"), s.sync_frequency,
			null, "Every 5 Minutes\nEvery 15 Minutes\nEvery 30 Minutes\nHourly\nEvery 6 Hours\nDaily");
		this.add_control(sch, "full_sync_daily", "Check", __("Daily Full Reconciliation"), s.full_sync_daily);
		this.add_control(sch, "full_sync_hour", "Int", __("Full Sync Hour (0-23)"), s.full_sync_hour);

		const what = section(__("What to Sync"));
		this.add_control(what, "sync_stock", "Check", __("Sync Stock"), s.sync_stock);
		this.add_control(what, "sync_price", "Check", __("Sync Price"), s.sync_price);
		this.add_control(what, "sync_disable", "Check", __("Disable on site when disabled in ERP"), s.sync_disable);
		this.add_control(what, "create_products", "Check", __("Create new products"), s.create_products);

		const stock = section(__("Stock"));
		this.add_control(stock, "warehouses", "MultiSelectList", __("Warehouses"), s.warehouses, null, "Warehouse");
		this.add_control(stock, "qty_basis", "Select", __("Quantity Basis"), s.qty_basis, null,
			"Actual Qty\nActual Qty - Reserved Qty\nProjected Qty");
		this.add_control(stock, "safety_stock", "Float", __("Safety Stock (subtracted per item)"), s.safety_stock);

		const price = section(__("Price"));
		this.add_control(price, "price_list", "Link", __("Selling Price List"), s.price_list, null, "Price List");
		this.add_control(price, "erp_price_includes_vat", "Check", __("ERP price includes VAT"), s.erp_price_includes_vat);
		this.add_control(price, "vat_rate", "Float", __("VAT Rate (%)"), s.vat_rate);
		this.add_control(price, "price_rounding", "Int", __("Price Rounding (decimals)"), s.price_rounding);
		this.add_control(price, "max_price_change_pct", "Float", __("Max Price Change % (else held)"), s.max_price_change_pct);
		this.add_control(price, "never_send_zero_price", "Check", __("Never send zero / missing price"), s.never_send_zero_price);

		const cr = section(__("Create Products"));
		this.add_control(cr, "new_product_category_id", "Int", __("New Product Category ID (site)"), s.new_product_category_id);
		this.add_control(cr, "new_product_tax_id", "Int", __("New Product Tax ID"), s.new_product_tax_id);
		this.add_control(cr, "new_product_name_field", "Data", __("Name Field"), s.new_product_name_field);

		const match = section(__("Matching & Safety"));
		this.add_control(match, "sku_field", "Data", __("SKU Field (Item)"), s.sku_field);
		this.add_control(match, "update_disabled_products", "Check", __("Also update products disabled on site"), s.update_disabled_products);
		this.add_control(match, "max_zero_outs", "Int", __("Max Zero-Outs (abort guard)"), s.max_zero_outs);
		this.add_control(match, "max_updates_per_run", "Int", __("Max Updates per Run (abort guard)"), s.max_updates_per_run);

		const filt = section(__("Filters"));
		this.add_control(filt, "item_filter_type", "Select", __("Item Filter"), s.item_filter_type, null, "Exclude\nInclude Only");
		this.add_control(filt, "items", "MultiSelectList", __("Items"), s.items, null, "Item");
		this.add_control(filt, "brand_filter_type", "Select", __("Brand Filter"), s.brand_filter_type, null, "Exclude\nInclude Only");
		this.add_control(filt, "brands", "MultiSelectList", __("Brands"), s.brands, null, "Brand");
		this.add_control(filt, "item_group_filter_type", "Select", __("Item Group Filter"), s.item_group_filter_type, null, "Exclude\nInclude Only");
		this.add_control(filt, "item_groups", "MultiSelectList", __("Item Groups"), s.item_groups, null, "Item Group");
		this.add_control(filt, "skip_disabled_items", "Check", __("Skip disabled items (ignored for Disable sync)"), s.skip_disabled_items);

		$c.find('[data-act="save"]').on("click", () => this.do_save());
	}

	add_control($parent, fieldname, fieldtype, label, value, placeholder, options) {
		const $wrap = $(`<div class="cs-field"></div>`).appendTo($parent);
		const df = { fieldname, fieldtype, label, placeholder };
		if (fieldtype === "Select") df.options = options;
		if (fieldtype === "Link") df.options = options;
		if (fieldtype === "MultiSelectList") {
			df.get_data = (txt) => frappe.db.get_link_options(options, txt);
		}
		const ctrl = frappe.ui.form.make_control({ df, parent: $wrap.get(0), render_input: true });
		if (fieldtype === "MultiSelectList") {
			ctrl.set_value((value || []).map((v) => ({ value: v, label: v, description: "" })).map((o) => o.value));
			ctrl.set_data((value || []).map((v) => ({ value: v, label: v })));
		} else {
			ctrl.set_value(value != null ? value : "");
		}
		this.fields[fieldname] = ctrl;
		return ctrl;
	}

	collect_settings() {
		const out = {};
		Object.keys(this.fields).forEach((fn) => {
			const ctrl = this.fields[fn];
			let v = ctrl.get_value();
			if (ctrl.df.fieldtype === "Check") v = v ? 1 : 0;
			out[fn] = v;
		});
		if (!out.api_key) delete out.api_key; // blank = keep existing
		return out;
	}

	do_save() {
		const payload = this.collect_settings();
		this.call("save_settings", { payload: JSON.stringify(payload) }, __("Saving...")).then((m) => {
			this.state = m;
			frappe.show_alert({ message: __("Settings saved"), indicator: "green" });
			this.render();
		});
	}

	// ===================================================================
	// PREVIEW
	// ===================================================================
	render_preview($c) {
		$c.html(`
			<div class="cs-toolbar">
				<label class="cs-check"><input type="checkbox" id="cs-onlydiff" checked> ${__("Only differences")}</label>
				<input type="text" id="cs-search" class="cs-input" placeholder="${__("Search SKU or Item...")}">
				<button class="cs-btn cs-btn-ghost" data-act="run"><i class="fa fa-play"></i> ${__("Run Preview")}</button>
				<span class="cs-preview-counts"></span>
			</div>
			<div class="cs-table-wrap"><div class="cs-hint">${__("Press Run Preview to compute what the next sync would do.")}</div></div>
		`);
		$c.find('[data-act="run"]').on("click", () => this.run_preview($c));
		$c.find("#cs-search").on("keydown", (e) => { if (e.key === "Enter") this.run_preview($c); });
	}

	run_preview($c) {
		const only_diff = $c.find("#cs-onlydiff").is(":checked") ? 1 : 0;
		const search = $c.find("#cs-search").val();
		this.call("get_preview", { only_diff, search, limit: 500 }, __("Computing preview...")).then((m) => {
			const c = m.counts;
			$c.find(".cs-preview-counts").html(
				`${c.eligible_items} ${__("eligible")} · ${c.map_rows} ${__("mapped")} · <b class="cs-amber">${c.erp_only}</b> ${__("ERP-only")} · <b class="cs-amber">${c.site_only}</b> ${__("site-only")}`
			);
			const rows = m.rows.map((r) => `
				<tr class="${r.matched ? "" : "cs-row-unmatched"}">
					<td>${frappe.utils.escape_html(r.sku || "")}</td>
					<td>${r.item_code ? frappe.utils.escape_html(r.item_code) : `<span class="cs-amber">${__("unmatched")}</span>`}</td>
					<td class="cs-mut">${frappe.utils.escape_html((r.product_name || "").slice(0, 40))}</td>
					<td><span class="cs-badge cs-st-${r.site_status}">${r.site_status || "?"}</span></td>
					<td class="cs-num">${r.erp_qty != null ? r.erp_qty : "—"}</td>
					<td class="cs-num">${r.site_qty}</td>
					<td class="cs-num ${r.qty_diff ? "cs-diff" : ""}">${r.qty_diff != null ? r.qty_diff : "—"}</td>
					<td class="cs-num">${r.erp_price != null ? r.erp_price : "—"}</td>
					<td class="cs-num">${r.site_price}</td>
					<td class="cs-num ${r.price_diff_pct ? "cs-diff" : ""}">${r.price_diff_pct != null ? r.price_diff_pct + "%" : "—"}</td>
					<td class="cs-action">${frappe.utils.escape_html(r.action)}</td>
				</tr>`).join("");
			$c.find(".cs-table-wrap").html(`
				<table class="cs-table">
					<thead><tr>
						<th>${__("SKU")}</th><th>${__("Item")}</th><th>${__("Name")}</th><th>${__("Site")}</th>
						<th>${__("ERP Qty")}</th><th>${__("Site Qty")}</th><th>${__("ΔQty")}</th>
						<th>${__("ERP Price")}</th><th>${__("Site Price")}</th><th>${__("ΔPrice")}</th><th>${__("Action")}</th>
					</tr></thead>
					<tbody>${rows || `<tr><td colspan="11" class="cs-hint">${__("No rows.")}</td></tr>`}</tbody>
				</table>
			`);
		});
	}

	// ===================================================================
	// LOGS
	// ===================================================================
	render_logs($c) {
		this.call("get_logs", { limit: 40 }).then((logs) => {
			const rows = (logs || []).map((l) => `
				<tr data-log="${l.name}">
					<td>${frappe.utils.escape_html(l.name)}</td>
					<td>${l.sync_type || ""}</td>
					<td><span class="cs-badge cs-log-${(l.status || "").replace(/\\s/g, "")}">${l.status || ""}</span></td>
					<td class="cs-mut">${this.fmt_dt(l.started_on)}</td>
					<td class="cs-num">${l.duration_sec || 0}s</td>
					<td class="cs-num">${l.stock_changes || 0}</td>
					<td class="cs-num">${l.price_changes || 0}</td>
					<td class="cs-num">${l.price_held || 0}</td>
					<td class="cs-num">${l.created || 0}</td>
					<td class="cs-num ${l.failed ? "cs-diff" : ""}">${l.failed || 0}</td>
				</tr>`).join("");
			$c.html(`
				<div class="cs-table-wrap"><table class="cs-table cs-table-click">
					<thead><tr>
						<th>${__("Log")}</th><th>${__("Type")}</th><th>${__("Status")}</th><th>${__("Started")}</th><th>${__("Time")}</th>
						<th>${__("Stock")}</th><th>${__("Price")}</th><th>${__("Held")}</th><th>${__("Created")}</th><th>${__("Failed")}</th>
					</tr></thead>
					<tbody>${rows || `<tr><td colspan="10" class="cs-hint">${__("No runs yet.")}</td></tr>`}</tbody>
				</table></div>
			`);
			$c.find("tr[data-log]").on("click", (e) => this.show_log($(e.currentTarget).data("log")));
		});
	}

	show_log(name) {
		this.call("get_log_detail", { name }).then((d) => {
			const dlg = new frappe.ui.Dialog({ title: name, size: "large" });
			$(dlg.body).html(`
				<div class="cs-log-detail">
					<div class="cs-log-meta">${d.sync_type} · <b>${d.status}</b> · ${this.fmt_dt(d.started_on)} → ${this.fmt_dt(d.finished_on)}</div>
					${d.errors ? `<div class="cs-sec-h">${__("Errors")}</div><pre class="cs-pre cs-pre-err">${frappe.utils.escape_html(d.errors)}</pre>` : ""}
					<div class="cs-sec-h">${__("Planned / Applied changes")}</div>
					<pre class="cs-pre">${frappe.utils.escape_html(d.changes || "—")}</pre>
				</div>
			`);
			dlg.show();
		});
	}

	// ===================================================================
	// PRICE HOLDS
	// ===================================================================
	render_holds($c) {
		this.call("get_price_holds", {}).then((holds) => {
			const rows = (holds || []).map((h) => `
				<tr data-hold="${h.name}">
					<td>${frappe.utils.escape_html(h.item_code)}</td>
					<td>${frappe.utils.escape_html(h.product_id || "")}</td>
					<td class="cs-num">${h.old_price}</td>
					<td class="cs-num">${h.new_price}</td>
					<td class="cs-num cs-diff">${h.change_pct}%</td>
					<td><span class="cs-badge cs-hold-${h.status}">${h.status}</span></td>
					<td class="cs-hold-actions">
						${h.status !== "Approved" ? `<button class="cs-btn cs-btn-mini cs-ok" data-set="Approved">${__("Approve")}</button>` : ""}
						${h.status !== "Rejected" ? `<button class="cs-btn cs-btn-mini cs-no" data-set="Rejected">${__("Reject")}</button>` : ""}
					</td>
				</tr>`).join("");
			$c.html(`
				<div class="cs-table-wrap"><table class="cs-table">
					<thead><tr>
						<th>${__("Item")}</th><th>${__("Product ID")}</th><th>${__("Old")}</th><th>${__("New")}</th>
						<th>${__("Change")}</th><th>${__("Status")}</th><th>${__("Action")}</th>
					</tr></thead>
					<tbody>${rows || `<tr><td colspan="7" class="cs-hint">${__("No price holds.")}</td></tr>`}</tbody>
				</table></div>
			`);
			$c.find("button[data-set]").on("click", (e) => {
				const $btn = $(e.currentTarget);
				const name = $btn.closest("tr").data("hold");
				this.call("set_price_hold_status", { name, status: $btn.data("set") }).then(() => {
					frappe.show_alert({ message: __("Updated"), indicator: "green" });
					this.render_holds($c);
				});
			});
		});
	}

	// ===================================================================
	// ACTIONS
	// ===================================================================
	do_test() {
		this.call("test_connection", {}, __("Contacting CS-Cart...")).then((m) => {
			frappe.msgprint({ title: __("Connected"), indicator: "green",
				message: __("Connected. {0} products on the store.", [m.total_products]) });
		});
	}

	do_refresh() {
		this.call("refresh_map", {}, __("Queuing...")).then(() => {
			frappe.show_alert({ message: __("Map refresh queued. Check Logs shortly."), indicator: "blue" });
			setTimeout(() => this.load_state(), 3000);
		});
	}

	do_sync(full) {
		const s = this.state.settings;
		const msg = s.dry_run
			? __("Dry run: nothing will be written to the store. Continue?")
			: __("LIVE: this will write to the CS-Cart store. Continue?");
		frappe.confirm(msg, () => {
			this.call("sync_now", { full }, __("Queuing...")).then((m) => {
				frappe.show_alert({ message: __("{0} sync queued.", [m.sync_type]), indicator: "blue" });
				setTimeout(() => this.load_state(), 3000);
			});
		});
	}

	// -------------------------------------------------------------------
	fmt_dt(v) {
		if (!v) return "—";
		return frappe.datetime.str_to_user(v);
	}

	// ===================================================================
	inject_styles() {
		if (document.getElementById("cs-cart-styles")) return;
		const css = `
:root {
	--cs-bg: #f1f5f9; --cs-surface: #ffffff; --cs-ink: #0f172a; --cs-ink-2: #334155; --cs-muted: #94a3b8;
	--cs-line: #e2e8f0; --cs-side: #0f172a; --cs-side-2: #1e293b; --cs-accent: #2563eb; --cs-accent-soft: #dbeafe;
	--cs-ok: #22c55e; --cs-amber: #f59e0b; --cs-red: #ef4444; --cs-slate: #64748b;
}
.cs-root { display:flex; gap:0; font-family:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif; color:var(--cs-ink); min-height:78vh; background:var(--cs-bg); border-radius:14px; overflow:hidden; border:1px solid var(--cs-line); }
.cs-side { width:210px; flex-shrink:0; background:var(--cs-side); color:#cbd5e1; display:flex; flex-direction:column; padding:18px 12px; }
.cs-brand { display:flex; align-items:center; gap:10px; color:#fff; font-weight:700; font-size:1.05rem; padding:4px 8px 18px; }
.cs-brand i { font-size:1.4rem; color:#60a5fa; }
.cs-brand small { color:#64748b; font-weight:500; font-size:0.68rem; }
.cs-nav { display:flex; flex-direction:column; gap:2px; flex:1; }
.cs-nav-link { display:flex; align-items:center; gap:10px; padding:0.6rem 0.8rem; border-radius:8px; color:#cbd5e1; font-size:0.86rem; font-weight:500; cursor:pointer; transition:all .15s; }
.cs-nav-link:hover { background:var(--cs-side-2); color:#fff; text-decoration:none; }
.cs-nav-link.active { background:var(--cs-accent); color:#fff; }
.cs-nav-link i { width:18px; text-align:center; }
.cs-side-foot { display:flex; align-items:center; gap:8px; font-size:0.72rem; color:#94a3b8; padding:12px 8px 2px; border-top:1px solid #1e293b; }
.cs-dot { width:9px; height:9px; border-radius:50%; background:var(--cs-muted); }
.cs-main { flex:1; display:flex; flex-direction:column; min-width:0; }
.cs-topbar { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px 20px; background:var(--cs-surface); border-bottom:1px solid var(--cs-line); flex-wrap:wrap; }
.cs-pills { display:flex; gap:6px; flex-wrap:wrap; }
.cs-pill { font-size:0.68rem; font-weight:600; padding:3px 9px; border-radius:20px; letter-spacing:.02em; }
.cs-pill.on { background:#dcfce7; color:#15803d; } .cs-pill.off { background:#f1f5f9; color:#64748b; }
.cs-pill.live { background:#fee2e2; color:#b91c1c; } .cs-pill.warn { background:#fef3c7; color:#b45309; }
.cs-pill.run { background:#dbeafe; color:#1d4ed8; }
.cs-actions { display:flex; gap:8px; flex-wrap:wrap; }
.cs-btn { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--cs-line); background:var(--cs-surface); color:var(--cs-ink-2); font-size:0.8rem; font-weight:600; padding:0.5rem 0.85rem; border-radius:8px; cursor:pointer; transition:all .15s; }
.cs-btn:hover { border-color:var(--cs-accent); color:var(--cs-accent); }
.cs-btn-primary { background:var(--cs-accent); color:#fff; border-color:var(--cs-accent); }
.cs-btn-primary:hover { background:#1d4ed8; color:#fff; }
.cs-btn-ghost { background:var(--cs-surface); }
.cs-btn-mini { padding:0.25rem 0.6rem; font-size:0.72rem; }
.cs-btn.cs-ok { color:#15803d; border-color:#bbf7d0; } .cs-btn.cs-ok:hover { background:#dcfce7; }
.cs-btn.cs-no { color:#b91c1c; border-color:#fecaca; } .cs-btn.cs-no:hover { background:#fee2e2; }
.cs-content { padding:20px; overflow:auto; flex:1; }
.cs-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:14px; margin-bottom:18px; }
.cs-card { display:flex; align-items:center; gap:14px; background:var(--cs-surface); border:1px solid var(--cs-line); border-radius:12px; padding:16px; }
.cs-card-ic { width:44px; height:44px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:1.15rem; background:#f1f5f9; color:#64748b; }
.cs-card-ic.blue { background:#dbeafe; color:#2563eb; } .cs-card-ic.green { background:#dcfce7; color:#16a34a; }
.cs-card-ic.amber { background:#fef3c7; color:#d97706; } .cs-card-ic.red { background:#fee2e2; color:#dc2626; }
.cs-card-ic.slate { background:#e2e8f0; color:#475569; }
.cs-card-val { font-size:1.35rem; font-weight:700; color:var(--cs-ink); line-height:1.1; }
.cs-card-lbl { font-size:0.74rem; color:var(--cs-muted); font-weight:500; }
.cs-panel { background:var(--cs-surface); border:1px solid var(--cs-line); border-radius:12px; padding:18px; }
.cs-panel-h { font-weight:700; margin-bottom:10px; color:var(--cs-ink); }
.cs-steps { margin:0; padding-left:20px; color:var(--cs-ink-2); font-size:0.85rem; line-height:1.9; }
.cs-form { display:flex; flex-direction:column; gap:14px; }
.cs-sec { background:var(--cs-surface); border:1px solid var(--cs-line); border-radius:12px; padding:16px 18px; }
.cs-sec-h { font-weight:700; font-size:0.82rem; text-transform:uppercase; letter-spacing:.04em; color:var(--cs-slate); margin-bottom:12px; }
.cs-sec-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px 20px; }
.cs-field .frappe-control { margin-bottom:0; }
.cs-field .control-label { font-size:0.78rem !important; color:var(--cs-ink-2) !important; }
.cs-form-foot { position:sticky; bottom:0; padding:14px 0 2px; display:flex; justify-content:flex-end; }
.cs-toolbar { display:flex; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap; }
.cs-check { font-size:0.82rem; color:var(--cs-ink-2); display:flex; align-items:center; gap:6px; margin:0; }
.cs-input { border:1px solid var(--cs-line); border-radius:8px; padding:0.45rem 0.7rem; font-size:0.82rem; min-width:220px; }
.cs-preview-counts { font-size:0.8rem; color:var(--cs-ink-2); margin-left:auto; }
.cs-amber { color:var(--cs-amber); } .cs-diff { color:var(--cs-accent); font-weight:700; }
.cs-table-wrap { background:var(--cs-surface); border:1px solid var(--cs-line); border-radius:12px; overflow:auto; }
.cs-table { width:100%; border-collapse:collapse; font-size:0.8rem; }
.cs-table th { text-align:left; padding:10px 12px; background:#f8fafc; color:var(--cs-slate); font-weight:600; font-size:0.72rem; text-transform:uppercase; letter-spacing:.03em; border-bottom:1px solid var(--cs-line); position:sticky; top:0; }
.cs-table td { padding:9px 12px; border-bottom:1px solid #f1f5f9; color:var(--cs-ink-2); white-space:nowrap; }
.cs-table tbody tr:hover { background:#f8fafc; }
.cs-table-click tbody tr { cursor:pointer; }
.cs-num { text-align:right; font-variant-numeric:tabular-nums; }
.cs-mut { color:var(--cs-muted); }
.cs-row-unmatched { background:#fffbeb; }
.cs-action { color:var(--cs-accent); font-weight:600; }
.cs-badge { font-size:0.68rem; font-weight:700; padding:2px 7px; border-radius:5px; background:#f1f5f9; color:#475569; }
.cs-st-A,.cs-hold-Approved,.cs-log-Success { background:#dcfce7; color:#15803d; }
.cs-st-D,.cs-hold-Rejected,.cs-log-Failed,.cs-log-Aborted { background:#fee2e2; color:#b91c1c; }
.cs-st-H,.cs-hold-Pending,.cs-log-DryRun { background:#fef3c7; color:#b45309; }
.cs-log-Partial { background:#fef3c7; color:#b45309; } .cs-log-Running { background:#dbeafe; color:#1d4ed8; }
.cs-hint { padding:24px; text-align:center; color:var(--cs-muted); font-size:0.85rem; }
.cs-pre { background:#0f172a; color:#e2e8f0; padding:14px; border-radius:8px; font-size:0.75rem; max-height:360px; overflow:auto; white-space:pre-wrap; }
.cs-pre-err { background:#7f1d1d; }
.cs-log-meta { color:var(--cs-slate); font-size:0.82rem; margin-bottom:12px; }
@media (max-width: 768px) { .cs-side { width:64px; } .cs-brand span, .cs-nav-link span, .cs-conn-label { display:none; } .cs-nav-link { justify-content:center; } }
`;
		const el = document.createElement("style");
		el.id = "cs-cart-styles";
		el.textContent = css;
		document.head.appendChild(el);
	}
}
