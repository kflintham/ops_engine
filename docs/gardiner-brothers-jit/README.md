# Gardiner Brothers — Wholesale / JIT integration

**Partner:** Gardiner Bros & Footsure
**Contacts:** Sam Murray-Matthews (sam.murray-matthews@gardinerbros.co.uk),
Mark Pownall (mark.pownall@gardinerbros.co.uk)
**Replaces:** the daily email that Usamah (Purchasing) currently sends to
Gardiners listing JIT items to order.
**Why:** the email process is unreliable. Gardiners already handle our DF
(dropship) orders via SFTP via BSITC — we're doing the same for JIT but
building it ourselves so we own it and can extend it.

## What "JIT" means here

- **Dropship (DF):** Gardiners ship direct to the end customer from their
  warehouse. We never touch the stock.
- **Just-in-time (JIT / wholesale):** Gardiners ship to **our** WBYS warehouse
  next day. We then dispatch to the customer ourselves.

Same supplier, same SFTP server, but a **different folder tree and different
file format** from DF. This folder documents the JIT format only.

## The file flows

Two flows are in scope for this integration. Both run over SFTP. The SFTP
server is owned by WBYS (not BSITC):
`sftp://ec2-63-32-88-8.eu-west-1.compute.amazonaws.com`.

```
     WBYS (us)                    SFTP server                 Gardiners
     ─────────                    ───────────                 ─────────
  Brightpearl order ──┐
                      ├── CSV ──▶  /JIT/Orders/      ──▶  picks up every 15 min
                      │                                       │
                      │                                       ▼
                      │                                   warehouse processes
                      │                                       │
  Brightpearl  ◀──────┤                                       │
  order status        │                                       ▼
  updated      ◀─ CSV ─── /JIT/Notifications/  ◀── drops file per status change
```

A third flow (supplier stock feed) is offered by Gardiners but is **out of
scope** for this integration — WBYS already gets Gardiners stock levels
through a separate existing feed. See Flow C below.

Folder paths are provisional — final structure to be agreed with Sam.

### Flow A — Orders out (WBYS ▶ Gardiners)

- **We write:** one CSV per order (or one CSV with multiple orders in — to be
  confirmed with Sam).
- **Location:** `/JIT/Orders/` on our SFTP.
- **Cut-off:** 10:00 AM UK for next-working-day delivery to our warehouse.
- **Pickup cadence:** Gardiners poll every 15 minutes.
- **Format:** see [`process-docs/order-file-requirements.pdf`](process-docs/order-file-requirements.pdf)
  and [`process-docs/order-processing.pdf`](process-docs/order-processing.pdf).
- **Sample:** [`samples/order-file-template.csv`](samples/order-file-template.csv).
- **Required fields:** `SKU` (or `Barcode`), `Quantity`, `Order Reference`.
  Strongly preferred: `Order Line Reference` — without it, Gardiners generate
  their own and we lose the link back to our Brightpearl order lines.
- **Field-level mapping Brightpearl ↔ CSV:** see
  [`field-mapping.md`](field-mapping.md).

### Flow B — Notifications in (Gardiners ▶ WBYS)

- **They write:** one CSV per order per status change.
- **Location:** `/JIT/Notifications/` on our SFTP.
- **Format:** see [`process-docs/notifications-processing.pdf`](process-docs/notifications-processing.pdf).
- **Samples:** [`samples/order-notification-received.csv`](samples/order-notification-received.csv),
  [`samples/order-notification-despatched.csv`](samples/order-notification-despatched.csv).
- **Status codes we care about** for JIT:
  - `RCS` / `RCD` — Received (Gardiners have accepted the order)
  - `CAN` / `COS` / `CBP` — Cancelled (we need to notify customer / reorder)
  - `DES` — Despatched (carrier + tracking URL arrives with this event;
    triggers our own fulfilment workflow once it lands at our warehouse)
- **Status codes we probably ignore for JIT:** `PIK` (picking), `PAK` (packed)
  — these are Gardiners' internal warehouse states and don't affect us.
- **Key fields for matching back to Brightpearl:**
  - `Customer Header Reference` = our `Order Reference` (= Brightpearl order ID
    or reference we sent in Flow A)
  - `Customer Line Reference` = our `Order Line Reference` (= Brightpearl order
    line ID we sent in Flow A)

### Flow C — Stock feed in (Gardiners ▶ WBYS) — **OUT OF SCOPE**

WBYS already receives Gardiners stock levels via a separate feed. This
integration therefore does **not** handle the Gardiners stock feed. Samples
and the process PDF are retained in this folder for reference only, in case
we consolidate feeds later.

Files kept for reference:
[`samples/stock-feed-sku.csv`](samples/stock-feed-sku.csv),
[`samples/stock-feed-barcode.csv`](samples/stock-feed-barcode.csv),
[`process-docs/stock-feed-processing.pdf`](process-docs/stock-feed-processing.pdf).

