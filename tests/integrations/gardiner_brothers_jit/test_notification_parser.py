from __future__ import annotations

from pathlib import Path

import pytest

from ops_engine.integrations.gardiner_brothers_jit.notification_parser import (
    Consignment,
    EventKind,
    NotificationEvent,
    parse_notification_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIVED_SAMPLE = (
    REPO_ROOT
    / "docs"
    / "gardiner-brothers-jit"
    / "samples"
    / "order-notification-received.csv"
)
DESPATCHED_SAMPLE = (
    REPO_ROOT
    / "docs"
    / "gardiner-brothers-jit"
    / "samples"
    / "order-notification-despatched.csv"
)


def test_parses_received_sample() -> None:
    events = parse_notification_csv(RECEIVED_SAMPLE.read_text())

    assert events == [
        NotificationEvent(
            order_reference="Test002",
            line_reference="12346-1",
            sku="34233-58447-07",
            quantity=1,
            kind=EventKind.RECEIVED,
            raw_current_status="Recieved",
            consignment=None,
        ),
        NotificationEvent(
            order_reference="Test002",
            line_reference="12346-2",
            sku="24840-41090-04",
            quantity=1,
            kind=EventKind.RECEIVED,
            raw_current_status="Recieved",
            consignment=None,
        ),
    ]


def test_parses_despatched_sample() -> None:
    events = parse_notification_csv(DESPATCHED_SAMPLE.read_text())

    expected_consignment = Consignment(
        carrier="DPD",
        status_code="DES",
        reference="123456789",
        tracking_url="https://www.dpd.co.uk/apps/tracking/?reference=123456789",
    )
    assert len(events) == 2
    for event in events:
        assert event.kind is EventKind.DESPATCHED
        assert event.raw_current_status == "Despatched"
        assert event.consignment == expected_consignment


def test_unknown_current_status_maps_to_other() -> None:
    csv_text = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
        "A,A-1,SKU-1,Thing,Black,7,1,,,,,Picking\r\n"
    )
    events = parse_notification_csv(csv_text)

    assert len(events) == 1
    assert events[0].kind is EventKind.OTHER
    assert events[0].raw_current_status == "Picking"


def test_cancelled_status_is_recognised() -> None:
    csv_text = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
        "A,A-1,SKU-1,Thing,Black,7,1,,,,,Cancelled\r\n"
    )
    events = parse_notification_csv(csv_text)
    assert events[0].kind is EventKind.CANCELLED


def test_empty_file_with_header_only_returns_no_events() -> None:
    csv_text = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
    )
    assert parse_notification_csv(csv_text) == []


def test_missing_order_reference_raises() -> None:
    csv_text = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
        ",A-1,SKU-1,Thing,Black,7,1,,,,,Recieved\r\n"
    )
    with pytest.raises(ValueError, match="Customer Header Reference"):
        parse_notification_csv(csv_text)


def test_non_integer_quantity_raises() -> None:
    csv_text = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
        "A,A-1,SKU-1,Thing,Black,7,one,,,,,Recieved\r\n"
    )
    with pytest.raises(ValueError, match="Quantity"):
        parse_notification_csv(csv_text)
