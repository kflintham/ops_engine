"""Parse Gardiner Brothers JIT order-status notification CSVs.

Gardiners drop one CSV per order per status change into the SFTP
notifications folder. Format is documented in
``docs/gardiner-brothers-jit/process-docs/notifications-processing.pdf``;
worked examples are in
``docs/gardiner-brothers-jit/samples/order-notification-received.csv`` and
``docs/gardiner-brothers-jit/samples/order-notification-despatched.csv``.

This module turns a raw CSV (as bytes or string) into a list of
``NotificationEvent`` objects. It does not read files, talk to Brightpearl,
or decide what to do with the events -- those are separate concerns.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from enum import Enum


class EventKind(Enum):
    RECEIVED = "received"
    CANCELLED = "cancelled"
    DESPATCHED = "despatched"
    OTHER = "other"


_CURRENT_STATUS_MAP: dict[str, EventKind] = {
    "received": EventKind.RECEIVED,
    "recieved": EventKind.RECEIVED,
    "cancelled": EventKind.CANCELLED,
    "canceled": EventKind.CANCELLED,
    "despatched": EventKind.DESPATCHED,
    "dispatched": EventKind.DESPATCHED,
}


# Brightpearl sample CSVs (committed under ``docs/.../samples/``) used
# space-separated column names like ``Customer Header Reference``. The live
# Gardiners notification template uses CamelCase with no spaces
# (``CustomerHeaderReference``) and adds many extra columns we ignore.
# Accept both. The order in each list is preference -- production-format
# first since that's what we'll see in real notifications.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "order_reference": ("CustomerHeaderReference", "Customer Header Reference"),
    "line_reference": ("CustomerLineReference", "Customer Line Reference"),
    "sku": ("Sku",),
    "quantity": ("Quantity",),
    "current_status": ("CurrentStatus", "Current Status"),
    "carrier": ("Carrier",),
    "consignment_status": ("ConsignmentStatus", "Consignment Status"),
    "consignment_reference": ("ConsignmentReference", "Consignment Reference"),
    "consignment_tracking_url": ("ConsignmentTrackingUrl", "Consignment Tracking Url"),
}


@dataclass(frozen=True)
class Consignment:
    carrier: str
    status_code: str
    reference: str
    tracking_url: str


@dataclass(frozen=True)
class NotificationEvent:
    order_reference: str
    line_reference: str
    sku: str
    quantity: int
    kind: EventKind
    raw_current_status: str
    consignment: Consignment | None


def parse_notification_csv(text: str) -> list[NotificationEvent]:
    reader = csv.DictReader(io.StringIO(text))
    events: list[NotificationEvent] = []
    for row_number, row in enumerate(reader, start=2):
        events.append(_row_to_event(row, row_number))
    return events


def _row_to_event(row: dict[str, str], row_number: int) -> NotificationEvent:
    order_reference = _required(row, "order_reference", row_number)
    line_reference = _required(row, "line_reference", row_number)
    sku = _required(row, "sku", row_number)
    quantity = _parse_quantity(_field(row, "quantity"), row_number)
    raw_status = (_field(row, "current_status") or "").strip()
    kind = _CURRENT_STATUS_MAP.get(raw_status.lower(), EventKind.OTHER)
    consignment = _parse_consignment(row)
    return NotificationEvent(
        order_reference=order_reference,
        line_reference=line_reference,
        sku=sku,
        quantity=quantity,
        kind=kind,
        raw_current_status=raw_status,
        consignment=consignment,
    )


def _field(row: dict[str, str], field: str) -> str | None:
    """Return the first non-empty value across that field's known aliases."""
    for column in _COLUMN_ALIASES[field]:
        raw = row.get(column)
        if raw is None:
            continue
        value = raw.strip()
        if value:
            return value
    return None


def _required(row: dict[str, str], field: str, row_number: int) -> str:
    value = _field(row, field)
    if value is None:
        aliases = " / ".join(repr(c) for c in _COLUMN_ALIASES[field])
        raise ValueError(
            f"Row {row_number}: required column ({aliases}) is missing or empty"
        )
    return value


def _parse_quantity(raw: str | None, row_number: int) -> int:
    if not raw:
        raise ValueError(f"Row {row_number}: Quantity is missing")
    value = raw.strip()
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"Row {row_number}: Quantity {value!r} is not an integer"
        ) from exc


def _parse_consignment(row: dict[str, str]) -> Consignment | None:
    carrier = _field(row, "carrier") or ""
    status_code = _field(row, "consignment_status") or ""
    reference = _field(row, "consignment_reference") or ""
    tracking_url = _field(row, "consignment_tracking_url") or ""
    if not any((carrier, status_code, reference, tracking_url)):
        return None
    return Consignment(
        carrier=carrier,
        status_code=status_code,
        reference=reference,
        tracking_url=tracking_url,
    )