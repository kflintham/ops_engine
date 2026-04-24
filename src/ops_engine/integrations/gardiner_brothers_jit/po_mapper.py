"""Map a Brightpearl purchase-order payload into an :class:`Order`.

This module is pure: no HTTP, no SFTP, no env vars. It takes a Brightpearl
PO dict plus two pre-fetched lookup maps (product -> supplier IDs, product
-> Gardiners SKU) and produces an :class:`Order` ready for
:func:`build_order_csv`, or raises :class:`GbrJitMappingError` with a clear
message if the PO violates any of the JIT business rules recorded in
``docs/gardiner-brothers-jit/field-mapping.md``.

Brightpearl API response shape notes (treat as working assumptions;
verify against real data at implementation time):

- ``po["id"]`` -- integer PO ID.
- ``po.get("ref")`` -- optional human reference; falls back to ``str(po["id"])``.
- ``po["orderRows"]`` -- either a ``dict`` keyed by row ID (common) or a
  ``list`` of row objects. Normalised below.
- ``row["id"]`` -- integer row ID (when rows are a list); when rows are a
  dict, the key is the row ID.
- ``row["productId"]`` -- integer product ID.
- ``row["productQuantity"]["magnitude"]`` -- quantity as a string decimal,
  e.g. ``"1.000000"``.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .order_builder import Order, OrderLine


class GbrJitMappingError(Exception):
    """Raised when a Brightpearl PO cannot be mapped into a JIT order."""


def build_order_from_po(
    po: Mapping[str, Any],
    *,
    product_supplier_ids: Mapping[int, list[int]],
    product_gardiners_skus: Mapping[int, str | None],
    required_supplier_contact_id: int,
) -> Order:
    po_id = _require_int(po, "id")
    reference = _order_reference(po)
    rows = _iter_order_rows(po)

    lines: list[OrderLine] = []
    errors: list[str] = []

    for row_id, row in rows:
        product_id = _require_int(row, "productId", ctx=f"row {row_id}")
        try:
            _assert_product_has_supplier(
                product_id,
                product_supplier_ids,
                required_supplier_contact_id,
            )
            sku = _resolve_gardiners_sku(product_id, product_gardiners_skus)
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
    ref = po.get("ref")
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


def _assert_product_has_supplier(
    product_id: int,
    product_supplier_ids: Mapping[int, list[int]],
    required_supplier_contact_id: int,
) -> None:
    supplier_ids = product_supplier_ids.get(product_id)
    if supplier_ids is None:
        raise GbrJitMappingError(
            f"product {product_id} has no supplier information available"
        )
    if required_supplier_contact_id not in supplier_ids:
        raise GbrJitMappingError(
            f"product {product_id} does not list supplier "
            f"{required_supplier_contact_id} (Gardiner Bros JIT, B1358)"
        )


def _resolve_gardiners_sku(
    product_id: int,
    product_gardiners_skus: Mapping[int, str | None],
) -> str:
    sku = product_gardiners_skus.get(product_id)
    if not sku or not sku.strip():
        raise GbrJitMappingError(
            f"product {product_id} has no SKU on the Gardiners price list"
        )
    return sku.strip()


def _parse_quantity(row: Mapping[str, Any], *, ctx: str) -> int:
    quantity_obj = row.get("productQuantity") or {}
    magnitude = quantity_obj.get("magnitude") if isinstance(quantity_obj, Mapping) else None
    if magnitude is None:
        raise GbrJitMappingError(f"{ctx}: missing productQuantity.magnitude")
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
