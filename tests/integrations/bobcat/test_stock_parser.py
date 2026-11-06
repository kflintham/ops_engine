from __future__ import annotations

from decimal import Decimal

import pytest

from ops_engine.integrations.bobcat.stock_parser import (
    BobcatStockError,
    parse_stock_csv,
)


HEADER = (
    '"Brand","EAN","Purchase Price (GBP)","RRP (GBP)","Style Code",'
    '"Colour Code","Size","Description","Style Name","+10 Available","SKU"'
)


def _row(sku: str, availability: str, price: str = "54.79") -> str:
    return (
        f'Bobcat Mens,"5059529681363","{price}","100.00",BC11436,200,40,'
        f'Black Nubuck,KEYSTONE Virginia,"{availability}",{sku}'
    )


def test_yes_maps_to_in_stock_true() -> None:
    text = "\n".join([HEADER, _row("BC11436-200-40", "Yes")])
    [line] = parse_stock_csv(text)
    assert line.sku == "BC11436-200-40"
    assert line.in_stock is True
    assert line.purchase_price == Decimal("54.79")


def test_no_maps_to_in_stock_false() -> None:
    text = "\n".join([HEADER, _row("BC11436-200-40", "No")])
    [line] = parse_stock_csv(text)
    assert line.in_stock is False


def test_no_with_trailing_space_maps_to_in_stock_false() -> None:
    # Bobcat's live feed uses the string "No " for out-of-stock rows.
    text = "\n".join([HEADER, _row("BC41436-20X-35", "No ")])
    [line] = parse_stock_csv(text)
    assert line.in_stock is False


def test_mixed_rows() -> None:
    text = "\n".join(
        [
            HEADER,
            _row("A", "Yes"),
            _row("B", "No "),
            _row("C", "yes"),  # case-insensitive
            _row("D", "NO"),
        ]
    )
    by_sku = {line.sku: line for line in parse_stock_csv(text)}
    assert by_sku["A"].in_stock is True
    assert by_sku["B"].in_stock is False
    assert by_sku["C"].in_stock is True
    assert by_sku["D"].in_stock is False


def test_unknown_availability_raises() -> None:
    text = "\n".join([HEADER, _row("X", "Maybe")])
    with pytest.raises(BobcatStockError, match="unexpected"):
        parse_stock_csv(text)


def test_blank_sku_raises() -> None:
    text = "\n".join([HEADER, _row("", "Yes")])
    with pytest.raises(BobcatStockError, match="SKU is missing"):
        parse_stock_csv(text)


def test_missing_sku_column_raises() -> None:
    bad_header = HEADER.replace(',"SKU"', "")
    text = bad_header
    with pytest.raises(BobcatStockError, match="SKU"):
        parse_stock_csv(text)


def test_missing_availability_column_raises() -> None:
    bad_header = HEADER.replace(',"+10 Available"', "")
    text = bad_header
    with pytest.raises(BobcatStockError, match="\\+10 Available"):
        parse_stock_csv(text)


def test_empty_csv_raises() -> None:
    with pytest.raises(BobcatStockError, match="no header"):
        parse_stock_csv("")


def test_duplicate_sku_last_wins() -> None:
    text = "\n".join(
        [HEADER, _row("DUP", "Yes"), _row("DUP", "No ")]
    )
    [line] = parse_stock_csv(text)
    assert line.sku == "DUP"
    assert line.in_stock is False


def test_bad_price_does_not_block_stock_update() -> None:
    text = "\n".join([HEADER, _row("A", "Yes", price="not-a-number")])
    [line] = parse_stock_csv(text)
    assert line.in_stock is True
    assert line.purchase_price is None
