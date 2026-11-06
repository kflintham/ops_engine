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
        _archive_file(sftp, config, filename, clock)
    except Exception as exc:  # noqa: BLE001 -- boundary catch
        _LOGGER.exception("Bobcat stock sync failed on %s", filename)
        summary.error = _error_message(exc)
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
    # SKU -> target quantity we want in the warehouse after this run.
    targets_by_sku: dict[str, int] = {
        line.sku: (config.in_stock_quantity if line.in_stock else 0)
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
    # the feed didn't mention today.
    feed_product_ids = set(sku_to_pid.values())
    existing_in_warehouse = set(
        queries.list_warehouse_product_ids(bp, warehouse_id=config.warehouse_id)
    )
    to_zero_product_ids = existing_in_warehouse - feed_product_ids

    # Read current levels for the union so we can compute per-product
    # deltas. A single read is cheaper than a read-per-correction and
    # matches Brightpearl's batching sweet spot.
    all_product_ids = sorted(feed_product_ids | to_zero_product_ids)
    current_levels = queries.get_warehouse_stock_levels(
        bp, all_product_ids, warehouse_id=config.warehouse_id
    )

    # Feed lines first, in input order (preserves parser's "last wins").
    for sku, target in targets_by_sku.items():
        pid = sku_to_pid.get(sku)
        if pid is None:
            continue
        current = current_levels.get(pid, 0)
        _apply_one(
            bp, config, sku=sku, product_id=pid,
            previous=current, target=target, summary=summary,
        )

    # Zero-out products absent from today's feed.
    for pid in sorted(to_zero_product_ids):
        current = current_levels.get(pid, 0)
        if current == 0:
            continue
        _apply_one(
            bp, config, sku=f"<pid:{pid}>", product_id=pid,
            previous=current, target=0, summary=summary,
        )


def _apply_one(
    bp: BrightpearlClient,
    config: BobcatConfig,
    *,
    sku: str,
    product_id: int,
    previous: int,
    target: int,
    summary: StockSyncSummary,
) -> None:
    delta = target - previous
    update = StockUpdate(
        sku=sku,
        product_id=product_id,
        previous_quantity=previous,
        new_quantity=target,
    )
    if delta == 0:
        summary.updates.append(update)
        return
    try:
        queries.apply_stock_correction(
            bp,
            warehouse_id=config.warehouse_id,
            product_id=product_id,
            quantity_delta=delta,
            note="Bobcat daily stock feed",
        )
        summary.updates.append(update)
        _LOGGER.info(
            "SKU %s (pid=%d) %d -> %d (delta %+d)",
            sku, product_id, previous, target, delta,
        )
    except Exception as exc:  # noqa: BLE001 -- per-SKU boundary
        _LOGGER.exception(
            "Failed to apply correction for SKU %s (pid=%d)", sku, product_id
        )
        summary.skipped_update_errors.append((sku, _error_message(exc)))


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


def _default_clock() -> datetime:
    return datetime.now(tz=timezone.utc)


def _error_message(exc: BaseException) -> str:
    name = type(exc).__name__
    return f"{name}: {exc}" if str(exc) else name
