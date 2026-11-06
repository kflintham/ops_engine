from __future__ import annotations

from typing import Any

import pytest

from ops_engine.integrations.bobcat import brightpearl_queries as queries


class FakeBrightpearl:
    def __init__(self, get_responses: dict[str, Any] | None = None) -> None:
        self.get_responses = get_responses or {}
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, Any]] = []

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append((path, dict(params) if params else None))
        if path in self.get_responses:
            return self.get_responses[path]
        # Simulate per-param lookup (product-search returns different data
        # per SKU param).
        if params and "SKU" in params:
            key = f"{path}?SKU={params['SKU']}"
            return self.get_responses.get(key)
        return None

    def post(self, path: str, *, json: Any = None) -> Any:
        self.post_calls.append((path, json))
        return None


# ---------------------------------------------------------------------------
# search_product_ids_by_sku
# ---------------------------------------------------------------------------


def _search_response_for(rows: list[tuple[int, str]]) -> dict[str, Any]:
    return {
        "metaData": {
            "columns": [{"name": "productId"}, {"name": "SKU"}]
        },
        "results": [[pid, sku] for pid, sku in rows],
    }


def test_search_product_ids_returns_mapping() -> None:
    bp = FakeBrightpearl(
        get_responses={
            "/product-service/product-search?SKU=A|B": _search_response_for(
                [(500, "A"), (501, "B")]
            ),
        }
    )
    result = queries.search_product_ids_by_sku(bp, ["A", "B"])
    assert result == {"A": 500, "B": 501}


def test_search_product_ids_skips_unknown() -> None:
    bp = FakeBrightpearl(
        get_responses={
            "/product-service/product-search?SKU=A|B": _search_response_for(
                [(500, "A")]
            ),
        }
    )
    result = queries.search_product_ids_by_sku(bp, ["A", "B"])
    assert result == {"A": 500}


def test_search_product_ids_requires_exact_sku_match() -> None:
    # Brightpearl's OR filter can match a different SKU in the same row
    # pattern; our wrapper filters results back to the SKUs we asked for.
    bp = FakeBrightpearl(
        get_responses={
            "/product-service/product-search?SKU=A": _search_response_for(
                [(500, "A-SIMILAR")]
            ),
        }
    )
    result = queries.search_product_ids_by_sku(bp, ["A"])
    assert result == {}


def test_search_product_ids_deduplicates_input() -> None:
    bp = FakeBrightpearl(
        get_responses={
            "/product-service/product-search?SKU=A": _search_response_for(
                [(500, "A")]
            ),
        }
    )
    queries.search_product_ids_by_sku(bp, ["A", "A", "A"])
    calls = [c for c in bp.get_calls if c[0] == "/product-service/product-search"]
    assert len(calls) == 1


def test_search_product_ids_batches_big_lists_into_pipe_separated_calls() -> None:
    bp = FakeBrightpearl()
    skus = [f"SKU{i}" for i in range(queries.PRODUCT_SEARCH_SKU_BATCH_SIZE + 5)]
    queries.search_product_ids_by_sku(bp, skus)
    # Expect two calls: one with 50 pipe-joined SKUs, one with 5.
    search_calls = [c for c in bp.get_calls if c[0] == "/product-service/product-search"]
    assert len(search_calls) == 2
    first_filter = search_calls[0][1]["SKU"]
    assert first_filter.count("|") == queries.PRODUCT_SEARCH_SKU_BATCH_SIZE - 1
    second_filter = search_calls[1][1]["SKU"]
    assert second_filter.count("|") == 4


# ---------------------------------------------------------------------------
# get_warehouse_stock_levels
# ---------------------------------------------------------------------------


def test_warehouse_stock_levels_reads_in_stock() -> None:
    bp = FakeBrightpearl(
        get_responses={
            "/warehouse-service/product-availability/500,501": {
                "500": {
                    "warehouses": {"14": {"inStock": 10}},
                },
                "501": {
                    "warehouses": {"14": {"inStock": 0}, "2": {"inStock": 5}},
                },
            },
        }
    )
    levels = queries.get_warehouse_stock_levels(bp, [500, 501], warehouse_id=14)
    assert levels == {500: 10, 501: 0}


def test_warehouse_stock_levels_omits_products_without_record() -> None:
    bp = FakeBrightpearl(
        get_responses={
            "/warehouse-service/product-availability/500,501": {
                "500": {"warehouses": {"14": {"inStock": 10}}},
                "501": {"warehouses": {"2": {"inStock": 5}}},  # not in wh 14
            },
        }
    )
    levels = queries.get_warehouse_stock_levels(bp, [500, 501], warehouse_id=14)
    assert levels == {500: 10}


def test_warehouse_stock_levels_batches_product_ids() -> None:
    # 120 IDs should batch into 50 + 50 + 20.
    bp = FakeBrightpearl()
    product_ids = list(range(1, 121))

    captured_paths: list[str] = []
    original_get = bp.get

    def capture_get(path: str, *, params: Any = None) -> Any:
        captured_paths.append(path)
        return {}

    bp.get = capture_get  # type: ignore[assignment]
    queries.get_warehouse_stock_levels(bp, product_ids, warehouse_id=14)
    assert len(captured_paths) == 3


