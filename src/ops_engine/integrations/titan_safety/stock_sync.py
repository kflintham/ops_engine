"""Stock-sync pipeline: Titan Safety SFTP XLSX -> Brightpearl warehouse 15.

Each run:

1. Lists the Titan stock folder on SFTP, picks the newest xlsx.
2. Parses it into typed stock lines (with SKUs already normalised so
   ``TC01-03`` becomes ``TC01-3`` for Brightpearl lookup).
3. Resolves each SKU to a Brightpearl product ID.
4. Reads the warehouse's current stock levels for every feed product.
5. Computes the delta needed per product (target - current) and POSTs a
   single batched stock correction when non-zero.
6. Archives the XLSX into the processed folder so the next run skips it.

Zero-out-on-absence is intentionally not implemented; Titan's feed is
authoritative for every SKU in it, and the Brightpearl endpoint to
enumerate warehouse contents isn't exposed on this account (same as
Bobcat).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from ops_engine.core.brightpearl import BrightpearlClient
from ops_engine.core.sftp import SftpClient

from . import brightpearl_queries as queries
from .config import TitanSafetyConfig
from .stock_parser import TitanStockLine, parse_stock_xlsx


_LOGGER = logging.getLogger("ops_engine.titan_safety.stock_sync")
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"
_XLSX_EXTENSIONS = (".xlsx", ".xlsm")
_CORRECTION_NOTE = "Titan Safety daily stock feed"


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
    skipped_update_errors: list[tuple[str, str]] = field(default_factory=list)
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
    config: TitanSafetyConfig,
    *,
    now: ClockFn | None = None,
) -> StockSyncSummary:
    clock = now if now is not None else _default_clock

    sftp.ensure_dir(config.stock_processed_path)

    filename = _pick_latest_feed_file(sftp, config)
    if filename is None:
        _LOGGER.info(
            "No xlsx files in %s", config.stock_remote_path
        )
        return StockSyncSummary()

    summary = StockSyncSummary(filename=filename)
    remote_path = _join(config.stock_remote_path, filename)
    try:
        data = sftp.download_bytes(remote_path)
        lines = parse_stock_xlsx(data)
        _LOGGER.info("Parsed %d line(s) from %s", len(lines), filename)

        _apply(brightpearl, config, lines, summary)
    except Exception as exc:  # noqa: BLE001 -- boundary catch
        _LOGGER.exception("Titan Safety stock sync failed on %s", filename)
        summary.error = _error_message(exc)
        return summary

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
    config: TitanSafetyConfig,
    lines: list[TitanStockLine],
    summary: StockSyncSummary,
) -> None:
    # SKU -> target quantity for every feed row. Titan's "Available Qty"
    # is an actual count; we write that number into Brightpearl.
    targets_by_sku: dict[str, int] = {
        line.sku: line.quantity for line in lines
    }

    sku_to_pid = queries.search_product_ids_by_sku(bp, targets_by_sku.keys())
    for sku in targets_by_sku:
        if sku not in sku_to_pid:
            _LOGGER.warning("SKU %s not found in Brightpearl; skipping", sku)
            summary.skipped_unknown_sku.append(sku)

    feed_product_ids = sorted(set(sku_to_pid.values()))
    current_levels = queries.get_warehouse_stock_levels(
        bp, feed_product_ids, warehouse_id=config.warehouse_id
    )

    corrections: list[queries.StockCorrection] = []
    pending_updates: list[StockUpdate] = []

    for sku, target in targets_by_sku.items():
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
                # Titan's feed doesn't carry per-SKU cost, so we send
                # "0.00". If stock corrections are ever rejected with
                # WHSC-017 we can source cost from Brightpearl itself
                # or add a cost column request to Titan.
                cost_value="0.00",
                currency=config.currency_code,
                note=_CORRECTION_NOTE,
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


def _pick_latest_feed_file(
    sftp: SftpClient, config: TitanSafetyConfig
) -> str | None:
    try:
        entries = sftp.list_dir(config.stock_remote_path)
    except FileNotFoundError:
        return None
    candidates = sorted(
        name
        for name in entries
        if name.lower().endswith(_XLSX_EXTENSIONS)
    )
    return candidates[-1] if candidates else None


def _archive_file(
    sftp: SftpClient,
    config: TitanSafetyConfig,
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
