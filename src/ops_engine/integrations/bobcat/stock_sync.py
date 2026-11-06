"""Stock-sync pipeline: Bobcat SFTP CSV -> Brightpearl warehouse levels.

Each run:

1. Lists the Bobcat stock folder on SFTP, picks the newest
   ``BC_STOCK_*.csv`` (Bobcat drop one per day, but we tolerate several).
2. Parses it into typed stock lines.
3. Resolves each SKU to a Brightpearl product ID.
4. Reads the warehouse's current stock levels (both for the feed SKUs
   *and* for products Brightpearl thinks are in the warehouse but the
   feed doesn't mention -- those get zeroed out).
5. Computes the delta needed to reach the target per product and POSTs a
   stock correction when the delta is non-zero.
6. Archives the CSV into the processed folder so the next run skips it.

Failures on an individual SKU are logged and counted but don't stop the
rest of the run; a run-wide failure (bad CSV, SFTP down) bails cleanly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from ops_engine.core.brightpearl import BrightpearlClient
from ops_engine.core.sftp import SftpClient

from decimal import Decimal

from . import brightpearl_queries as queries
from .config import BobcatConfig
from .stock_parser import BobcatStockLine, parse_stock_csv


_LOGGER = logging.getLogger("ops_engine.bobcat.stock_sync")
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"


@dataclass(frozen=True)
class StockUpdate:
    sku: str
    product_id: int
    previous_quantity: int
    new_quantity: int

    @property
    def delta(self) -> int:
        return self.new_quantity - self.previous_quantity


@dataclass
class StockSyncSummary:
    filename: str | None = None
    updates: list[StockUpdate] = field(default_factory=list)
    skipped_unknown_sku: list[str] = field(default_factory=list)
    skipped_update_errors: list[tuple[str, str]] = field(
        default_factory=list
    )  # (sku, error)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def changed(self) -> list[StockUpdate]:
        return [u for u in self.updates if u.delta != 0]


ClockFn = Callable[[], datetime]


def run_stock_sync(
    brightpearl: BrightpearlClient,
    sftp: SftpClient,
    config: BobcatConfig,
    *,
    now: ClockFn | None = None,
) -> StockSyncSummary:
    clock = now if now is not None else _default_clock

    sftp.ensure_dir(config.stock_processed_path)

    filename = _pick_latest_feed_file(sftp, config)
    if filename is None:
        _LOGGER.info(
            "No %s*.csv files in %s",
            config.file_name_prefix,
            config.stock_remote_path,
        )
        return StockSyncSummary()

    summary = StockSyncSummary(filename=filename)
    remote_path = _join(config.stock_remote_path, filename)
    try:
        text = sftp.download_text(remote_path)
        lines = parse_stock_csv(text)
        _LOGGER.info("Parsed %d line(s) from %s", len(lines), filename)

        _apply(brightpearl, config, lines, summary)
    except Exception as exc:  # noqa: BLE001 -- boundary catch
        _LOGGER.exception("Bobcat stock sync failed on %s", filename)
        summary.error = _error_message(exc)
        return summary

    # Only archive the file if the Brightpearl write actually succeeded.
    # If every correction errored we want the file to stay put so the next
    # run (or a human) can retry it.
    if summary.changed or (
        summary.updates and not summary.skipped_update_errors
    ):
        _archive_file(sftp, config, filename, clock)
    else:
        _LOGGER.warning(
            "%s: not archiving -- no corrections succeeded "
            "(%d changed, %d errors, %d unknown SKUs)",
            filename,
            len(summary.changed),
            len(summary.skipped_update_errors),
            len(summary.skipped_unknown_sku),
        )
    return summary


# ---------------------------------------------------------------------------
# Core apply step
# ---------------------------------------------------------------------------


def _apply(
    bp: BrightpearlClient,
    config: BobcatConfig,
    lines: list[BobcatStockLine],
    summary: StockSyncSummary,
) -> None:
    # SKU -> (target quantity, purchase_price) for every feed row.
    targets_by_sku: dict[str, tuple[int, Decimal | None]] = {
        line.sku: (
            config.in_stock_quantity if line.in_stock else 0,
            line.purchase_price,
        )
        for line in lines
    }

    # Resolve SKUs -> product IDs. SKUs Brightpearl doesn't know about are
    # recorded and skipped: we can't update stock for a product that isn't
    # there.
    sku_to_pid = queries.search_product_ids_by_sku(bp, targets_by_sku.keys())
    for sku in targets_by_sku:
        if sku not in sku_to_pid:
            _LOGGER.warning("SKU %s not found in Brightpearl; skipping", sku)
            summary.skipped_unknown_sku.append(sku)

    # Zero out anything Brightpearl thinks is in the warehouse but that
    # the feed didn't mention today. This step is best-effort: Brightpearl
    # doesn't expose a clean "list warehouse contents" endpoint on every
    # account, so if the lookup fails we log it and carry on without the
    # zero-out. The feed SKUs still get their stock set correctly.
    feed_product_ids = set(sku_to_pid.values())
    try:
        existing_in_warehouse = set(
            queries.list_warehouse_product_ids(
                bp, warehouse_id=config.warehouse_id
            )
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort warning
        _LOGGER.warning(
            "Could not list existing warehouse contents (zero-out skipped): %s",
            exc,
        )
        existing_in_warehouse = set()
    to_zero_product_ids = existing_in_warehouse - feed_product_ids

    # Read current levels for the union so we can compute per-product
    # deltas. A single read is cheaper than a read-per-correction and
    # matches Brightpearl's batching sweet spot.
    all_product_ids = sorted(feed_product_ids | to_zero_product_ids)
    current_levels = queries.get_warehouse_stock_levels(
        bp, all_product_ids, warehouse_id=config.warehouse_id
    )

    # Build the list of corrections (and the matching StockUpdate records
    # for the summary). We hand the whole list to the queries layer which
    # splits it into batched POSTs -- one POST per SKU is both wasteful
    # and quickly rate-limited by Brightpearl.
    corrections: list[queries.StockCorrection] = []
    pending_updates: list[StockUpdate] = []

    for sku, (target, price) in targets_by_sku.items():
        pid = sku_to_pid.get(sku)
        if pid is None:
            continue
        previous = current_levels.get(pid, 0)
        update = StockUpdate(
            sku=sku,
            product_id=pid,
            previous_quantity=previous,
            new_quantity=target,
        )
        pending_updates.append(update)
        if update.delta == 0:
            continue
        corrections.append(
            queries.StockCorrection(
                product_id=pid,
                location_id=config.location_id,
                quantity_delta=update.delta,
                cost_value=_format_cost(price),
                currency=config.currency_code,
                note="Bobcat daily stock feed",
            )
        )

    for pid in sorted(to_zero_product_ids):
        previous = current_levels.get(pid, 0)
        if previous == 0:
            continue
        update = StockUpdate(
            sku=f"<pid:{pid}>",
            product_id=pid,
            previous_quantity=previous,
            new_quantity=0,
        )
        pending_updates.append(update)
        corrections.append(
            queries.StockCorrection(
                product_id=pid,
                location_id=config.location_id,
                quantity_delta=update.delta,
                cost_value=_format_cost(None),
                currency=config.currency_code,
                note="Bobcat daily stock feed (zero-out)",
            )
        )

    if not corrections:
        _LOGGER.info("No stock changes to apply")
        summary.updates.extend(pending_updates)
        return

    try:
        queries.apply_stock_corrections(
            bp,
            warehouse_id=config.warehouse_id,
            corrections=corrections,
        )
    except Exception as exc:  # noqa: BLE001 -- record and continue
        _LOGGER.exception("Stock-correction batch failed")
        err = _error_message(exc)
        for update in pending_updates:
            if update.delta != 0:
                summary.skipped_update_errors.append((update.sku, err))
        return

    summary.updates.extend(pending_updates)
    for u in pending_updates:
        if u.delta != 0:
            _LOGGER.info(
                "SKU %s (pid=%d) %d -> %d (delta %+d)",
                u.sku, u.product_id, u.previous_quantity, u.new_quantity, u.delta,
            )


# ---------------------------------------------------------------------------
# SFTP helpers
# ---------------------------------------------------------------------------


def _pick_latest_feed_file(sftp: SftpClient, config: BobcatConfig) -> str | None:
    """Return the newest ``BC_STOCK_*.csv`` in the stock folder, or None.

    Bobcat's filenames carry the timestamp ``BC_STOCK_YYYYMMDDHHMMSS.csv``,
    so alphabetical order sorts oldest-first and we take the last entry.
    """
    try:
        entries = sftp.list_dir(config.stock_remote_path)
    except FileNotFoundError:
        return None
    candidates = sorted(
        name
        for name in entries
        if name.startswith(config.file_name_prefix)
        and name.lower().endswith(".csv")
    )
    return candidates[-1] if candidates else None


def _archive_file(
    sftp: SftpClient,
    config: BobcatConfig,
    filename: str,
    clock: ClockFn,
) -> None:
    src = _join(config.stock_remote_path, filename)
    dst = _join(
        config.stock_processed_path,
        f"{clock().strftime(_TIMESTAMP_FORMAT)}-{filename}",
    )
    sftp.rename(src, dst)


def _join(folder: str, filename: str) -> str:
    if folder.endswith("/"):
        return folder + filename
    return folder + "/" + filename


def _format_cost(price: Decimal | None) -> str:
    """Render the per-unit cost as the string Brightpearl expects.

    Brightpearl's stock-correction endpoint demands a cost even when the
    correction is a reduction. Missing / unparseable prices in the feed
    fall back to "0.00" so the correction still goes through; cost
    accuracy is handled by a separate cost sync (not built yet).
    """
    if price is None:
        return "0.00"
    return f"{price:.2f}"


def _default_clock() -> datetime:
    return datetime.now(tz=timezone.utc)


def _error_message(exc: BaseException) -> str:
    name = type(exc).__name__
    return f"{name}: {exc}" if str(exc) else name
