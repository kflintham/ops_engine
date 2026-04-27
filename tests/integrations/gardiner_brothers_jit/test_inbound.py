from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from ops_engine.integrations.gardiner_brothers_jit.config import GbrJitConfig
from ops_engine.integrations.gardiner_brothers_jit.inbound import run_inbound


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIVED_SAMPLE = (
    REPO_ROOT
    / "docs"
    / "gardiner-brothers-jit"
    / "samples"
    / "order-notification-received.csv"
)
DESPATCHED_SAMPLE = (
    REPO_ROOT
    / "docs"
    / "gardiner-brothers-jit"
    / "samples"
    / "order-notification-despatched.csv"
)


B1358_ID = 4242
STATUS_ACK = 132
STATUS_FULFILLED = 136
STATUS_CANCELLED = 134

FIXED_NOW = datetime(2026, 4, 27, 8, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBrightpearl:
    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.put_calls: list[tuple[str, Any]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self.get_responses: dict[str, Any] = {}

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append((path, dict(params) if params else None))
        return self.get_responses.get(path)

    def put(self, path: str, *, json: Any = None) -> Any:
        self.put_calls.append((path, json))
        return None

    def post(self, path: str, *, json: Any = None) -> Any:
        self.post_calls.append((path, json))
        return None


class FakeSftp:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.directories: set[str] = set()
        self.fail_paths: set[str] = set()

    def list_dir(self, remote_path: str) -> list[str]:
        prefix = remote_path.rstrip("/") + "/"
        names: list[str] = []
        for path in self.files:
            if path.startswith(prefix):
                rest = path[len(prefix):]
                if "/" not in rest:
                    names.append(rest)
        return names

    def download_text(self, remote_path: str) -> str:
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        return self.files[remote_path]

    def rename(self, source: str, destination: str) -> None:
        if source not in self.files:
            raise FileNotFoundError(source)
        self.files[destination] = self.files.pop(source)

    def ensure_dir(self, remote_path: str) -> None:
        self.directories.add(remote_path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> GbrJitConfig:
    return GbrJitConfig(
        gardiners_jit_supplier_contact_id=B1358_ID,
        gardiners_price_list_id=7,
        status_id_request_sent=101,
        status_id_pending=102,
        status_id_acknowledged=STATUS_ACK,
        status_id_order_fulfilled=STATUS_FULFILLED,
        status_id_cancelled=STATUS_CANCELLED,
        orders_remote_path="/JIT/Orders/",
        notifications_remote_path="/JIT/Notifications/",
        notifications_processed_path="/JIT/Notifications/processed/",
    )


@pytest.fixture
def bp() -> FakeBrightpearl:
    return FakeBrightpearl()


@pytest.fixture
def sftp() -> FakeSftp:
    return FakeSftp()


def _drop_file(sftp: FakeSftp, name: str, content: str) -> str:
    path = f"/JIT/Notifications/{name}"
    sftp.files[path] = content
    return path


# ---------------------------------------------------------------------------
# Happy paths -- using the real Gardiners sample files
# ---------------------------------------------------------------------------


def test_received_notification_transitions_po_to_acknowledged(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    _drop_file(sftp, "received.csv", RECEIVED_SAMPLE.read_text())
    bp.get_responses["/order-service/order-search"] = {"results": [[1234]]}

    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert summary.failures == []
    assert len(summary.successes) == 1
    transitions = summary.successes[0].transitions
    assert transitions == (("Test002", 1234, STATUS_ACK),)
    assert (
        "/order-service/order/1234/status",
        {"orderStatusId": STATUS_ACK},
    ) in bp.put_calls


def test_despatched_notification_transitions_po_to_order_fulfilled(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    _drop_file(sftp, "despatched.csv", DESPATCHED_SAMPLE.read_text())
    bp.get_responses["/order-service/order-search"] = {"results": [[1234]]}

    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert summary.failures == []
    transitions = summary.successes[0].transitions
    assert transitions == (("Test002", 1234, STATUS_FULFILLED),)


def test_handles_no_files_gracefully(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)
    assert summary.results == []
    assert bp.put_calls == []


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


def test_processed_file_is_archived_with_timestamp(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    src = _drop_file(sftp, "received.csv", RECEIVED_SAMPLE.read_text())
    bp.get_responses["/order-service/order-search"] = {"results": [[1234]]}

    run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert src not in sftp.files
    expected_archive = "/JIT/Notifications/processed/20260427083000-received.csv"
    assert expected_archive in sftp.files


def test_only_csv_files_are_processed(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    _drop_file(sftp, "received.csv", RECEIVED_SAMPLE.read_text())
    _drop_file(sftp, "ignore.txt", "not a csv")
    bp.get_responses["/order-service/order-search"] = {"results": [[1234]]}

    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert [r.filename for r in summary.results] == ["received.csv"]
    assert "/JIT/Notifications/ignore.txt" in sftp.files  # left in place


def test_processed_dir_is_ensured(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)
    assert "/JIT/Notifications/processed/" in sftp.directories


# ---------------------------------------------------------------------------
# PO resolution
# ---------------------------------------------------------------------------


def test_numeric_reference_skips_search_and_uses_id_directly(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    csv = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
        "1214015,1214015-1,SKU-1,Thing,Black,7,1,,,,,Recieved\r\n"
    )
    _drop_file(sftp, "n.csv", csv)

    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert summary.failures == []
    transitions = summary.successes[0].transitions
    assert transitions == (("1214015", 1214015, STATUS_ACK),)
    # No order-search call -- the numeric ref was used directly.
    search_calls = [c for c in bp.get_calls if c[0] == "/order-service/order-search"]
    assert search_calls == []


def test_unresolvable_reference_records_failure(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    _drop_file(sftp, "received.csv", RECEIVED_SAMPLE.read_text())
    bp.get_responses["/order-service/order-search"] = {"results": []}

    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert summary.successes == []
    assert len(summary.failures) == 1
    assert "Test002" in (summary.failures[0].error or "")
    assert bp.put_calls == []


# ---------------------------------------------------------------------------
# Status precedence
# ---------------------------------------------------------------------------


def test_cancelled_wins_over_other_statuses_in_same_file(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    csv = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
        "ORDER-A,A-1,X,T,B,7,1,,,,,Recieved\r\n"
        "ORDER-A,A-2,X,T,B,7,1,,,,,Cancelled\r\n"
    )
    _drop_file(sftp, "mixed.csv", csv)
    bp.get_responses["/order-service/order-search"] = {"results": [[999]]}

    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    assert summary.successes[0].transitions == (
        ("ORDER-A", 999, STATUS_CANCELLED),
    )


def test_other_statuses_only_skip_transition(
    bp: FakeBrightpearl, sftp: FakeSftp, config: GbrJitConfig
) -> None:
    csv = (
        "Customer Header Reference,Customer Line Reference,Sku,Description,"
        "Colour,Size,Quantity,Carrier,Consignment Status,Consignment Reference,"
        "Consignment Tracking Url,Current Status\r\n"
        "ORDER-B,B-1,X,T,B,7,1,,,,,Picking\r\n"
    )
    _drop_file(sftp, "intermediate.csv", csv)

    summary = run_inbound(bp, sftp, config, now=lambda: FIXED_NOW)

    # File processed cleanly, no transitions, no PUT calls.
    assert summary.successes[0].transitions == ()
    assert bp.put_calls == []
