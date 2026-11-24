# Titan Safety stock-feed integration

Titan Safety can only email their daily stock file, so we glue together
Zoho Mail forwarding, a dedicated Gmail inbox, and the existing SFTP
pattern we use for other suppliers.

## Flow

```
Titan email -> stockfeed@wbys.co.uk (Zoho alias on Katie's mailbox)
             -> Zoho filter rule forwards to wbysstockfeed@gmail.com
             -> ops_engine IMAP poller downloads attachment (cron every 15 min)
             -> SFTP drop at /home/wbysltd/SupplierStockFeeds/TitanSafety/
             -> stock sync (cron every 15 min, offset) picks up newest xlsx
             -> Brightpearl warehouse 15 ("Titan Safety") updated
```

## What Titan's feed looks like

- Format: one-sheet XLSX
- Filename varies (Titan generate it)
- Columns:

| Product Code | Product Description | Available Qty |
| ------------ | ------------------- | ------------- |
| `TC01-03`    | Quest Size 03       | 35            |
| `TC01-10`    | Quest Size 10       | 237           |

### SKU normalisation

Titan ship SKUs with a leading zero on the size (`TC01-03`); Brightpearl
stores them without it (`TC01-3`). The parser strips the leading zero
from the last `-NN` segment via `int()`. `TC01-10` stays `TC01-10`
since there's no leading zero to drop.

## Config

See `.env.example` under `--- Titan Safety integration config ---` and
`--- Titan Safety Gmail IMAP ---`. Gmail credentials use a **Google App
Password** (16 chars) — never the account login password. 2SV must be
enabled on the Gmail account first.

## Commands

```bash
# One-off: create the SFTP folder.
python -m ops_engine.integrations.titan_safety setup-folders

# One-off: find the location ID inside warehouse 15.
python -m ops_engine.integrations.titan_safety list-locations

# Pull xlsx attachments from Gmail -> SFTP (runs via cron every 15 min).
python -m ops_engine.integrations.titan_safety poll-inbox

# Apply the newest xlsx on SFTP to Brightpearl (runs via cron).
python -m ops_engine.integrations.titan_safety sync

# Debug: list a folder on SFTP.
python -m ops_engine.integrations.titan_safety list-sftp /home/wbysltd/SupplierStockFeeds/TitanSafety/

# Debug: hit an arbitrary Brightpearl endpoint.
python -m ops_engine.integrations.titan_safety dump-path /warehouse-service/warehouse/15/stock
```

## Operational notes

- **IMAP rate limits**: Gmail's generous for this volume — one message/day.
  No sleep between operations needed inside the poller.
- **Rate limit on Brightpearl**: shared with the Bobcat module (~200/min).
  The SKU search inherits the same 0.35s per-call throttle.
- **Zero-out-on-absence**: intentionally not implemented. Titan's feed
  carries every SKU, so there's nothing to zero out; the Brightpearl
  endpoint to enumerate warehouse contents isn't exposed on this account
  anyway. Same story as Bobcat.
- **Stock-correction cost**: Titan's feed doesn't carry per-unit cost so
  we send `0.00 GBP`. Brightpearl requires _some_ value even on downward
  corrections. If we ever need accurate cost we can either source it
  from BP itself or ask Titan to add a cost column.

## Follow-ups (not blocking the first live run)

- **Share Brightpearl queries** between Bobcat and Titan by moving them
  into `ops_engine.core.brightpearl_stock`. We've deliberately duplicated
  for now (rule of three — refactor once a third supplier needs the same
  surface).
- **Cost column** from Titan — would enable accurate stock-correction
  value and cost sync in BP.
