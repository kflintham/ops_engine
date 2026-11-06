"""Parse Bobcat daily stock CSVs.

Bobcat's feed has one row per SKU. The column we care about is
``+10 Available``:

- ``"Yes"`` means Bobcat have at least ten units in stock -- we set the
  level in Brightpearl to a configurable "in stock" quantity (default 10).
- ``"No "`` (sometimes with trailing whitespace) means no stock -- we set
  the level to 0.

Any other value in that column raises ``BobcatStockError``. A row with a
blank or missing SKU is also rejected: we'd rather fail loudly than
silently skip stock. The ``Purchase Price (GBP)`` column is parsed so
later work can sync costs, but the orchestrator doesn't use it yet.

This module is pure: it reads text and returns dataclasses. It doesn't
touch SFTP or Brightpearl.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


class BobcatStockError(Exception):
    """Raised when a Bobcat stock CSV can't be parsed into stock lines."""


@dataclass(frozen=True)
class BobcatStockLine:
    sku: str
    in_stock: bool
    purchase_price: Decimal | None  # GBP, parsed but currently unused


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "sku": ("SKU",),
    "availability": ("+10 Available",),
    "purchase_price": ("Purchase Price (GBP)",),
}


def parse_stock_csv(text: str) -> list[BobcatStockLine]:
    """Parse a Bobcat stock CSV into typed lines.

    Raises ``BobcatStockError`` on a missing header, missing columns, or
    a row whose availability flag isn't ``Yes`` / ``No``. Duplicate SKUs
    are deduplicated with the *last* occurrence winning, matching how a
    downstream system would process rows sequentially.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise BobcatStockError("Bobcat stock CSV has no header row")

    _require_columns(reader.fieldnames)

    seen: dict[str, BobcatStockLine] = {}
    for row_number, row in enumerate(reader, start=2):
        line = _row_to_line(row, row_number)
        seen[line.sku] = line
    return list(seen.values())


def _require_columns(fieldnames: Iterable[str]) -> None:
    present = {name.strip() for name in fieldnames if name is not None}
    for field, aliases in _COLUMN_ALIASES.items():
        if field == "purchase_price":
            # Optional for v1.
            continue
        if not any(alias in present for alias in aliases):
            aliases_text = " / ".join(repr(a) for a in aliases)
            raise BobcatStockError(
                f"Bobcat stock CSV is missing required column ({aliases_text})"
            )


def _row_to_line(row: dict[str, str], row_number: int) -> BobcatStockLine:
    sku = _field(row, "sku")
    if not sku:
        raise BobcatStockError(f"Row {row_number}: SKU is missing or blank")

    availability_raw = _field(row, "availability")
    if availability_raw is None:
        raise BobcatStockError(
            f"Row {row_number} (SKU {sku}): +10 Available is missing"
        )
    in_stock = _parse_availability(availability_raw, sku, row_number)

    price = _parse_price(_field(row, "purchase_price"))

    return BobcatStockLine(sku=sku, in_stock=in_stock, purchase_price=price)


def _field(row: dict[str, str], field: str) -> str | None:
    for column in _COLUMN_ALIASES[field]:
        raw = row.get(column)
        if raw is None:
            continue
        value = raw.strip()
        if value:
            return value
    return None


def _parse_availability(raw: str, sku: str, row_number: int) -> bool:
    value = raw.strip().lower()
    if value == "yes":
        return True
    if value == "no":
        return False
    raise BobcatStockError(
        f"Row {row_number} (SKU {sku}): unexpected +10 Available value {raw!r} "
        "(expected 'Yes' or 'No')"
    )


def _parse_price(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        # Bad price shouldn't block a stock update; costs are secondary.
        return None
