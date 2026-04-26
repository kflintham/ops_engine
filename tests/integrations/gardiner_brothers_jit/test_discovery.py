from __future__ import annotations

from typing import Any

from ops_engine.integrations.gardiner_brothers_jit import discovery


class FakeBrightpearl:
    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, dict(params) if params else None))
        return self.responses.get(path)


# ---------------------------------------------------------------------------
# Supplier lookup
# ---------------------------------------------------------------------------


def test_finds_supplier_contact_id_in_results_list_of_lists() -> None:
    bp = FakeBrightpearl()
    bp.responses["/contact-service/contact-search"] = {
        "metaData": {
            "columns": [
                {"name": "contactId"},
                {"name": "companyName"},
            ]
        },
        "results": [
            [9999, "Gardiner Bros & Co (B3116) DF"],
            [4242, "Gardiner Bros & Co (B1358)"],
        ],
    }
    assert discovery.find_supplier_id(
        bp, "Gardiner Bros & Co (B1358)"
    ) == 4242


def test_finds_supplier_contact_id_in_results_list_of_dicts() -> None:
    bp = FakeBrightpearl()
    bp.responses["/contact-service/contact-search"] = {
        "results": [
            {"contactId": 4242, "companyName": "Gardiner Bros & Co (B1358)"},
        ]
    }
    assert discovery.find_supplier_id(
        bp, "Gardiner Bros & Co (B1358)"
    ) == 4242


def test_returns_none_when_supplier_not_found() -> None:
    bp = FakeBrightpearl()
    bp.responses["/contact-service/contact-search"] = {"results": []}
    assert (
        discovery.find_supplier_id(bp, "Gardiner Bros & Co (B1358)") is None
    )


# ---------------------------------------------------------------------------
# Price list lookup
# ---------------------------------------------------------------------------


def test_finds_price_list_in_top_level_list() -> None:
    bp = FakeBrightpearl()
    bp.responses["/product-service/price-list"] = [
        {"id": 1, "name": "Default"},
        {"id": 7, "name": "Cost Price GBR (Net)"},
    ]
    assert discovery.find_price_list_id(bp, "Cost Price GBR (Net)") == 7


def test_finds_price_list_when_keyed_by_id() -> None:
    bp = FakeBrightpearl()
    bp.responses["/product-service/price-list"] = {
        "1": {"name": "Default"},
        "7": {"name": "Cost Price GBR (Net)"},
    }
    assert discovery.find_price_list_id(bp, "Cost Price GBR (Net)") == 7


# ---------------------------------------------------------------------------
# Order status lookup
# ---------------------------------------------------------------------------


def test_finds_order_status_id() -> None:
    bp = FakeBrightpearl()
    bp.responses["/order-service/order-status"] = [
        {"id": 100, "name": "Open"},
        {"id": 101, "name": "GBR JIT - Request Sent"},
        {"id": 102, "name": "GBR JIT - Pending"},
    ]
    assert (
        discovery.find_order_status_id(bp, "GBR JIT - Request Sent") == 101
    )
    assert discovery.find_order_status_id(bp, "GBR JIT - Pending") == 102


# ---------------------------------------------------------------------------
# Top-level discover()
# ---------------------------------------------------------------------------


def test_discover_returns_complete_result_when_everything_found() -> None:
    bp = FakeBrightpearl()
    bp.responses["/contact-service/contact-search"] = {
        "results": [
            {"contactId": 4242, "companyName": "Gardiner Bros & Co (B1358)"}
        ]
    }
    bp.responses["/product-service/price-list"] = [
        {"id": 7, "name": "Cost Price GBR (Net)"}
    ]
    bp.responses["/order-service/order-status"] = [
        {"id": 101, "name": "GBR JIT - Request Sent"},
        {"id": 102, "name": "GBR JIT - Pending"},
    ]
    result = discovery.discover(bp)
    assert result.supplier_contact_id == 4242
    assert result.price_list_id == 7
    assert result.status_id_request_sent == 101
    assert result.status_id_pending == 102
    assert result.is_complete is True


def test_discover_partial_results_is_marked_incomplete() -> None:
    bp = FakeBrightpearl()
    bp.responses["/contact-service/contact-search"] = {"results": []}
    bp.responses["/product-service/price-list"] = [
        {"id": 7, "name": "Cost Price GBR (Net)"}
    ]
    bp.responses["/order-service/order-status"] = []
    result = discovery.discover(bp)
    assert result.price_list_id == 7
    assert result.supplier_contact_id is None
    assert result.is_complete is False


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------


