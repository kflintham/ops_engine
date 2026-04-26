"""Brightpearl queries specific to the GBR JIT integration.

Each function here wraps one Brightpearl endpoint we call and documents the
assumed request/response shape. These are the functions most likely to need
tweaking once we run against the live account -- Brightpearl's concrete
endpoint shapes can differ per account and per API version. Keeping them
isolated in one file means any corrections land here and nowhere else.

The functions take an already-constructed :class:`BrightpearlClient` and
return plain Python data, so they're trivially fakeable in tests by
substituting the client.
"""
from __future__ import annotations

from typing import Any, Mapping

from ops_engine.core.brightpearl import BrightpearlClient


def search_jit_pos_awaiting_send(
    bp: BrightpearlClient,
    *,
    supplier_contact_id: int,
    status_id_request_sent: int,
) -> list[int]:
    """Return the PO IDs that should be sent to Gardiners JIT.

    Assumed endpoint: ``GET /order-service/order-search`` with filters on
    order type, status, and supplier contact. Brightpearl's order-search
    typically returns a ``{"metaData": ..., "results": [[...], ...]}`` shape
    where each result row is a list of column values; the first column is
    the order ID. If the shape differs in the live account, adjust the
    response handling below.
    """
    response = bp.get(
        "/order-service/order-search",
        params={
            "orderTypeCode": "PO",
            "orderStatusId": status_id_request_sent,
            "supplierContactId": supplier_contact_id,
        },
    )
    return _extract_order_ids(response)


def get_order(bp: BrightpearlClient, order_id: int) -> Mapping[str, Any]:
    """Fetch a single purchase order with all its rows.

    Assumed endpoint: ``GET /order-service/order/{id}``. Returns the order
    object; Brightpearl sometimes wraps single-GET responses in a one-element
    list, which is normalised here.
    """
    response = bp.get(f"/order-service/order/{order_id}")
    if isinstance(response, list) and response:
        response = response[0]
    if not isinstance(response, Mapping):
        raise RuntimeError(
            f"Unexpected order payload for {order_id}: {type(response).__name__}"
        )
    return response


def get_product_supplier_ids(
    bp: BrightpearlClient, product_ids: list[int]
) -> dict[int, list[int]]:
    """Return the supplier contact IDs associated with each product.

    Endpoint: ``GET /product-service/product/{ids}/supplier``. Brightpearl
    returns a dict keyed by product ID; the value is either a list of plain
    integer contact IDs (the common shape) or a list of objects with a
    ``supplierId`` / ``contactId`` field. Both shapes are handled.
    """
    if not product_ids:
        return {}
    path = f"/product-service/product/{_csv_ids(product_ids)}/supplier"
    response = bp.get(path) or {}
    result: dict[int, list[int]] = {}
    if isinstance(response, Mapping):
        for product_id_str, entries in response.items():
            product_id = _as_int(product_id_str)
            supplier_ids: list[int] = []
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, bool):
                        continue
                    if isinstance(entry, int):
                        supplier_ids.append(entry)
                    elif isinstance(entry, str) and entry.strip().isdigit():
                        supplier_ids.append(int(entry.strip()))
                    elif isinstance(entry, Mapping):
                        raw = (
                            entry.get("supplierId")
                            or entry.get("contactId")
                            or entry.get("id")
                        )
                        if raw is not None:
                            try:
                                supplier_ids.append(_as_int(raw))
                            except ValueError:
                                continue
            result[product_id] = supplier_ids
    for product_id in product_ids:
        result.setdefault(product_id, [])
    return result


def get_product_gardiners_skus(
    bp: BrightpearlClient,
    product_ids: list[int],
    *,
    price_list_id: int,
) -> dict[int, str | None]:
    """Return the Gardiners SKU for each product.

    TEMPORARY: this currently returns ``product.identity.sku`` (the WBYS
    internal SKU). The intended source -- the per-price-list SKU field on
    the ``Cost Price GBR (Net)`` price list, labelled "Optional supplier
    or customer-specific product code" in the Brightpearl UI -- doesn't
    appear to be exposed by the obvious public-API endpoints on this
    account (``/product-service/product/{id}/price`` 404s,
    ``/product-price-service/product-price/{id}`` and the search variant
    both 500). For products whose primary supplier is Gardiners the
    internal SKU matches the Gardiners SKU, so this shortcut works for
    the common case. It will silently send the wrong code for any
    product that's also sourced from a different supplier with a
    different code; revisit once we know which endpoint exposes the
    price-list-specific SKU.

    The ``price_list_id`` parameter is retained on the signature so the
    rest of the integration doesn't need to change when the proper
    lookup lands.
    """
    if not product_ids:
        return {}
    path = f"/product-service/product/{_csv_ids(product_ids)}"
    response = bp.get(path)
    result: dict[int, str | None] = {}
    items = response if isinstance(response, list) else []
    for product in items:
        if not isinstance(product, Mapping):
            continue
        try:
            product_id = _as_int(product.get("id"))
        except ValueError:
            continue
        identity = product.get("identity")
        sku: str | None = None
        if isinstance(identity, Mapping):
            raw = identity.get("sku")
            if isinstance(raw, str) and raw.strip():
                sku = raw.strip()
        result[product_id] = sku
    for product_id in product_ids:
        result.setdefault(product_id, None)
    return result


def set_order_status(
    bp: BrightpearlClient, order_id: int, *, status_id: int
) -> None:
    """Transition a PO to a new custom status.

    Assumed endpoint: ``POST /order-service/order/{id}/status`` with a body
    of ``{"orderStatusId": <id>}``. Some Brightpearl accounts use
    ``PUT`` or a different path shape; adjust here if so.
    """
    bp.post(
        f"/order-service/order/{order_id}/status",
        json={"orderStatusId": status_id},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_order_ids(response: Any) -> list[int]:
    if isinstance(response, Mapping):
        results = response.get("results")
    else:
        results = response
    if not isinstance(results, list):
        return []
    order_ids: list[int] = []
    for row in results:
        if isinstance(row, list) and row:
            candidate = row[0]
        elif isinstance(row, Mapping):
            candidate = row.get("orderId") or row.get("id")
        else:
            continue
        try:
            order_ids.append(_as_int(candidate))
        except ValueError:
            continue
    return order_ids


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"expected int, got bool {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value.strip())
    raise ValueError(f"expected int, got {type(value).__name__}: {value!r}")


def _csv_ids(ids: list[int]) -> str:
    # de-duplicate while preserving order
    seen: set[int] = set()
    unique: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return ",".join(str(i) for i in unique)
