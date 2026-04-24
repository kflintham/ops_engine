# Field mapping: Brightpearl → Gardiners JIT order CSV

This is the contract that the outbound order CSV generator has to satisfy.
For every field Gardiners expect in the order file, this table says where it
comes from in Brightpearl (or that we're hard-coding it, or that we're
leaving it blank).

**Read this before writing any code.** Every row marked "TBD" or "Confirm" is
a real question that has to be answered by WBYS operations before we can
build.

## Legend

- **GB requirement** — taken verbatim from
  [`process-docs/order-file-requirements.pdf`](process-docs/order-file-requirements.pdf).
- **Source** — where the value comes from when we build the CSV row.
  - `BP:<resource>.<field>` → Brightpearl API resource + field.
  - `config` → a static value from the integration's config file.
  - `derived` → calculated from other fields.
  - `blank` → intentionally empty.
- **Brightpearl API reference** — the Brightpearl endpoint we'd fetch this
  from. These need to be verified against the live account's data; treat them
  as our working assumption.

## Order-level fields (one per Brightpearl order)

| GB CSV column | GB requirement | Source | Brightpearl API reference | Notes / open questions |
| --- | --- | --- | --- | --- |
| `Order Reference` | **Required** — text or numeric | `BP:order.reference` (preferred) or `BP:order.id` | `GET /order-service/order/{id}` → `reference` or `id` | Must be unique per order and **stable** — we need to match it back in notifications (`Customer Header Reference`). Need to confirm whether Brightpearl's sales-order `reference` is always populated for JIT orders; if not, fall back to the numeric order ID. |
| `CustomerID` | Not required | `config.customer_id` | n/a | This is our Gardiners account number, not a Brightpearl customer. One value per integration; set once in config. **Ask Usamah for the account number.** |
| `Name` | Not required | `config.delivery_address.name` | n/a | For JIT we always ship to the WBYS warehouse. Hard-code in config. |
| `Address Line 1` | Not required | `config.delivery_address.line1` | n/a | Hard-code. |
| `Address Line 2` | Not required | `config.delivery_address.line2` | n/a | Hard-code. |
| `Address Line 3` | Not required | `config.delivery_address.line3` | n/a | Hard-code. |
| `Address Line 4` | Not required | `config.delivery_address.line4` | n/a | Hard-code. |
| `Postcode` | Not required | `config.delivery_address.postcode` | n/a | Hard-code. |
| `Country` | Not required — ISO alpha-2 | `config.delivery_address.country` | n/a | `GB`. |
| `Delivery Service Code` | Not required — Gardiners' own codes | `config.delivery_service_code` | n/a | **Ask Sam for the correct JIT service code**, then pin it in config. |
| `Contact Name` | Not required | `config.contact.name` | n/a | WBYS goods-in contact. |
| `Contact Phone/SMS` | Not required | `config.contact.phone` | n/a | WBYS goods-in phone. |
| `Contact Email` | Not required | `config.contact.email` | n/a | WBYS goods-in email (likely a monitored inbox, not an individual). |
| `PDF File Name` | Not required | `blank` | n/a | Only used when we also SFTP a despatch-note PDF. Not in scope for v1. |

## Line-level fields (one row per Brightpearl order line)

