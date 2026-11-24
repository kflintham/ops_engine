from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from ops_engine.integrations.titan_safety.stock_parser import (
    TitanStockError,
    normalize_sku,
    parse_stock_xlsx,
)


# ---------------------------------------------------------------------------
# normalize_sku
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("TC01-03", "TC01-3"),
        ("TC02-04", "TC02-4"),
        ("TC01-09", "TC01-9"),
        ("TC01-10", "TC01-10"),
        ("TC01-13", "TC01-13"),
        ("TC01-3", "TC01-3"),  # already normalised
        ("TC01-00", "TC01-0"),  # edge: zero stock size
        ("TC01", "TC01"),  # no trailing segment
        ("TC01-03A", "TC01-03A"),  # non-numeric tail stays
        ("", ""),  # empty stays
    ],
)
def test_normalize_sku(source: str, expected: str) -> None:
    assert normalize_sku(source) == expected


# ---------------------------------------------------------------------------
# parse_stock_xlsx
# ---------------------------------------------------------------------------


def _xlsx_bytes(rows: list[tuple]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


HEADER = ("Product Code", "Product Description", "Available Qty")


def test_parses_basic_rows_and_normalizes_skus() -> None:
    data = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest Size 03", 35),
            ("TC02-10", "Supreme size 10", 412),
        ]
    )
    lines = parse_stock_xlsx(data)
    skus = {line.sku for line in lines}
    assert skus == {"TC01-3", "TC02-10"}
    first = {line.sku: line for line in lines}["TC01-3"]
    assert first.source_sku == "TC01-03"
    assert first.description == "Quest Size 03"
    assert first.quantity == 35


def test_tolerates_blank_leading_row_like_titan_feed() -> None:
    # The real feed has a (None, None, None) row before the header.
    data = _xlsx_bytes(
        [
            (None, None, None),
            HEADER,
            ("TC01-03", "Quest Size 03", 35),
        ]
    )
    [line] = parse_stock_xlsx(data)
    assert line.sku == "TC01-3"
    assert line.quantity == 35


def test_skips_blank_sku_rows_without_erroring() -> None:
    data = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest", 35),
            (None, None, None),
            ("TC02-04", "Supreme", 10),
        ]
    )
    lines = parse_stock_xlsx(data)
    assert {line.sku for line in lines} == {"TC01-3", "TC02-4"}


def test_duplicate_sku_last_wins() -> None:
    data = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest", 35),
            ("TC01-03", "Quest", 50),
        ]
    )
    [line] = parse_stock_xlsx(data)
    assert line.quantity == 50


def test_quantity_accepts_float_whole_number() -> None:
    data = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest", 35.0),
        ]
    )
    [line] = parse_stock_xlsx(data)
    assert line.quantity == 35


def test_rejects_fractional_quantity() -> None:
    data = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest", 35.5),
        ]
    )
    with pytest.raises(TitanStockError, match="not whole"):
        parse_stock_xlsx(data)


def test_negative_quantity_clamps_to_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Titan's real feed carries occasional negative quantities
    # (oversold / committed-but-unshipped stock). We clamp to 0 so a
    # whole run doesn't fail on them.
    data = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest", -4),
            ("TC01-10", "Quest size 10", 15),
        ]
    )
    with caplog.at_level("WARNING"):
        lines = parse_stock_xlsx(data)
    by_sku = {line.sku: line for line in lines}
    assert by_sku["TC01-3"].quantity == 0
    assert by_sku["TC01-10"].quantity == 15
    # The original value is logged so we can see it happened.
    assert any(
        "negative quantity -4" in record.message for record in caplog.records
    )


def test_rejects_non_numeric_quantity() -> None:
    data = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest", "lots"),
        ]
    )
    with pytest.raises(TitanStockError, match="not an integer"):
        parse_stock_xlsx(data)


def test_missing_header_raises() -> None:
    data = _xlsx_bytes(
        [
            ("something-else", "foo", "bar"),
            ("TC01-03", "Quest", 35),
        ]
    )
    with pytest.raises(TitanStockError, match="header not found"):
        parse_stock_xlsx(data)


def test_empty_file_raises() -> None:
    data = _xlsx_bytes([HEADER])
    with pytest.raises(TitanStockError, match="no usable stock lines"):
        parse_stock_xlsx(data)


def test_bad_xlsx_bytes_raises() -> None:
    with pytest.raises(TitanStockError, match="Could not open XLSX"):
        parse_stock_xlsx(b"this is not a workbook")