def test_format_env_snippet_complete() -> None:
    result = discovery.DiscoveryResult(
        supplier_contact_id=4242,
        price_list_id=7,
        status_id_request_sent=101,
        status_id_pending=102,
    )
    snippet = discovery.format_env_snippet(result)
    assert "GBR_JIT_SUPPLIER_CONTACT_ID=4242" in snippet
    assert "GBR_JIT_PRICE_LIST_ID=7" in snippet
    assert "GBR_JIT_STATUS_ID_REQUEST_SENT=101" in snippet
    assert "GBR_JIT_STATUS_ID_PENDING=102" in snippet


def test_format_env_snippet_marks_missing_values() -> None:
    result = discovery.DiscoveryResult(
        supplier_contact_id=None,
        price_list_id=7,
        status_id_request_sent=None,
        status_id_pending=None,
    )
    snippet = discovery.format_env_snippet(result)
    assert snippet.count("NOT FOUND") == 3
    assert "GBR_JIT_PRICE_LIST_ID=7" in snippet


# ---------------------------------------------------------------------------
# Candidate listing when something isn't found by exact match
# ---------------------------------------------------------------------------


def test_discover_lists_price_lists_when_target_not_found() -> None:
    bp = FakeBrightpearl()
    bp.responses["/contact-service/contact-search"] = {
        "results": [
            {"contactId": 4242, "companyName": "Gardiner Bros & Co (B1358)"}
        ]
    }
    bp.responses["/product-service/price-list"] = [
        {"id": 1, "name": "Default"},
        {"id": 2, "name": "Cost Price GBR Net"},  # close but not exact
    ]
    bp.responses["/order-service/order-status"] = [
        {"id": 101, "name": "GBR JIT - Request Sent"},
        {"id": 102, "name": "GBR JIT - Pending"},
    ]
    result = discovery.discover(bp)
    assert result.price_list_id is None
    assert (1, "Default") in result.available_price_lists
    assert (2, "Cost Price GBR Net") in result.available_price_lists
    # Order statuses were all found, so we don't fetch / surface them.
    assert result.available_order_statuses == ()


def test_format_env_snippet_includes_price_list_candidates_when_missing() -> None:
    result = discovery.DiscoveryResult(
        supplier_contact_id=341,
        price_list_id=None,
        status_id_request_sent=101,
        status_id_pending=102,
        available_price_lists=(
            (2, "Cost Price GBR Net"),
            (1, "Default"),
        ),
    )
    snippet = discovery.format_env_snippet(result)
    assert "Available price lists" in snippet
    assert "Cost Price GBR Net" in snippet
    assert "Default" in snippet
    # Lines are sorted by ID.
    assert snippet.find("Default") < snippet.find("Cost Price GBR Net")


def test_format_env_snippet_includes_status_candidates_when_missing() -> None:
    result = discovery.DiscoveryResult(
        supplier_contact_id=341,
        price_list_id=7,
        status_id_request_sent=None,
        status_id_pending=None,
        available_order_statuses=(
            (10, "GBR JIT - Pending"),
            (11, "GBR JIT - Request sent"),  # different capitalisation
        ),
    )
    snippet = discovery.format_env_snippet(result)
    assert "Available order statuses" in snippet
    assert "GBR JIT - Request sent" in snippet
    assert "GBR JIT - Pending" in snippet


def test_list_price_lists_returns_id_name_pairs() -> None:
    bp = FakeBrightpearl()
    bp.responses["/product-service/price-list"] = [
        {"id": 1, "name": "Default"},
        {"id": 7, "name": "Cost Price GBR (Net)"},
    ]
    assert discovery.list_price_lists(bp) == [
        (1, "Default"),
        (7, "Cost Price GBR (Net)"),
    ]


def test_list_order_statuses_returns_id_name_pairs() -> None:
    bp = FakeBrightpearl()
    bp.responses["/order-service/order-status"] = [
        {"id": 101, "name": "Open"},
        {"id": 102, "name": "Closed"},
    ]
    assert discovery.list_order_statuses(bp) == [
        (101, "Open"),
        (102, "Closed"),
    ]
