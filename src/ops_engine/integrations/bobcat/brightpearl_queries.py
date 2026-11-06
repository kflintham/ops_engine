"""Brightpearl queries specific to the Bobcat stock feed.

Each function here wraps one Brightpearl endpoint. Keeping them isolated
means any shape fix lands in one place -- same pattern as the Gardiners
integration. Live response shapes vary between accounts, so each function
documents the assumed shape and the fallback it tolerates.

Endpoints used (all documented at
https://api-docs.brightpearl.com/):

- ``POST /product-service/product-search`` to resolve SKU -> productId
- ``GET /warehouse-service/product-availability/{productIds}`` to read
  current stock levels per warehouse
- ``POST /warehouse-service/warehouse/{wh}/stock-correction`` to apply
  deltas against a warehouse

Brightpearl's warehouse / stock APIs do not have a "set absolute level"
primitive. Everything is expressed as a stock *correction* -- a signed
delta applied against a reason code. We therefore read the current level
first and POST the delta needed to reach the target.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ops_engine.core.brightpearl import BrightpearlClient


# Brightpearl limits URL path segments (CMNC-006) so we batch multi-ID
# requests the same way the Gardiners integration does.
_MULTI_ID_BATCH_SIZE = 50


# Default stock-correction reason. 1 == "Found" on most Brightpearl
# accounts; override at the call site if the WBYS account uses something
# else.
DEFAULT_CORRECTION_REASON = 1


def search_product_ids_by_sku(
    bp: BrightpearlClient, skus: Iterable[str]
) -> dict[str, int]:
    """Resolve each SKU to its Brightpearl product ID.

    Uses ``GET /product-service/product-search?SKU=...``. The search is
    exact-match on SKU and (per Brightpearl docs) returns a
    ``{"metaData": ..., "results": [...]}`` table where column metadata
    tells us which column holds the product ID and which holds the SKU.
    We tolerate the alternative plain-list shape some endpoints use.

    SKUs not found in Brightpearl are simply absent from the returned
    dict; the caller decides what to do (currently: log + skip).
    """
    unique = _dedupe_preserve_order(skus)
    found: dict[str, int] = {}
    for sku in unique:
        product_id = _search_one_sku(bp, sku)
        if product_id is not None:
            found[sku] = product_id
    return found


def _search_one_sku(bp: BrightpearlClient, sku: str) -> int | None:
    response = bp.get(
        "/product-service/product-search",
        params={"SKU": sku},
    )
    rows = _result_rows(response)
    columns = _result_columns(response)
    for row in rows:
        row_map = _row_to_map(row, columns)
        row_sku = _first(row_map, ("SKU", "sku"))
        if row_sku is None or str(row_sku).strip() != sku:
            continue
        pid = _first(row_map, ("productId", "productID", "id"))
        try:
            return _as_int(pid)
        except ValueError:
            continue
    return None


def get_warehouse_stock_levels(
    bp: BrightpearlClient, product_ids: list[int], *, warehouse_id: int
) -> dict[int, int]:
    """Return current on-hand stock per product ID for one warehouse.

    Endpoint: ``GET /warehouse-service/product-availability/{ids}``.
    Response is a dict keyed by product-ID-as-string; each value carries
    per-warehouse quantities under ``warehouses[warehouseId].inStock``.
    Missing entries are treated as zero. Products with no stock record
    for the warehouse are absent from the returned dict so the caller
    can tell "no record" from "explicitly zero".
    """
    if not product_ids:
        return {}

    unique_ids = _dedupe_preserve_order(product_ids)
    levels: dict[int, int] = {}
    for offset in range(0, len(unique_ids), _MULTI_ID_BATCH_SIZE):
        batch = unique_ids[offset : offset + _MULTI_ID_BATCH_SIZE]
        path = f"/warehouse-service/product-availability/{_csv_ids(batch)}"
        response = bp.get(path) or {}
        if not isinstance(response, Mapping):
            continue
        for product_id_str, payload in response.items():
            try:
                product_id = _as_int(product_id_str)
            except ValueError:
                continue
            quantity = _extract_warehouse_quantity(payload, warehouse_id)
            if quantity is not None:
                levels[product_id] = quantity
    return levels


def list_warehouse_product_ids(
    bp: BrightpearlClient, *, warehouse_id: int
) -> list[int]:
    """Return every product ID that currently has stock in the warehouse.

    Endpoint: ``GET /warehouse-service/warehouse/{wh}/stock``. This is
    how we find the products we need to zero out when they fall off the
    Bobcat feed. Brightpearl returns either a mapping keyed by product ID
    or a list of ``{"productId": ..., ...}`` rows -- we accept both.

    NOTE: Brightpearl's live API doesn't actually expose this exact path
    on all accounts (the WBYS account returns 404 for it). The zero-out-
    on-absence feature in :mod:`stock_sync` therefore catches any error
    this function raises and falls back to only setting stock for SKUs
    present in today's feed. Ongoing: find the correct endpoint
    (likely a stock-search or report).
    """
    response = bp.get(f"/warehouse-service/warehouse/{warehouse_id}/stock")
    if response is None:
        return []
    ids: list[int] = []
    if isinstance(response, Mapping):
        for key in response.keys():
            try:
                ids.append(_as_int(key))
            except ValueError:
                continue
        return ids
    if isinstance(response, list):
        for row in response:
            if isinstance(row, Mapping):
                raw = row.get("productId") or row.get("id")
                try:
                    ids.append(_as_int(raw))
                except ValueError:
                    continue
    return ids


@dataclass(frozen=True)
class StockCorrection:
    """One line in a batched stock-correction POST."""

    product_id: int
    location_id: int
    quantity_delta: int
    cost_value: str
    currency: str
    reason_id: int = DEFAULT_CORRECTION_REASON
    note: str = ""


# Brightpearl accepts many corrections in one POST. Keeping the batch
# modest avoids hitting their rate limits and keeps request bodies sane.
STOCK_CORRECTION_BATCH_SIZE = 50


def apply_stock_corrections(
    bp: BrightpearlClient,
    *,
    warehouse_id: int,
    corrections: list[StockCorrection],
) -> None:
    """POST a batch of stock corrections to one warehouse.

    Endpoint: ``POST /warehouse-service/warehouse/{wh}/stock-correction``
    with a body shaped like::

        {"corrections": [
            {
                "quantity": <delta>,
                "productId": <pid>,
                "locationId": <locId>,
                "cost": {"value": "<GBP cost>", "currency": "GBP"},
                "reason": <reasonId>,
                "reference": "<note>"
            },
            ...
        ]}

    Brightpearl requires locationId and cost (value + currency) on every
    correction. Zero-delta entries are dropped before send. The list is
    split into batches of ``STOCK_CORRECTION_BATCH_SIZE`` so a large
    sync doesn't trigger 503s.
    """
    non_zero = [c for c in corrections if c.quantity_delta != 0]
    if not non_zero:
        return
    for offset in range(0, len(non_zero), STOCK_CORRECTION_BATCH_SIZE):
        batch = non_zero[offset : offset + STOCK_CORRECTION_BATCH_SIZE]
        body = {
            "corrections": [
                {
                    "quantity": c.quantity_delta,
                    "productId": c.product_id,
                    "locationId": c.location_id,
                    "cost": {"value": c.cost_value, "currency": c.currency},
                    "reason": c.reason_id,
                    "reference": c.note,
                }
                for c in batch
            ]
        }
        bp.post(
            f"/warehouse-service/warehouse/{warehouse_id}/stock-correction",
            json=body,
        )


# ---------------------------------------------------------------------------
# Response-parsing helpers
# ---------------------------------------------------------------------------


def _result_rows(response: Any) -> list[Any]:
    if isinstance(response, Mapping):
        results = response.get("results")
        if isinstance(results, list):
            return results
    if isinstance(response, list):
        return response
    return []


def _result_columns(response: Any) -> list[str]:
    if not isinstance(response, Mapping):
        return []
    meta = response.get("metaData")
    if not isinstance(meta, Mapping):
        return []
    columns = meta.get("columns")
    if not isinstance(columns, list):
        return []
    names: list[str] = []
    for col in columns:
        if isinstance(col, Mapping):
            name = col.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _row_to_map(row: Any, columns: list[str]) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if isinstance(row, list) and columns and len(row) == len(columns):
        return dict(zip(columns, row))
    return {}


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _extract_warehouse_quantity(payload: Any, warehouse_id: int) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    warehouses = payload.get("warehouses")
    if not isinstance(warehouses, Mapping):
        return None
    wh = warehouses.get(str(warehouse_id)) or warehouses.get(warehouse_id)
    if not isinstance(wh, Mapping):
        return None
    for key in ("inStock", "onHand", "quantity"):
        value = wh.get(key)
        if value is not None:
            try:
                return _as_int(value)
            except ValueError:
                continue
    return None


def _dedupe_preserve_order(items: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"expected int, got bool {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        s = value.strip()
        # Tolerate "10.000000" style
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError as exc:
                raise ValueError(
                    f"expected int, got str {value!r}"
                ) from exc
            if f.is_integer():
                return int(f)
            raise ValueError(f"expected int, got str {value!r}")
    raise ValueError(f"expected int, got {type(value).__name__}: {value!r}")


def _csv_ids(ids: list[int]) -> str:
    # Sorted + deduped: Brightpearl rejects multi-ID paths whose IDs are
    # not in ascending order (CMNC-006 "is not in order").
    return ",".join(str(i) for i in sorted(set(ids)))
