"""Inbound pipeline: SFTP notifications -> Brightpearl status transitions.

For each new notification CSV Gardiners drop into the SFTP notifications
folder this:

1. Downloads the file.
2. Parses it into typed events with :func:`parse_notification_csv`.
3. Groups events by ``Customer Header Reference`` (the order reference
   we sent in Flow A). Each group becomes one Brightpearl PO transition.
4. Looks up the matching Brightpearl PO. If the reference is purely
   numeric we treat it as a PO ID; otherwise we search BP for it.
5. Maps the events' status to a Brightpearl custom status and PUTs the
   transition.
6. Moves the processed file out of the live notifications folder so we
   don't reprocess it on the next run.

V1 deliberately maps each terminal status directly without distinguishing
"Partially X" cases -- if any line in the file is Despatched we transition
the whole PO to ``GBR JIT - Order Fulfilled``. We can refine once we've
seen real-world notification cadences.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from ops_engine.core.brightpearl import BrightpearlClient
from ops_engine.core.sftp import SftpClient

from . import brightpearl_queries as queries
from .config import GbrJitConfig
from .notification_parser import EventKind, NotificationEvent, parse_notification_csv


_LOGGER = logging.getLogger("ops_engine.gbr_jit.inbound")
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"


@dataclass(frozen=True)
class InboundResult:
    filename: str
    ok: bool
    transitions: tuple[tuple[str, int, int], ...] = ()  # (order_ref, po_id, status_id)
    error: str | None = None


@dataclass
class InboundSummary:
    results: list[InboundResult] = field(default_factory=list)

    @property
    def successes(self) -> list[InboundResult]:
        return [r for r in self.results if r.ok]

    @property
    def failures(self) -> list[InboundResult]:
        return [r for r in self.results if not r.ok]


ClockFn = Callable[[], datetime]


def run_inbound(
    brightpearl: BrightpearlClient,
    sftp: SftpClient,
    config: GbrJitConfig,
    *,
    now: ClockFn | None = None,
) -> InboundSummary:
    clock = now if now is not None else _default_clock

    sftp.ensure_dir(config.notifications_processed_path)

    filenames = _list_csv_files(sftp, config.notifications_remote_path)
    _LOGGER.info(
        "Found %d notification file(s) in %s",
        len(filenames),
        config.notifications_remote_path,
    )

    summary = InboundSummary()
    for filename in filenames:
        summary.results.append(
            _process_one(brightpearl, sftp, config, filename, clock)
        )
    return summary


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def _process_one(
    bp: BrightpearlClient,
    sftp: SftpClient,
    config: GbrJitConfig,
    filename: str,
    clock: ClockFn,
) -> InboundResult:
    remote_path = _join(config.notifications_remote_path, filename)
    transitions: list[tuple[str, int, int]] = []
    try:
        text = sftp.download_text(remote_path)
        events = parse_notification_csv(text)
        if not events:
            _LOGGER.info("%s contained no events", filename)
        for order_ref, group in _group_by_order_ref(events).items():
            transition = _apply_status_transition(bp, config, order_ref, group)
            if transition is not None:
                transitions.append(transition)

        _archive_file(sftp, config, filename, clock)
        _LOGGER.info(
            "Processed %s -> %d transition(s)", filename, len(transitions)
        )
        return InboundResult(
            filename=filename,
            ok=True,
            transitions=tuple(transitions),
        )
    except Exception as exc:  # noqa: BLE001 -- deliberate boundary catch
        _LOGGER.exception("Failed to process %s", filename)
        return InboundResult(
            filename=filename,
            ok=False,
            transitions=tuple(transitions),
            error=_error_message(exc),
        )


def _apply_status_transition(
    bp: BrightpearlClient,
    config: GbrJitConfig,
    order_ref: str,
    events: list[NotificationEvent],
) -> tuple[str, int, int] | None:
    new_status_id = _decide_status_id(events, config)
    if new_status_id is None:
        _LOGGER.info(
            "Order %s: no actionable status across %d events; skipping",
            order_ref,
            len(events),
        )
        return None

    po_id = _resolve_po_id(bp, order_ref, config)
    if po_id is None:
        raise RuntimeError(
            f"Order reference {order_ref!r} could not be resolved to a "
            "Brightpearl PO"
        )

    queries.set_order_status(bp, po_id, status_id=new_status_id)
    _LOGGER.info(
        "Transitioned PO %s (ref=%s) to status %d",
        po_id,
        order_ref,
        new_status_id,
    )
    return (order_ref, po_id, new_status_id)


def _decide_status_id(
    events: list[NotificationEvent], config: GbrJitConfig
) -> int | None:
    """Map the strongest event in the group to a Brightpearl status ID.

    Cancellation wins over despatch wins over receipt; OTHER events are
    ignored. Returns None if nothing actionable.
    """
    kinds = {e.kind for e in events}
    if EventKind.CANCELLED in kinds:
        return config.status_id_cancelled
    if EventKind.DESPATCHED in kinds:
        return config.status_id_order_fulfilled
    if EventKind.RECEIVED in kinds:
        return config.status_id_acknowledged
    return None


def _resolve_po_id(
    bp: BrightpearlClient, order_ref: str, config: GbrJitConfig
) -> int | None:
    if order_ref.isdigit():
        return int(order_ref)
    matches = queries.find_po_id_by_reference(
        bp,
        order_ref,
        supplier_contact_id=config.gardiners_jit_supplier_contact_id,
    )
    if not matches:
        return None
    if len(matches) > 1:
        _LOGGER.warning(
            "Reference %r matched %d POs (%s); using the first",
            order_ref,
            len(matches),
            matches,
        )
    return matches[0]


# ---------------------------------------------------------------------------
# SFTP helpers
# ---------------------------------------------------------------------------


def _list_csv_files(sftp: SftpClient, remote_path: str) -> list[str]:
    return sorted(
        name
        for name in sftp.list_dir(remote_path)
        if name.lower().endswith(".csv")
    )


def _archive_file(
    sftp: SftpClient,
    config: GbrJitConfig,
    filename: str,
    clock: ClockFn,
) -> None:
    src = _join(config.notifications_remote_path, filename)
    dst = _join(
        config.notifications_processed_path,
        f"{clock().strftime(_TIMESTAMP_FORMAT)}-{filename}",
    )
    sftp.rename(src, dst)


def _join(folder: str, filename: str) -> str:
    if folder.endswith("/"):
        return folder + filename
    return folder + "/" + filename


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _group_by_order_ref(
    events: Iterable[NotificationEvent],
) -> dict[str, list[NotificationEvent]]:
    grouped: dict[str, list[NotificationEvent]] = {}
    for event in events:
        grouped.setdefault(event.order_reference, []).append(event)
    return grouped


def _default_clock() -> datetime:
    return datetime.now(tz=timezone.utc)


def _error_message(exc: BaseException) -> str:
    name = type(exc).__name__
    return f"{name}: {exc}" if str(exc) else name
