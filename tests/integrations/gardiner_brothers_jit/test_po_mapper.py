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


# Brightpearl test IDs used throughout. B1358_ID stands in for whatever
# numeric contact ID 'Gardiner Bros & Co (B1358)' actually has in BP.
B1358_ID = 4242
B3116_DF_ID = 9999  # the dropship account; must never satisfy the filter


def _po_with_rows_as_dict() -> dict[str, Any]:
    """Mirrors the shape Brightpearl most commonly returns for orderRows."""
    return {
        "id": 12346,
        "orderRows": {
            "101": {
                "id": 101,
                "productId": 501,
                "productQuantity": {"magnitude": "1.000000"},
            },
            "102": {
                "id": 102,
                "productId": 502,
                "productQuantity": {"magnitude": "3"},
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
                "productQuantity": {"magnitude": "2"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_builds_order_when_all_validations_pass() -> None:
    order = build_order_from_po(
        _po_with_rows_as_dict(),
        product_supplier_ids={501: [B1358_ID, B3116_DF_ID], 502: [B1358_ID]},
        product_gardiners_skus={501: "34233-58447-07", 502: "24840-41090-04"},
        required_supplier_contact_id=B1358_ID,
    )
    assert order == Order(
        reference="12346",
        lines=(
            OrderLine(sku="34233-58447-07", quantity=1, line_reference="12346-101"),
            OrderLine(sku="24840-41090-04", quantity=3, line_reference="12346-102"),
        ),
    )


def test_prefers_po_ref_over_numeric_id_when_set() -> None:
    order = build_order_from_po(
        _po_with_rows_as_list(),
        product_supplier_ids={501: [B1358_ID]},
        product_gardiners_skus={501: "SKU-1"},
        required_supplier_contact_id=B1358_ID,
    )
    assert order.reference == "PO-Alpha"
    assert order.lines[0].line_reference == "777-1"


def test_handles_order_rows_as_list() -> None:
    order = build_order_from_po(
        _po_with_rows_as_list(),
        product_supplier_ids={501: [B1358_ID]},
        product_gardiners_skus={501: "SKU-1"},
        required_supplier_contact_id=B1358_ID,
    )
    assert order.lines[0].quantity == 2


def test_strips_whitespace_from_ref_and_sku() -> None:
    po = _po_with_rows_as_list()
    po["ref"] = "  PO-Alpha  "
    order = build_order_from_po(
        po,
        product_supplier_ids={501: [B1358_ID]},
        product_gardiners_skus={501: "  SKU-1  "},
        required_supplier_contact_id=B1358_ID,
    )
    assert order.reference == "PO-Alpha"
    assert order.lines[0].sku == "SKU-1"


# ---------------------------------------------------------------------------
# JIT eligibility rule
# ---------------------------------------------------------------------------


def test_rejects_po_when_product_missing_jit_supplier() -> None:
    po = _po_with_rows_as_list()
    with pytest.raises(GbrJitMappingError) as excinfo:
        build_order_from_po(
            po,
            product_supplier_ids={501: [B3116_DF_ID]},  # DF only, no JIT
            product_gardiners_skus={501: "SKU-1"},
            required_supplier_contact_id=B1358_ID,
        )
    assert "does not list supplier" in str(excinfo.value)
    assert "4242" in str(excinfo.value)


def test_rejects_po_when_no_supplier_info_available() -> None:
    po = _po_with_rows_as_list()
    with pytest.raises(GbrJitMappingError, match="no supplier information"):
        build_order_from_po(
            po,
            product_supplier_ids={},  # nothing known about product 501
            product_gardiners_skus={501: "SKU-1"},
            required_supplier_contact_id=B1358_ID,
        )


# ---------------------------------------------------------------------------
# SKU resolution rule
# ---------------------------------------------------------------------------


def test_rejects_po_when_product_has_no_gardiners_sku() -> None:
    po = _po_with_rows_as_list()
    with pytest.raises(GbrJitMappingError, match="no SKU on the Gardiners price list"):
        build_order_from_po(
            po,
            product_supplier_ids={501: [B1358_ID]},
            product_gardiners_skus={501: None},
            required_supplier_contact_id=B1358_ID,
        )


def test_rejects_po_when_sku_is_empty_string() -> None:
    po = _po_with_rows_as_list()
    with pytest.raises(GbrJitMappingError, match="no SKU"):
        build_order_from_po(
            po,
            product_supplier_ids={501: [B1358_ID]},
            product_gardiners_skus={501: "   "},
            required_supplier_contact_id=B1358_ID,
        )


def test_aggregates_multiple_errors_across_lines() -> None:
    po = _po_with_rows_as_dict()
    with pytest.raises(GbrJitMappingError) as excinfo:
        build_order_from_po(
            po,
            product_supplier_ids={501: [B3116_DF_ID], 502: [B1358_ID]},
            product_gardiners_skus={501: "SKU-1", 502: None},
            required_supplier_contact_id=B1358_ID,
        )
    msg = str(excinfo.value)
    assert "product 501" in msg
    assert "product 502" in msg


# ---------------------------------------------------------------------------
# Quantity parsing
# ---------------------------------------------------------------------------


def test_quantity_accepts_brightpearl_decimal_strings() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["productQuantity"] = {"magnitude": "5.000000"}
    order = build_order_from_po(
        po,
        product_supplier_ids={501: [B1358_ID]},
        product_gardiners_skus={501: "SKU-1"},
        required_supplier_contact_id=B1358_ID,
    )
    assert order.lines[0].quantity == 5


def test_quantity_rejects_fractional_values() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["productQuantity"] = {"magnitude": "1.5"}
    with pytest.raises(GbrJitMappingError, match="not an integer"):
        build_order_from_po(
            po,
            product_supplier_ids={501: [B1358_ID]},
            product_gardiners_skus={501: "SKU-1"},
            required_supplier_contact_id=B1358_ID,
        )


def test_quantity_rejects_non_numeric_value() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["productQuantity"] = {"magnitude": "abc"}
    with pytest.raises(GbrJitMappingError, match="not a number"):
        build_order_from_po(
            po,
            product_supplier_ids={501: [B1358_ID]},
            product_gardiners_skus={501: "SKU-1"},
            required_supplier_contact_id=B1358_ID,
        )


def test_quantity_rejects_zero_or_negative() -> None:
    po = _po_with_rows_as_list()
    po["orderRows"][0]["productQuantity"] = {"magnitude": "0"}
    with pytest.raises(GbrJitMappingError, match="must be positive"):
        build_order_from_po(
            po,
            product_supplier_ids={501: [B1358_ID]},
            product_gardiners_skus={501: "SKU-1"},
            required_supplier_contact_id=B1358_ID,
        )


# ---------------------------------------------------------------------------
# Structural failures
# ---------------------------------------------------------------------------


def test_rejects_po_with_no_order_rows_field() -> None:
    with pytest.raises(GbrJitMappingError, match="no orderRows"):
        build_order_from_po(
            {"id": 1},
            product_supplier_ids={},
            product_gardiners_skus={},
            required_supplier_contact_id=B1358_ID,
        )


def test_rejects_po_with_empty_order_rows() -> None:
    with pytest.raises(GbrJitMappingError, match="no order lines"):
        build_order_from_po(
            {"id": 1, "orderRows": []},
            product_supplier_ids={},
            product_gardiners_skus={},
            required_supplier_contact_id=B1358_ID,
        )