def test_warehouse_stock_levels_empty_input() -> None:
    bp = FakeBrightpearl()
    assert queries.get_warehouse_stock_levels(bp, [], warehouse_id=14) == {}


def test_warehouse_stock_levels_sorts_batched_ids() -> None:
    bp = FakeBrightpearl()
    captured_paths: list[str] = []

    def capture_get(path: str, *, params: Any = None) -> Any:
        captured_paths.append(path)
        return {}

    bp.get = capture_get  # type: ignore[assignment]
    queries.get_warehouse_stock_levels(bp, [3, 1, 2], warehouse_id=14)
    assert captured_paths == [
        "/warehouse-service/product-availability/1,2,3"
    ]


# ---------------------------------------------------------------------------
# list_warehouse_product_ids
# ---------------------------------------------------------------------------


def test_list_warehouse_product_ids_handles_mapping_shape() -> None:
    bp = FakeBrightpearl(
        get_responses={
            "/warehouse-service/warehouse/14/stock": {
                "500": {"someField": 1},
                "501": {"someField": 2},
            },
        }
    )
    ids = queries.list_warehouse_product_ids(bp, warehouse_id=14)
    assert sorted(ids) == [500, 501]


def test_list_warehouse_product_ids_handles_list_shape() -> None:
    bp = FakeBrightpearl(
        get_responses={
            "/warehouse-service/warehouse/14/stock": [
                {"productId": 500},
                {"productId": 501},
            ],
        }
    )
    ids = queries.list_warehouse_product_ids(bp, warehouse_id=14)
    assert sorted(ids) == [500, 501]


def test_list_warehouse_product_ids_handles_empty() -> None:
    bp = FakeBrightpearl(
        get_responses={"/warehouse-service/warehouse/14/stock": None}
    )
    assert queries.list_warehouse_product_ids(bp, warehouse_id=14) == []


# ---------------------------------------------------------------------------
# apply_stock_correction
# ---------------------------------------------------------------------------


def _correction(**overrides: Any) -> queries.StockCorrection:
    base = dict(
        product_id=500,
        location_id=27,
        quantity_delta=10,
        cost_value="54.79",
        currency="GBP",
    )
    base.update(overrides)
    return queries.StockCorrection(**base)


def test_apply_stock_corrections_posts_the_whole_batch() -> None:
    bp = FakeBrightpearl()
    queries.apply_stock_corrections(
        bp,
        warehouse_id=14,
        corrections=[
            _correction(product_id=500, quantity_delta=10, note="test"),
            _correction(product_id=501, quantity_delta=-3, note="test"),
        ],
    )
    assert bp.post_calls == [
        (
            "/warehouse-service/warehouse/14/stock-correction",
            {
                "corrections": [
                    {
                        "quantity": 10,
                        "productId": 500,
                        "locationId": 27,
                        "cost": {"value": "54.79", "currency": "GBP"},
                        "reason": queries.DEFAULT_CORRECTION_REASON,
                        "reference": "test",
                    },
                    {
                        "quantity": -3,
                        "productId": 501,
                        "locationId": 27,
                        "cost": {"value": "54.79", "currency": "GBP"},
                        "reason": queries.DEFAULT_CORRECTION_REASON,
                        "reference": "test",
                    },
                ]
            },
        )
    ]


def test_apply_stock_corrections_drops_zero_delta_entries() -> None:
    bp = FakeBrightpearl()
    queries.apply_stock_corrections(
        bp,
        warehouse_id=14,
        corrections=[
            _correction(product_id=500, quantity_delta=0),
            _correction(product_id=501, quantity_delta=5),
        ],
    )
    posted = bp.post_calls[0][1]["corrections"]
    assert [c["productId"] for c in posted] == [501]


def test_apply_stock_corrections_empty_or_all_zero_is_noop() -> None:
    bp = FakeBrightpearl()
    queries.apply_stock_corrections(bp, warehouse_id=14, corrections=[])
    queries.apply_stock_corrections(
        bp,
        warehouse_id=14,
        corrections=[_correction(quantity_delta=0)],
    )
    assert bp.post_calls == []


def test_apply_stock_corrections_splits_into_batches() -> None:
    bp = FakeBrightpearl()
    total = queries.STOCK_CORRECTION_BATCH_SIZE * 2 + 5
    corrections = [
        _correction(product_id=pid, quantity_delta=1)
        for pid in range(1, total + 1)
    ]
    queries.apply_stock_corrections(
        bp, warehouse_id=14, corrections=corrections
    )
    # 50 + 50 + 5 = 3 POSTs.
    assert len(bp.post_calls) == 3
    assert len(bp.post_calls[0][1]["corrections"]) == queries.STOCK_CORRECTION_BATCH_SIZE
    assert len(bp.post_calls[-1][1]["corrections"]) == 5
