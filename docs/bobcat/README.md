# Bobcat stock-feed integration

Bobcat drop one stock CSV per day onto WBYS's own SFTP server. This
integration picks the newest file up, converts Bobcat's `+10 Available`
column into a stock level, and writes the levels into the Virtual
Brightpearl warehouse named **Bobcat** (ID `14` on production).

## What the feed looks like

Filename pattern: `BC_STOCK_YYYYMMDDHHMMSS.csv` (e.g.
`BC_STOCK_20261103080000.csv`).

Dropped into: `/home/wbysltd/SupplierStockFeeds/Bobcat/` on the WBYS
SFTP server (same host, same credentials as the other supplier feeds).

Column we read:

| Column           | Meaning                                       | How we map it           |
| ---------------- | --------------------------------------------- | ----------------------- |
| `+10 Available`  | `"Yes"` means ten or more units on hand       | Set stock to `10`       |
|                  | `"No "` (sometimes trailing space) means none | Set stock to `0`        |
| `SKU`            | Matches `product.identity.sku` in Brightpearl | Used to resolve product |
| `Purchase Price` | GBP cost (Bobcat net)                         | Parsed, not yet applied |

A row whose `+10 Available` is anything other than `Yes` / `No` fails the
whole run (we'd rather stop than mis-sync). An unknown SKU is logged and
skipped -- the next run picks it up if the product is added later.

## Zero-out on absence

A product that Brightpearl shows as having stock in warehouse 14, but
which does NOT appear in today's feed, is zeroed out. This keeps the
warehouse in lock-step with what Bobcat actually have.

## How stock is applied

Brightpearl does not expose a "set absolute level" primitive. We read
the current level, compute `delta = target - current`, and POST a stock
correction (`/warehouse-service/warehouse/14/stock-correction`) when
the delta is non-zero. Zero-delta rows are skipped quietly.

The correction carries the reference `"Bobcat daily stock feed"` so
Brightpearl's audit log shows why each change happened.

## How to run it

```bash
# One-off: create the SFTP folders.
python -m ops_engine.integrations.bobcat setup-folders

# Each run (via cron every 15 min, same cadence as the Gardiners jobs).
python -m ops_engine.integrations.bobcat sync

# Debugging:
python -m ops_engine.integrations.bobcat list-sftp /home/wbysltd/SupplierStockFeeds/Bobcat/
python -m ops_engine.integrations.bobcat dump-path /warehouse-service/warehouse/14/stock
```

## Config

Everything lives in environment variables; see `.env.example` under
`--- Bobcat integration config ---`. The two values you have to set are
`BOBCAT_WAREHOUSE_ID` and `BOBCAT_STOCK_PATH`; everything else falls
back to a sensible default.

## Not yet done

- **Costs sync** -- the parser reads `Purchase Price (GBP)` but the
  orchestrator doesn't push it anywhere. Secondary priority; add once
  the stock half is proven.
- **IP whitelisting for Bobcat uploader** -- if Bobcat upload from their
  own IP rather than via the existing SFTP user, we'll need to add that
  IP to the security group the same way we did for Gardiners.
