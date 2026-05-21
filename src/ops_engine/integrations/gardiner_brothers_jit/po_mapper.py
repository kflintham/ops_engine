"""Map a Brightpearl purchase-order payload into an :class:`Order`.

This module is pure: no HTTP, no SFTP, no env vars. It takes a Brightpearl
PO dict and produces an :class:`Order` ready for :func:`build_order_csv`,
or raises :class:`GbrJitMappingError` with a clear message if the PO is
structurally unusable (missing SKU on a line, missing quantity, etc.).

Verified Brightpearl response shapes (from a real PO on the WBYS account):

- ``po["id"]`` -- integer PO ID.
- ``po.get("reference")`` -- optional human reference (also accepts ``ref``);
  falls back to ``str(po["id"])``.
- ``po["orderRows"]`` -- a ``dict`` keyed by row ID (Brightpearl's actual
  shape); a ``list`` of row objects is also accepted defensively.
- ``row["productId"]`` -- integer product ID.
- ``row["productSku"]`` -- the SKU as recorded on the order row at the
  PO's price list. Used directly as the Gardiners SKU.
- ``row["quantity"]["magnitude"]`` -- quantity as a string decimal,
  e.g. ``"2.0000"``.

JIT eligibility note: we deliberately do NOT check each product's
Suppliers tab for Gardiner Bros & Co (B1358) here. The PO's supplier
choice is the source of truth -- if Purchasing raised the PO against
B1358, the lines are intended for B1358. If a SKU is genuinely unknown
to Gardiners' system, they reply with a Cancelled notification which
the inbound pipeline applies as a status transition.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .order_builder import Order, OrderLine


class GbrJitMappingError(Exception):
    """Raised when a Brightpearl PO cannot be mapped into a JIT order."""


def build_order_from_po(po: Mapping[str, Any]) -> Order:
    po_id = _require_int(po, "id")
    reference = _order_reference(po)
    rows = _iter_order_rows(po)

    lines: list[OrderLine] = []
    errors: list[str] = []

    for row_id, row in rows:
        try:
            sku = _resolve_row_sku(row, ctx=f"row {row_id}")
            quantity = _parse_quantity(row, ctx=f"row {row_id}")
        except GbrJitMappingError as exc:
            errors.append(str(exc))
            continue

        lines.append(
            OrderLine(
                sku=sku,
                quantity=quantity,
                line_reference=f"{po_id}-{row_id}",
            )
        )

    if errors:
        raise GbrJitMappingError(
            f"PO {po_id} cannot be sent as a JIT order: " + "; ".join(errors)
        )
    if not lines:
        raise GbrJitMappingError(f"PO {po_id} has no order lines")

    return Order(reference=reference, lines=tuple(lines))


# ---------------------------------------------------------------------------
# Helpers -- each one owns a single assumption about Brightpearl's data.
# ---------------------------------------------------------------------------


def _order_reference(po: Mapping[str, Any]) -> str:
    for key in ("ref", "reference"):
        ref = po.get(key)
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    return str(_require_int(po, "id"))


def _iter_order_rows(
    po: Mapping[str, Any],
) -> list[tuple[int, Mapping[str, Any]]]:
    raw = po.get("orderRows")
    if raw is None:
        raise GbrJitMappingError(f"PO {po.get('id')} has no orderRows field")

    rows: list[tuple[int, Mapping[str, Any]]] = []
    if isinstance(raw, Mapping):
        for key, row in raw.items():
            rows.append((_coerce_int(key, "orderRows key"), row))
    elif isinstance(raw, list):
        for row in raw:
            row_id = _require_int(row, "id", ctx="orderRows list item")
            rows.append((row_id, row))
    else:
        raise GbrJitMappingError(
            f"orderRows has unexpected type {type(raw).__name__}"
        )
    return rows


def _resolve_row_sku(row: Mapping[str, Any], *, ctx: str) -> str:
    sku = row.get("productSku")
    if not isinstance(sku, str) or not sku.strip():
        raise GbrJitMappingError(f"{ctx}: missing productSku")
    return sku.strip()


def _parse_quantity(row: Mapping[str, Any], *, ctx: str) -> int:
    quantity_obj = row.get("quantity") or {}
    magnitude = quantity_obj.get("magnitude") if isinstance(quantity_obj, Mapping) else None
    if magnitude is None:
        raise GbrJitMappingError(f"{ctx}: missing quantity.magnitude")
    try:
        decimal = Decimal(str(magnitude))
    except (InvalidOperation, ValueError) as exc:
        raise GbrJitMappingError(
            f"{ctx}: quantity {magnitude!r} is not a number"
        ) from exc
    if decimal != decimal.to_integral_value():
        raise GbrJitMappingError(
            f"{ctx}: quantity {magnitude!r} is not an integer"
        )
    quantity = int(decimal)
    if quantity <= 0:
        raise GbrJitMappingError(f"{ctx}: quantity must be positive")
    return quantity


def _require_int(
    obj: Mapping[str, Any], key: str, *, ctx: str | None = None
) -> int:
    if key not in obj:
        where = f" ({ctx})" if ctx else ""
        raise GbrJitMappingError(f"missing required field {key!r}{where}")
    return _coerce_int(obj[key], key)


def _coerce_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise GbrJitMappingError(f"{field!r} is boolean, expected int")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    raise GbrJitMappingError(f"{field!r} has non-integer value {value!r}")
