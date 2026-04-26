from __future__ import annotations

from typing import Any

import pytest

from ops_engine.integrations.gardiner_brothers_jit import brightpearl_queries as q


class FakeBrightpearl:
    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self.get_responses: dict[str, Any] = {}

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append((path, dict(params) if params else None))
        return self.get_responses.get(path)

    def post(self, path: str, *, json: Any = None) -> Any:
        self.post_calls.append((path, json))
        return None


# ---------------------------------------------------------------------------
# search_jit_pos_awaiting_send
# ---------------------------------------------------------------------------


def test_search_builds_correct_filter_params() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/order-service/order-search"] = {
        "results": [[100], [101]]
    }
    ids = q.search_jit_pos_awaiting_send(
        bp, supplier_contact_id=4242, status_id_request_sent=101
    )
    assert ids == [100, 101]
    path, params = bp.get_calls[0]
    assert path == "/order-service/order-search"
    assert params == {
        "orderTypeCode": "PO",
        "orderStatusId": 101,
        "supplierContactId": 4242,
    }


def test_search_handles_empty_results() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/order-service/order-search"] = {"results": []}
    assert q.search_jit_pos_awaiting_send(
        bp, supplier_contact_id=1, status_id_request_sent=1
    ) == []


def test_search_accepts_dict_shaped_rows() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/order-service/order-search"] = {
        "results": [{"orderId": 500}, {"id": 501}]
    }
    assert q.search_jit_pos_awaiting_send(
        bp, supplier_contact_id=1, status_id_request_sent=1
    ) == [500, 501]


def test_search_skips_unparseable_rows() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/order-service/order-search"] = {
        "results": [[100], "unexpected", [None]]
    }
    assert q.search_jit_pos_awaiting_send(
        bp, supplier_contact_id=1, status_id_request_sent=1
    ) == [100]


# ---------------------------------------------------------------------------
# get_order
# ---------------------------------------------------------------------------


def test_get_order_returns_mapping() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/order-service/order/123"] = {"id": 123, "orderRows": []}
    order = q.get_order(bp, 123)
    assert order == {"id": 123, "orderRows": []}


def test_get_order_unwraps_single_element_list() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/order-service/order/123"] = [{"id": 123}]
    assert q.get_order(bp, 123) == {"id": 123}


def test_get_order_raises_on_unexpected_shape() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/order-service/order/123"] = "oops"
    with pytest.raises(RuntimeError, match="Unexpected order payload"):
        q.get_order(bp, 123)


# ---------------------------------------------------------------------------
# get_product_supplier_ids
# ---------------------------------------------------------------------------


def test_supplier_ids_handles_empty_product_list() -> None:
    bp = FakeBrightpearl()
    assert q.get_product_supplier_ids(bp, []) == {}


def test_supplier_ids_parses_supplier_entries() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/product-service/product/501,502/supplier"] = {
        "501": [{"supplierId": 4242}, {"supplierId": 9999}],
        "502": [{"contactId": 4242}],
    }
    result = q.get_product_supplier_ids(bp, [501, 502])
    assert result == {501: [4242, 9999], 502: [4242]}


def test_supplier_ids_parses_plain_integer_lists() -> None:
    """Brightpearl's actual shape: a list of bare integer contact IDs."""
    bp = FakeBrightpearl()
    bp.get_responses["/product-service/product/53095/supplier"] = {
        "53095": [12, 13, 14, 341],
    }
    assert q.get_product_supplier_ids(bp, [53095]) == {53095: [12, 13, 14, 341]}


def test_supplier_ids_fills_in_missing_products_with_empty_list() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/product-service/product/501/supplier"] = {}
    assert q.get_product_supplier_ids(bp, [501]) == {501: []}


def test_supplier_ids_deduplicates_input_ids() -> None:
    bp = FakeBrightpearl()
    bp.get_responses["/product-service/product/501/supplier"] = {"501": []}
    q.get_product_supplier_ids(bp, [501, 501, 501])
    path, _ = bp.get_calls[0]
    assert path == "/product-service/product/501/supplier"


# ---------------------------------------------------------------------------
# set_order_status
# ---------------------------------------------------------------------------


def test_set_order_status_posts_expected_body() -> None:
    bp = FakeBrightpearl()
    q.set_order_status(bp, 123, status_id=102)
    assert bp.post_calls == [
        ("/order-service/order/123/status", {"orderStatusId": 102})
    ]