### Invoices

Gardiners email invoices separately (see Sam Rose's note in the email chain).
Not part of this SFTP integration. Sample for reference:
[`samples/invoice-sample.pdf`](samples/invoice-sample.pdf).

### Product master data

Gardiners also provided a full product catalogue
([`samples/product-data.csv`](samples/product-data.csv)) for initial seeding
into Brightpearl. This is a **one-off import**, not part of the runtime
integration. Ownership TBD (Purchasing, probably).

## Open questions for Gardiners

Before we build, Usamah / Katie need to confirm with Sam:

1. **SFTP host.** We want to use our own server
   (`ec2-63-32-88-8.eu-west-1.compute.amazonaws.com`), same as DF. Sam asked
   whether JIT should use the same FTP or be hosted by Gardiners — we said
   same FTP. Confirm and get any outbound IPs Gardiners will connect from so
   we can whitelist them.
2. **Folder layout.** Proposed: `/JIT/Orders/`, `/JIT/Notifications/`,
   `/JIT/Stock/` — siblings to the existing DF folders. Confirm with Sam.
3. **Stock feed key.** SKU or Barcode — we want SKU. Confirm.
4. **Delta vs. full stock feed.** Gardiners can optionally provide a delta
   file hourly. We'll start with the 30-minute full feed only; simpler.
5. **Delivery address for JIT orders.** Need the exact WBYS warehouse
   address and Gardiners delivery service code to hard-code into the order
   file.
6. **File naming convention.** Gardiners accept either `<OrderRef>.csv` or
   `<OrderRef>-yyyymmddhhmm.csv`. Pick one and stick to it (suggested:
   timestamp variant, so retries don't collide).
7. **One order per file, or many?** We need to confirm whether Flow A sends
   one file per Brightpearl order, or one daily batch file containing all
   orders. Affects the `Order Reference` strategy.
8. **Error handling.** What does Gardiners do if a file has a bad SKU or
   malformed row? Silent drop, email, or notification file?

## Open questions for WBYS internal

1. ✅ **Brightpearl trigger.** Purchase orders with status
   `GBR JIT - Request Sent` are picked up by the app. See
   [`field-mapping.md`](field-mapping.md#brightpearl-purchase-order-status-workflow)
   for the full status workflow.
2. ✅ **Brightpearl API credentials.** WBYS has its own API app credentials
   (not via BSITC). Runtime values live in `.env.local`; see `.env.example`
   at the repo root.
3. ✅ **How do JIT items get flagged in Brightpearl today?** Resolved:
   Gardiners have two supplier accounts in Brightpearl — `B1358` (JIT) and
   `B3116 DF` (dropship). A product is JIT-eligible iff it lists `B1358`
   among its suppliers; a PO is a JIT PO iff it's raised against `B1358`.
   See the
   [JIT eligibility rule](field-mapping.md#jit-eligibility-rule) for the
   full rule.
4. ✅ **Where does the Gardiners SKU live on a Brightpearl product?**
   Resolved: it's the `SKU` field on the product's entry in the
   `Cost Price GBR (Net)` price list (Prices tab in the UI). This is
   required because some products are sourced from more than one supplier,
   so the top-level `product.SKU` cannot be used. See
   [`field-mapping.md`](field-mapping.md#line-level-fields-one-row-per-brightpearl-order-line)
   for the full rule.
5. **Who monitors failures?** Need an alerting target (email list / Slack
   channel) for when a file can't be sent or a notification can't be applied.
6. **Cosmetic cleanups.** Two to consider before go-live:
   - Fix the typo in the `GBR JIT - Invoice Recieved` status name.
   - Consider renaming `GBR JIT - Order Fulfilled` to
     `GBR JIT - Fully Despatched` to distinguish it from customer-side
     fulfilment.

## Build stages

1. ✅ Documentation committed (this folder).
2. ✅ Field-mapping table drafted (see [`field-mapping.md`](field-mapping.md)).
3. ✅ Outbound order CSV builder + tests.
4. ✅ Inbound notification CSV parser + tests.
5. ✅ Core Brightpearl API client.
6. ⬜ Core SFTP client.
7. ⬜ Probe script: fetch a sample Gardiners PO and product, resolve the
   Gardiners SKU field.
8. ⬜ Wire outbound: fetch Brightpearl PO → build CSV → SFTP upload →
   transition PO to `GBR JIT - Pending`.
9. ⬜ Wire inbound: SFTP poll notifications → parse → transition PO status.
10. ⬜ Automated trigger (poll or webhook) for PO status changes.
11. ⬜ Config-driven generalisation so other integrations can be added.
12. ⬜ Admin UI over configs + audit log.
