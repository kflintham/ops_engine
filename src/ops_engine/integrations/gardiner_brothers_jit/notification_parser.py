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


# Free-text values Gardiners put in the ``Current Status`` column, normalised
# to lowercase. ``recieved`` is their spelling in the sample files; we accept
# both. ``cancelled`` / ``canceled`` covers UK + US spellings.
_CURRENT_STATUS_MAP: dict[str, EventKind] = {
    "received": EventKind.RECEIVED,
    "recieved": EventKind.RECEIVED,
    "cancelled": EventKind.CANCELLED,
    "canceled": EventKind.CANCELLED,
    "despatched": EventKind.DESPATCHED,
    "dispatched": EventKind.DESPATCHED,
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
    order_reference = _required(row, "Customer Header Reference", row_number)
    line_reference = _required(row, "Customer Line Reference", row_number)
    sku = _required(row, "Sku", row_number)
    quantity = _parse_quantity(row.get("Quantity", ""), row_number)
    raw_status = (row.get("Current Status") or "").strip()
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


def _required(row: dict[str, str], column: str, row_number: int) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise ValueError(
            f"Row {row_number}: required column {column!r} is missing or empty"
        )
    return value


def _parse_quantity(raw: str, row_number: int) -> int:
    value = raw.strip()
    if not value:
        raise ValueError(f"Row {row_number}: Quantity is missing")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"Row {row_number}: Quantity {value!r} is not an integer"
        ) from exc


def _parse_consignment(row: dict[str, str]) -> Consignment | None:
    carrier = (row.get("Carrier") or "").strip()
    status_code = (row.get("Consignment Status") or "").strip()
    reference = (row.get("Consignment Reference") or "").strip()
    tracking_url = (row.get("Consignment Tracking Url") or "").strip()
    if not any((carrier, status_code, reference, tracking_url)):
        return None
    return Consignment(
        carrier=carrier,
        status_code=status_code,
        reference=reference,
        tracking_url=tracking_url,
    )