| GB CSV column | GB requirement | Source | Brightpearl API reference | Notes / open questions |
| --- | --- | --- | --- | --- |
| `SKU` | **One of `SKU` or `Barcode` required; SKU preferred** | `BP:product.SKU` *(the Gardiners SKU)* | `GET /product-service/product/{productId}` → `SKU` **or** the supplier product code custom field | **Needs investigation.** Brightpearl's `product.SKU` may be our internal SKU, not Gardiners'. The Gardiners SKU is usually stored as a supplier product code on the product's supplier linkage, or as a custom field. We must resolve **which Brightpearl field holds the Gardiners SKU** before we can generate the file. |
| `Barcode` | Fallback if SKU unavailable | `BP:product.barcode` | `GET /product-service/product/{productId}` → `barcode` | Only used if we can't get the Gardiners SKU. Gardiners' product data CSV shows these are standard UPC/EAN. |
| `Customer SKU` | Not required | `blank` | n/a | "Used for bespoke integrations" — not ours. |
| `Quantity` | **Required — integer** | `BP:orderRow.quantity` | `GET /order-service/order/{id}` → `orderRows[].productQuantity.magnitude` | Must be integer. If Brightpearl ever allows fractional quantities on these products, we'll need a guard. |
| `Order Line Reference` | **Preferred** — text or numeric | `derived: f"{BP:order.id}-{BP:orderRow.id}"` (provisional) | `GET /order-service/order/{id}` → `orderRows[].id` | Must be **unique across all lines ever sent** and **stable** — we match on it in notifications (`Customer Line Reference`). Using `{orderId}-{rowId}` is unique, human-readable, and survives restatement. Confirm with Sam that hyphen is allowed. |
| `SoldAt` | Not required — GBP single-unit price | `blank` (v1) or `BP:orderRow.rowValue.taxExclusive / quantity` | `GET /order-service/order/{id}` → `orderRows[].rowValue` | **Probably leave blank for JIT.** "SoldAt" is the price the end-customer paid — useful for dropship, less obvious for JIT (where Gardiners invoice us at trade price anyway). Confirm with Sam whether it's useful to populate for JIT or if blank is fine. |

## Hard-coded config fields (one-time setup)

These live in the integration's config and never come from Brightpearl:

- `customer_id` — Gardiners account number for WBYS. **Ask Usamah.**
- `delivery_address.*` — WBYS warehouse address. **Ask Operations for the
  exact delivery address to use.**
- `delivery_service_code` — Gardiners code for the JIT delivery service.
  **Ask Sam.**
- `contact.*` — WBYS goods-in contact details.
- `sftp.host` = `ec2-63-32-88-8.eu-west-1.compute.amazonaws.com`
- `sftp.orders_path` = `/JIT/Orders/` *(to confirm with Sam)*
- `sftp.notifications_path` = `/JIT/Notifications/` *(to confirm with Sam)*
- `sftp.stock_path` = `/JIT/Stock/` *(to confirm with Sam)*
- `file_name_template` — e.g. `{order_reference}-{yyyymmddHHMM}.csv`.

## Inbound field mapping (notifications → Brightpearl)

For completeness — this is how Flow B (notifications) maps back into
Brightpearl. Not yet fleshed out; will be done as part of Stage 6.

| GB CSV column | Used for | Brightpearl target |
| --- | --- | --- |
| `Customer Header Reference` | Locating the order | lookup into `BP:order` by the `Order Reference` we sent in Flow A |
| `Customer Line Reference` | Locating the order line | lookup into `BP:orderRow.id` (parsed out of our `{orderId}-{rowId}` format) |
| `Current Status` → `Received` | Status update | **TBD** — probably "Acknowledged by supplier" custom status |
| `Current Status` → `Cancelled` | Status update + trigger reorder flow | **TBD** — probably cancel the PO / flag for re-sourcing |
| `Current Status` → `Despatched` | Status update | **TBD** — probably "Shipped by supplier"; warehouse receives the goods next day |
| `Carrier` + `Consignment Reference` + `Consignment Tracking Url` | Reference data | **TBD** — stored on the order as a note, custom field, or Brightpearl shipment? |
| `Sku`, `Description`, `Colour`, `Size`, `Quantity` | Verification only | Compared against what we sent; no write-back |

## Stock feed — out of scope

Gardiners' stock feed is already handled by a separate existing WBYS feed.
This integration does not consume it. See
[`README.md`](README.md#flow-c--stock-feed-in-gardiners--wbys--out-of-scope).

## What to verify before writing code

The following assumptions in this document must be validated against the live
Brightpearl account and a real test order **before** we start Stage 3 (core
helpers):

1. Which Brightpearl field holds the Gardiners SKU (not our internal SKU).
2. Whether `order.reference` is always set, or whether we need to fall back
   to `order.id`.
3. Whether `orderRows[].id` is a stable unique identifier across the life of
   the order (it should be, but verify).
4. Whether JIT orders can be identified in Brightpearl by status alone, or
   whether they also need a supplier / warehouse / custom-field filter.
5. Whether Brightpearl issues a webhook on the relevant status change, or
   whether we have to poll.
