"""Outbound pipeline: Brightpearl PO -> GB order CSV -> SFTP -> status update.

Single-entry orchestrator for sending Gardiner Brothers JIT orders. Wires
:mod:`brightpearl_queries`, :mod:`po_mapper`, :mod:`order_builder` and the
SFTP client together.

The pipeline processes each PO independently: a failure on one PO does not
stop the others, and the failing PO stays on ``GBR JIT - Request Sent`` so
the next run retries it. A :class:`OutboundSummary` is returned describing
what happened to each PO, and every failure is logged via the standard
``logging`` module under the ``ops_engine.gbr_jit.outbound`` logger name.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from ops_engine.core.brightpearl import BrightpearlClient
from ops_engine.core.sftp import SftpClient

from . import brightpearl_queries as queries
from .config import GbrJitConfig
from .order_builder import Order, build_order_csv
from .po_mapper import GbrJitMappingError, build_order_from_po


_LOGGER = logging.getLogger("ops_engine.gbr_jit.outbound")
_TIMESTAMP_FORMAT = "%Y%m%d%H%M"


@dataclass(frozen=True)
class OutboundResult:
    order_id: int
    ok: bool
    remote_path: str | None = None
    order_reference: str | None = None
    error: str | None = None


@dataclass
class OutboundSummary:
    results: list[OutboundResult] = field(default_factory=list)

    @property
    def successes(self) -> list[OutboundResult]:
        return [r for r in self.results if r.ok]

    @property
    def failures(self) -> list[OutboundResult]:
        return [r for r in self.results if not r.ok]


ClockFn = Callable[[], datetime]


def run_outbound(
    brightpearl: BrightpearlClient,
    sftp: SftpClient,
    config: GbrJitConfig,
    *,
    now: ClockFn | None = None,
) -> OutboundSummary:
    clock = now if now is not None else _default_clock

    po_ids = queries.search_jit_pos_awaiting_send(
        brightpearl,
        supplier_contact_id=config.gardiners_jit_supplier_contact_id,
        status_id_request_sent=config.status_id_request_sent,
    )
    _LOGGER.info("Found %d JIT PO(s) to send", len(po_ids))

    summary = OutboundSummary()
    for po_id in po_ids:
        summary.results.append(_send_one(brightpearl, sftp, config, po_id, clock))
    return summary


def _send_one(
    bp: BrightpearlClient,
    sftp: SftpClient,
    config: GbrJitConfig,
    po_id: int,
    clock: ClockFn,
) -> OutboundResult:
    try:
        po = queries.get_order(bp, po_id)
        order = _build_order(bp, po, config)
        csv_text = build_order_csv(order)
        remote_path = _remote_path(config, order, clock)

        sftp.upload_text(csv_text, remote_path)
        _LOGGER.info(
            "Uploaded PO %s to %s (%d lines)", po_id, remote_path, len(order.lines)
        )

        queries.set_order_status(bp, po_id, status_id=config.status_id_pending)
        _LOGGER.info("Moved PO %s to Pending", po_id)

        return OutboundResult(
            order_id=po_id,
            ok=True,
            remote_path=remote_path,
            order_reference=order.reference,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberate boundary catch
        _LOGGER.exception("Failed to send PO %s", po_id)
        return OutboundResult(
            order_id=po_id,
            ok=False,
            error=_error_message(exc),
        )


def _build_order(
    bp: BrightpearlClient, po: Mapping[str, object], config: GbrJitConfig
) -> Order:
    product_ids = _product_ids_for(po)
    supplier_map = queries.get_product_supplier_ids(bp, product_ids)
    sku_map = queries.get_product_gardiners_skus(
        bp, product_ids, price_list_id=config.gardiners_price_list_id
    )
    try:
        return build_order_from_po(
            po,
            product_supplier_ids=supplier_map,
            product_gardiners_skus=sku_map,
            required_supplier_contact_id=config.gardiners_jit_supplier_contact_id,
        )
    except GbrJitMappingError:
        # Re-raise as-is; the orchestrator's outer catch logs + records it.
        raise


def _product_ids_for(po: Mapping[str, object]) -> list[int]:
    rows = po.get("orderRows")
    product_ids: list[int] = []
    iterable: list[Mapping[str, object]] = []
    if isinstance(rows, Mapping):
        iterable = [r for r in rows.values() if isinstance(r, Mapping)]
    elif isinstance(rows, list):
        iterable = [r for r in rows if isinstance(r, Mapping)]
    for row in iterable:
        raw = row.get("productId")
        try:
            product_ids.append(int(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    # de-duplicate, preserve order
    seen: set[int] = set()
    unique: list[int] = []
    for pid in product_ids:
        if pid not in seen:
            seen.add(pid)
            unique.append(pid)
    return unique


def _remote_path(config: GbrJitConfig, order: Order, clock: ClockFn) -> str:
    timestamp = clock().strftime(_TIMESTAMP_FORMAT)
    filename = config.file_name_template.format(
        order_reference=order.reference,
        timestamp=timestamp,
    )
    folder = config.orders_remote_path
    if not folder.endswith("/"):
        folder = folder + "/"
    return folder + filename


def _default_clock() -> datetime:
    return datetime.now(tz=timezone.utc)


def _error_message(exc: BaseException) -> str:
    name = type(exc).__name__
    return f"{name}: {exc}" if str(exc) else name
