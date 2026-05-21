from __future__ import annotations

from typing import Any

import pytest

from ops_engine.integrations.gardiner_brothers_jit.order_builder import (
    Order,
    OrderLine,
)
from ops_engine.integrations.gardiner_brothers_jit.po_mapper import (
    GbrJitMappingError,
    build_order_from_po,
)


def _po_with_rows_as_dict() -> dict[str, Any]:
    """Mirrors the shape Brightpearl actually returns for orderRows."""
    return {
        "id": 12346,
        "orderRows": {
            "101": {
                "productId": 501,
                "productSku": "34233-58447-07",
                "quantity": {"magnitude": "1.000000"},
            },
            "102": {
                "productId": 502,
                "productSku": "24840-41090-04",
                "quantity": {"magnitude": "3"},
            },
        },
    }


def _po_with_rows_as_list() -> dict[str, Any]:
    """The alternative shape some Brightpearl clients / endpoints return."""
    return {
        "id": 777,
        "ref": "PO-Alpha",
        "orderRows": [
            {
                "id": 1,
                "productId": 501,
                "productSku": "SKU-1",
                "quantity": {"magnitude": "2"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_builds_order_when_all_validations_pass() -> None:
    order = build_order_from_po(_po_with_rows_as_dict())
    assert order == Order(
        reference="12346",
        lines=(
            OrderLine(sku="34233-58447-07", quantity=1, line_reference="12346-101"),
            OrderLine(sku="24840-41090-04", quantity=3, line_reference="12346-102"),
        ),
    )


def test_prefers_po_ref_over_numeric_id_when_set() -> None:
    order = build_order_from_po(_po_with_rows_as_list())
    assert order.reference == "PO-Alpha"
    assert order.lines[0].line_reference == "777-1"


def test_accepts_reference_field_as_alias_for_ref() -> None:
    """Brightpearl's actual orders use 'reference', not 'ref'."""
    po = _po_with_rows_as_list()
    del po["ref"]
    po["reference"] = "Test - Katie - FTP"
    order = build_order_from_po(po)
    assert order.reference == "Test - Katie - FTP"


def test_handles_order_rows_as_list() -> None:
    order = build_order_from_po(_po_with_rows_as_list())
    assert order.lines[0].quantity == 2


def test_strips_whitespace_from_ref_and_sku() -> None:
    po = _po_with_rows_as_list()
    po["ref"] = "  PO-Alpha  "
    po["orderRows"][0]["productSku"] = "  SKU-1  "
    order = build_order_from_po(po)
    assert order.reference == "PO-Alpha"
    assert order.lines[0].sku == "SKU-1"


# ---------------------------------------------------------------------------
# SKU resolution
# ---------------------------------------------------------------------------


def test_rejects_row_when_productSku_missing() -> None:
    po = _po_with_rows_as_list()
    del po["orderRows"][0]["productSku"]
    with pytest.raises(GbrJitMappingError, match="missing productSku"):
        build_order_from_po(po)


def test_rejects_row_when_productSku_blank() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["productSku"] = "   "
    with pytest.raises(GbrJitMappingError, match="missing productSku"):
        build_order_from_po(po)


def test_aggregates_multiple_errors_across_lines() -> None:
    po = _po_with_rows_as_dict()
    po["orderRows"]["102"]["productSku"] = ""  # second line has no SKU
    del po["orderRows"]["101"]["quantity"]  # first line has no quantity
    with pytest.raises(GbrJitMappingError) as excinfo:
        build_order_from_po(po)
    msg = str(excinfo.value)
    assert "row 101" in msg
    assert "row 102" in msg


# ---------------------------------------------------------------------------
# Quantity parsing
# ---------------------------------------------------------------------------


def test_quantity_accepts_brightpearl_decimal_strings() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["quantity"] = {"magnitude": "5.000000"}
    order = build_order_from_po(po)
    assert order.lines[0].quantity == 5


def test_quantity_rejects_fractional_values() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["quantity"] = {"magnitude": "1.5"}
    with pytest.raises(GbrJitMappingError, match="not an integer"):
        build_order_from_po(po)


def test_quantity_rejects_non_numeric_value() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["quantity"] = {"magnitude": "abc"}
    with pytest.raises(GbrJitMappingError, match="not a number"):
        build_order_from_po(po)


def test_quantity_rejects_zero_or_negative() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["quantity"] = {"magnitude": "0"}
    with pytest.raises(GbrJitMappingError, match="must be positive"):
        build_order_from_po(po)


def test_quantity_rejects_missing_field() -> None:
    po = _po_with_rows_as_list()
    del po["orderRows"][0]["quantity"]
    with pytest.raises(GbrJitMappingError, match="missing quantity.magnitude"):
        build_order_from_po(po)


# ---------------------------------------------------------------------------
# Structural failures
# ---------------------------------------------------------------------------


def test_rejects_po_with_no_order_rows_field() -> None:
    with pytest.raises(GbrJitMappingError, match="no orderRows"):
        build_order_from_po({"id": 1})


def test_rejects_po_with_empty_order_rows() -> None:
    with pytest.raises(GbrJitMappingError, match="no order lines"):
        build_order_from_po({"id": 1, "orderRows": []})
