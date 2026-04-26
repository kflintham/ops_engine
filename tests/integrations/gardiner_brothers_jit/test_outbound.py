from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from ops_engine.integrations.gardiner_brothers_jit.config import GbrJitConfig
from ops_engine.integrations.gardiner_brothers_jit.outbound import (
    OutboundSummary,
    run_outbound,
)


# Test IDs -----------------------------------------------------------------

B1358_ID = 4242
PRICE_LIST_ID = 7
STATUS_REQUEST_SENT = 101
STATUS_PENDING = 102

FIXED_NOW = datetime(2026, 4, 24, 9, 30, tzinfo=timezone.utc)


# Fake clients --------------------------------------------------------------


class FakeBrightpearl:
    def __init__(self) -> None:
        self.get_responses: dict[str, Any] = {}
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self._fail_status_update_for: set[int] = set()

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append((path, dict(params) if params else None))
        return self.get_responses.get(path)

    def post(self, path: str, *, json: Any = None) -> Any:
        self.post_calls.append((path, json))
        for order_id in self._fail_status_update_for:
            if path == f"/order-service/order/{order_id}/status":
                raise RuntimeError("status API is down")
        return None

    def fail_status_update_for(self, order_id: int) -> None:
        self._fail_status_update_for.add(order_id)


class FakeSftp:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.fail_paths: set[str] = set()

    def upload_text(self, text: str, remote_path: str) -> None:
        if remote_path in self.fail_paths:
            raise IOError("upload failed")
        self.files[remote_path] = text


# Fixtures ------------------------------------------------------------------


@pytest.fixture
def config() -> GbrJitConfig:
    return GbrJitConfig(
        gardiners_jit_supplier_contact_id=B1358_ID,
        gardiners_price_list_id=PRICE_LIST_ID,
        status_id_request_sent=STATUS_REQUEST_SENT,
        status_id_pending=STATUS_PENDING,
        orders_remote_path="/JIT/Orders/",
        notifications_remote_path="/JIT/Notifications/",
    )


@pytest.fixture
def bp() -> FakeBrightpearl:
    return FakeBrightpearl()


@pytest.fixture
def sftp() -> FakeSftp:
    return FakeSftp()


