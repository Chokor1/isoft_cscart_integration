# Isoft CSCart Integration

One-way **ERPNext → CS-Cart** product integration for ITEC's storefront
(https://itec.co.ao, CS-Cart 4.16 Ultimate). Phase 1 syncs **stock, price and
status**, and can optionally **create** products. Orders/customers are later phases.

Everything runs from ERPNext; nothing is installed on the CS-Cart server.

## Managing it

Open the console at **`/app/isoft-cscart`** (desk page *ISOFT CS-Cart*, or the
*CS-Cart Integration* workspace). The page is the whole UI — the doctypes below
are backend storage only.

Tabs: **Dashboard** (metrics), **Settings** (connection, schedule, what to sync,
stock/price rules, filters), **Preview** (what the next run would do + match
gaps), **Logs**, **Price Holds**.

Toolbar actions: **Test Connection**, **Refresh Map**, **Full Sync**, **Sync Now**.

## Go-live order (keep Dry Run ON until told otherwise)

1. Fill Connection → **Test Connection** (expects "Connected, ~4,930 products").
2. **Refresh Map** → open **Preview**, review the match rate with the business.
3. Turn on **Sync Stock**, dry-run a day, then go live on 2–3 SKUs via the
   *Include Only* item filter, then remove the filter.
4. Turn on **Sync Price** (+ Price Holds), pilot, then all.
5. Optional: **Disable sync**, then **Create products** (into a hidden category).

## Architecture

| File | Role |
|---|---|
| `api.py` | `CSCartClient` — Basic-auth REST client (list/update/create/ping), rejects Cloudflare HTML |
| `erp.py` | eligible items, `Bin` quantities (qty basis + safety stock), `Item Price` selling prices |
| `sync.py` | scheduling, `refresh_product_map`, `build_plan`, execute, guards, logs |
| `console.py` | whitelisted API behind the page (System Manager only) |
| `page/isoft_cscart` | the management console (Invenza-styled) |

### DocTypes (storage)
- **CSCart Integration Settings** (Single) — all configuration
- **CSCart Product Map** — one row per CS-Cart product (keyed by product_id)
- **CSCart Price Hold** — price changes over the threshold, awaiting review
- **CSCart Sync Log** — one row per run (30-day retention)
- Child tables: CSCart Sync Warehouse / Item / Brand / Item Group

### Safety
- **Dry Run** default ON; only `amount`/`price`/`status` are ever sent (partial PUT).
- Guards: `max_zero_outs`, `max_updates_per_run` abort the run with zero writes.
- Circuit breaker after 10 consecutive failures. Redis lock prevents overlap.
- Everything recomputed each run and compared with the map (v13 `Bin` updates
  do not reliably bump `modified`).

## Prerequisites handled by the site owner
1. A dedicated CS-Cart admin user with catalogue-only API access + API key.
2. A **Cloudflare WAF skip rule for `/api/*`** from this ERPNext server's IP
   (the store is Cloudflare-only and challenges automated PUT/POST).
3. Agree the ERPNext **selling price list** (AOA) and whether it includes VAT.
4. (For create) a hidden "Novos do ERP" category; note its ID.

Built from `cscart_erpnext_product_integration_plan.md`.
