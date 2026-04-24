"""Build the Gardiner Brothers JIT order CSV from an in-memory order.

The format is documented in
``docs/gardiner-brothers-jit/process-docs/order-file-requirements.pdf`` and
mirrored by the sample at
``docs/gardiner-brothers-jit/samples/order-file-template.csv``.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    line_reference: str


@dataclass(frozen=True)
class Order:
    reference: str
    lines: tuple[OrderLine, ...]


ORDER_CSV_COLUMNS: tuple[str, ...] = (
    "SKU",
    "Quantity",
    "Order Reference",
    "Order Line Reference",
)


def build_order_csv(order: Order) -> str:
    if not order.reference:
        raise ValueError("Order reference is required")
    if not order.lines:
        raise ValueError("Order must contain at least one line")

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(ORDER_CSV_COLUMNS)
    for line in order.lines:
        if not line.sku:
            raise ValueError("Order line SKU is required")
        if line.quantity <= 0:
            raise ValueError("Order line quantity must be a positive integer")
        if not line.line_reference:
            raise ValueError("Order line reference is required")
        writer.writerow(
            (line.sku, line.quantity, order.reference, line.line_reference)
        )
    return buffer.getvalue()
