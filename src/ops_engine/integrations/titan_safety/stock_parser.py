"""Parse Titan Safety daily stock XLSX files.

Titan's daily feed is a one-sheet workbook with three columns::

    Product Code | Product Description | Available Qty

Example rows::

    TC01-03 | Quest Size 03 | 35
    TC01-10 | Quest Size 10 | 237

Qty is an actual integer (unlike Bobcat's Yes/No flag) -- we set the
Brightpearl warehouse quantity to exactly what the sheet says.

SKU quirk: Titan include a leading zero on single-digit sizes so
``TC01-03`` on their side maps to ``TC01-3`` on ours (Brightpearl).
``TC01-10`` stays ``TC01-10``. The normaliser converts the final
``-NN`` segment via ``int()`` which naturally drops the zero.

This module is pure: it takes bytes in, returns dataclasses out. It
doesn't touch IMAP, SFTP, or Brightpearl.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterable

from openpyxl import load_workbook


class TitanStockError(Exception):
    """Raised when a Titan stock XLSX can't be parsed."""


@dataclass(frozen=True)
class TitanStockLine:
    sku: str  # Brightpearl-side SKU (leading zero stripped)
    source_sku: str  # Original SKU as it appears in the XLSX
    description: str
    quantity: int


_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "sku": ("Product Code", "SKU"),
    "description": ("Product Description", "Description"),
    "quantity": ("Available Qty", "Quantity", "Qty"),
}


def parse_stock_xlsx(data: bytes) -> list[TitanStockLine]:
    """Parse a Titan stock XLSX from bytes into typed lines.

    Raises ``TitanStockError`` on missing columns, missing header, or a
    row whose quantity isn't an integer. Duplicate SKUs are deduped with
    the last occurrence winning (matches how a downstream system would
    overwrite a prior row).
    """
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 -- surface any openpyxl error
        raise TitanStockError(f"Could not open XLSX: {exc}") from exc

    try:
        ws = wb[wb.sheetnames[0]]
    except IndexError as exc:
        raise TitanStockError("XLSX has no sheets") from exc

    header_row, header_index = _find_header_row(ws)
    seen: dict[str, TitanStockLine] = {}
    for row_number, row in enumerate(
        ws.iter_rows(min_row=header_row + 1, values_only=True),
        start=header_row + 1,
    ):
        line = _row_to_line(row, header_index, row_number)
        if line is None:
            continue
        seen[line.sku] = line
    if not seen:
        raise TitanStockError("XLSX contained no usable stock lines")
    return list(seen.values())


def normalize_sku(source_sku: str) -> str:
    """Strip leading zeros from the final ``-NN`` segment of a SKU.

    - ``TC01-03`` -> ``TC01-3``
    - ``TC02-04`` -> ``TC02-4``
    - ``TC01-10`` -> ``TC01-10`` (unchanged -- no leading zero to strip)
    - ``TC02`` -> ``TC02`` (no trailing segment -- unchanged)
    - ``TC01-03A`` -> ``TC01-03A`` (last segment isn't numeric -- unchanged)
    """
    if "-" not in source_sku:
        return source_sku
    head, _, tail = source_sku.rpartition("-")
    try:
        normalized_tail = str(int(tail))
    except ValueError:
        return source_sku
    return f"{head}-{normalized_tail}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_header_row(ws: Iterable) -> tuple[int, dict[str, int]]:
    """Scan the first few rows looking for one containing the expected columns.

    Returns the 1-indexed row number of the header and a mapping of
    logical name ("sku"/"description"/"quantity") to its column index
    (0-indexed into the row tuple).
    """
    for row_number, row in enumerate(ws.iter_rows(max_row=10, values_only=True), start=1):
        header_index: dict[str, int] = {}
        for col_index, value in enumerate(row):
            if value is None:
                continue
            text = str(value).strip()
            for field, aliases in _HEADER_ALIASES.items():
                if field in header_index:
                    continue
                if text in aliases:
                    header_index[field] = col_index
                    break
        if {"sku", "quantity"}.issubset(header_index.keys()):
            return row_number, header_index
    raise TitanStockError(
        "XLSX header not found; expected columns with 'Product Code' and "
        "'Available Qty' in the first 10 rows"
    )


def _row_to_line(
    row: tuple, header_index: dict[str, int], row_number: int
) -> TitanStockLine | None:
    sku_cell = _cell(row, header_index.get("sku"))
    if sku_cell is None:
        # Blank SKU row -- treat as end-of-data or spacer, not an error.
        return None
    source_sku = str(sku_cell).strip()
    if not source_sku:
        return None

    quantity_cell = _cell(row, header_index.get("quantity"))
    if quantity_cell is None:
        raise TitanStockError(
            f"Row {row_number} (SKU {source_sku}): Available Qty is missing"
        )
    quantity = _parse_quantity(quantity_cell, source_sku, row_number)

    description_cell = _cell(row, header_index.get("description"))
    description = (
        str(description_cell).strip() if description_cell is not None else ""
    )

    return TitanStockLine(
        sku=normalize_sku(source_sku),
        source_sku=source_sku,
        description=description,
        quantity=quantity,
    )


def _cell(row: tuple, column_index: int | None) -> object | None:
    if column_index is None or column_index >= len(row):
        return None
    return row[column_index]


def _parse_quantity(value: object, sku: str, row_number: int) -> int:
    if isinstance(value, bool):
        raise TitanStockError(
            f"Row {row_number} (SKU {sku}): quantity is a boolean, expected int"
        )
    if isinstance(value, int):
        if value < 0:
            raise TitanStockError(
                f"Row {row_number} (SKU {sku}): quantity {value} is negative"
            )
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise TitanStockError(
                f"Row {row_number} (SKU {sku}): quantity {value} is not whole"
            )
        if value < 0:
            raise TitanStockError(
                f"Row {row_number} (SKU {sku}): quantity {value} is negative"
            )
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            qty = int(stripped)
        except ValueError:
            try:
                qty_f = float(stripped)
            except ValueError as exc:
                raise TitanStockError(
                    f"Row {row_number} (SKU {sku}): quantity {value!r} "
                    "is not an integer"
                ) from exc
            if not qty_f.is_integer():
                raise TitanStockError(
                    f"Row {row_number} (SKU {sku}): quantity {value!r} "
                    "is not an integer"
                )
            qty = int(qty_f)
        if qty < 0:
            raise TitanStockError(
                f"Row {row_number} (SKU {sku}): quantity {qty} is negative"
            )
        return qty
    raise TitanStockError(
        f"Row {row_number} (SKU {sku}): unexpected quantity type "
        f"{type(value).__name__}"
    )
