from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from ops_engine.integrations.gardiner_brothers_jit.order_builder import (
    ORDER_CSV_COLUMNS,
    Order,
    OrderLine,
    build_order_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_CSV = (
    REPO_ROOT
    / "docs"
    / "gardiner-brothers-jit"
    / "samples"
    / "order-file-template.csv"
)


def _rows(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text)) if row]


def test_builder_output_matches_gardiners_sample_file() -> None:
    order = Order(
        reference="Test002",
        lines=(
            OrderLine(sku="34233-58447-07", quantity=1, line_reference="12346-1"),
            OrderLine(sku="24840-41090-04", quantity=1, line_reference="12346-2"),
        ),
    )

    built = build_order_csv(order)
    sample = SAMPLE_CSV.read_text()

    assert _rows(built) == _rows(sample)


def test_header_row_matches_spec() -> None:
    order = Order(
        reference="X",
        lines=(OrderLine(sku="SKU-1", quantity=1, line_reference="L1"),),
    )
    first_row = _rows(build_order_csv(order))[0]
    assert tuple(first_row) == ORDER_CSV_COLUMNS


def test_uses_crlf_line_endings() -> None:
    order = Order(
        reference="X",
        lines=(OrderLine(sku="SKU-1", quantity=1, line_reference="L1"),),
    )
    csv_text = build_order_csv(order)
    assert "\r\n" in csv_text
    assert csv_text.count("\r\n") == csv_text.count("\n")


def test_order_reference_repeats_on_every_line() -> None:
    order = Order(
        reference="REF-ABC",
        lines=(
            OrderLine(sku="A", quantity=1, line_reference="L1"),
            OrderLine(sku="B", quantity=2, line_reference="L2"),
        ),
    )
    rows = _rows(build_order_csv(order))[1:]
    assert [row[2] for row in rows] == ["REF-ABC", "REF-ABC"]


def test_rejects_empty_order() -> None:
    with pytest.raises(ValueError, match="at least one line"):
        build_order_csv(Order(reference="X", lines=()))


def test_rejects_missing_order_reference() -> None:
    with pytest.raises(ValueError, match="Order reference"):
        build_order_csv(
            Order(
                reference="",
                lines=(OrderLine(sku="A", quantity=1, line_reference="L1"),),
            )
        )


def test_rejects_zero_or_negative_quantity() -> None:
    order = Order(
        reference="X",
        lines=(OrderLine(sku="A", quantity=0, line_reference="L1"),),
    )
    with pytest.raises(ValueError, match="positive integer"):
        build_order_csv(order)


def test_rejects_missing_sku() -> None:
    order = Order(
        reference="X",
        lines=(OrderLine(sku="", quantity=1, line_reference="L1"),),
    )
    with pytest.raises(ValueError, match="SKU"):
        build_order_csv(order)


def test_rejects_missing_line_reference() -> None:
    order = Order(
        reference="X",
        lines=(OrderLine(sku="A", quantity=1, line_reference=""),),
    )
    with pytest.raises(ValueError, match="line reference"):
        build_order_csv(order)