def _install_po(
    bp: FakeBrightpearl,
    order_id: int,
    *,
    ref: str | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> None:
    if rows is None:
        rows = [{"id": 1, "productId": 501, "productQuantity": {"magnitude": "1"}}]
    po: dict[str, Any] = {"id": order_id, "orderRows": rows}
    if ref:
        po["ref"] = ref
    bp.get_responses[f"/order-service/order/{order_id}"] = po


def _install_product_catalog(bp: FakeBrightpearl) -> None:
    bp.get_responses["/product-service/product/501/supplier"] = {
        "501": [B1358_ID]
    }
    bp.get_responses["/product-service/product/501"] = [
        {"id": 501, "identity": {"sku": "34233-58447-07"}}
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sends_one_po_and_transitions_status(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    bp.get_responses["/order-service/order-search"] = {"results": [[555]]}
    _install_po(bp, 555, ref="PO-555")
    _install_product_catalog(bp)

    summary = run_outbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert len(summary.successes) == 1
    assert summary.failures == []

    result = summary.successes[0]
    assert result.order_id == 555
    assert result.order_reference == "PO-555"
    assert result.remote_path == "/JIT/Orders/PO-555-202604240930.csv"

    # The CSV uploaded matches what build_order_csv produces for that PO.
    assert result.remote_path in sftp.files
    uploaded = sftp.files[result.remote_path]
    assert uploaded.startswith("SKU,Quantity,Order Reference,Order Line Reference\r\n")
    assert "34233-58447-07,1,PO-555,555-1" in uploaded

    # Status was transitioned to Pending.
    assert (
        "/order-service/order/555/status",
        {"orderStatusId": STATUS_PENDING},
    ) in bp.post_calls


def test_handles_no_pos_to_send(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    bp.get_responses["/order-service/order-search"] = {"results": []}
    summary = run_outbound(bp, sftp, config, now=lambda: FIXED_NOW)
    assert summary.results == []
    assert sftp.files == {}
    assert bp.post_calls == []


def test_falls_back_to_numeric_order_id_when_ref_missing(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    bp.get_responses["/order-service/order-search"] = {"results": [[555]]}
    _install_po(bp, 555, ref=None)
    _install_product_catalog(bp)

    summary = run_outbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert summary.successes[0].order_reference == "555"
    assert "/JIT/Orders/555-202604240930.csv" in sftp.files


# ---------------------------------------------------------------------------
# Per-PO failure isolation
# ---------------------------------------------------------------------------


def test_mapping_failure_records_failure_and_does_not_upload(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    bp.get_responses["/order-service/order-search"] = {"results": [[555]]}
    _install_po(bp, 555, ref="PO-555")
    # Product 501 doesn't have B1358 as a supplier -> JIT eligibility fails.
    bp.get_responses["/product-service/product/501/supplier"] = {
        "501": [9999]
    }
    bp.get_responses["/product-service/product/501"] = [
        {"id": 501, "identity": {"sku": "ANY"}}
    ]

    summary = run_outbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert summary.successes == []
    assert len(summary.failures) == 1
    assert "does not list supplier" in (summary.failures[0].error or "")
    assert sftp.files == {}
    # PO status was NOT transitioned -- it stays on Request Sent for retry.
    assert not any(
        call[0].endswith("/status") for call in bp.post_calls
    )


def test_sftp_failure_leaves_po_on_request_sent(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    bp.get_responses["/order-service/order-search"] = {"results": [[555]]}
    _install_po(bp, 555, ref="PO-555")
    _install_product_catalog(bp)
    sftp.fail_paths.add("/JIT/Orders/PO-555-202604240930.csv")

    summary = run_outbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert len(summary.failures) == 1
    assert "upload failed" in (summary.failures[0].error or "")
    assert not any(
        call[0].endswith("/status") for call in bp.post_calls
    )


def test_status_update_failure_is_recorded_but_upload_still_happened(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    bp.get_responses["/order-service/order-search"] = {"results": [[555]]}
    _install_po(bp, 555, ref="PO-555")
    _install_product_catalog(bp)
    bp.fail_status_update_for(555)

    summary = run_outbound(bp, sftp, config, now=lambda: FIXED_NOW)

    # File is already up on SFTP.
    assert "/JIT/Orders/PO-555-202604240930.csv" in sftp.files
    # But the overall result is a failure because the PO state drifted.
    assert summary.successes == []
    assert len(summary.failures) == 1
    assert "status API is down" in (summary.failures[0].error or "")


def test_one_po_failure_does_not_stop_the_others(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    bp.get_responses["/order-service/order-search"] = {
        "results": [[555], [666]]
    }
    _install_po(bp, 555, ref="PO-555")
    _install_po(
        bp,
        666,
        ref="PO-666",
        rows=[{"id": 1, "productId": 999, "productQuantity": {"magnitude": "1"}}],
    )
    _install_product_catalog(bp)
    # Product 999 has no catalog info -> mapping fails for PO 666.
    bp.get_responses["/product-service/product/999/supplier"] = {"999": []}
    bp.get_responses["/product-service/product/999"] = [{"id": 999}]

    summary = run_outbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert len(summary.successes) == 1
    assert summary.successes[0].order_id == 555
    assert len(summary.failures) == 1
    assert summary.failures[0].order_id == 666


# ---------------------------------------------------------------------------
# OutboundSummary helpers
# ---------------------------------------------------------------------------


def test_summary_partitions_results() -> None:
    from ops_engine.integrations.gardiner_brothers_jit.outbound import (
        OutboundResult,
    )

    summary = OutboundSummary(
        results=[
            OutboundResult(order_id=1, ok=True, remote_path="/x"),
            OutboundResult(order_id=2, ok=False, error="boom"),
        ]
    )
    assert [r.order_id for r in summary.successes] == [1]
    assert [r.order_id for r in summary.failures] == [2]
