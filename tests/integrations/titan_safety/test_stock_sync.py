from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

import pytest
from openpyxl import Workbook

from ops_engine.integrations.titan_safety.config import TitanSafetyConfig
from ops_engine.integrations.titan_safety.stock_sync import run_stock_sync


WAREHOUSE_ID = 15
LOCATION_ID = 27
FIXED_NOW = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)


HEADER = ("Product Code", "Product Description", "Available Qty")


# The SKU search in brightpearl_queries has a per-call sleep to keep us
# under Brightpearl's rate limit in production. Tests patch it to a no-op
# so a 3-SKU sync doesn't spend a second sleeping.
@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops_engine.integrations.titan_safety.brightpearl_queries as queries
    monkeypatch.setattr(queries, "PRODUCT_SEARCH_DELAY_SECONDS", 0)


def _xlsx_bytes(rows: list[tuple]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def config() -> TitanSafetyConfig:
    return TitanSafetyConfig(
        warehouse_id=WAREHOUSE_ID,
        location_id=LOCATION_ID,
        currency_code="GBP",
        stock_remote_path="/home/wbysltd/SupplierStockFeeds/TitanSafety/",
        stock_processed_path=(
            "/home/wbysltd/SupplierStockFeeds/TitanSafety/processed/"
        ),
        file_name_prefix="TITAN_STOCK_",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="x",
        imap_password="x",
        imap_folder="INBOX",
        imap_sender_filter="",
    )


class FakeSftp:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.ensured: list[str] = []
        self.renames: list[tuple[str, str]] = []

    def ensure_dir(self, path: str) -> None:
        self.ensured.append(path)

    def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            key[len(prefix):]
            for key in self.files
            if key.startswith(prefix) and "/" not in key[len(prefix):]
        )

    def download_bytes(self, path: str) -> bytes:
        return self.files[path]

    def rename(self, src: str, dst: str) -> None:
        self.renames.append((src, dst))
        self.files[dst] = self.files.pop(src)


class FakeBrightpearl:
    def __init__(
        self,
        *,
        sku_to_pid: dict[str, int] | None = None,
        warehouse_stock: dict[int, int] | None = None,
    ) -> None:
        self.sku_to_pid = sku_to_pid or {}
        self.warehouse_stock = warehouse_stock or {}
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, Any]] = []

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append((path, dict(params) if params else None))
        if path == "/product-service/product-search" and params and "SKU" in params:
            sku = params["SKU"]
            if sku not in self.sku_to_pid:
                return {"metaData": {"columns": []}, "results": []}
            return {
                "metaData": {
                    "columns": [{"name": "productId"}, {"name": "SKU"}]
                },
                "results": [[self.sku_to_pid[sku], sku]],
            }
        if path.startswith("/warehouse-service/product-availability/"):
            ids_part = path.rsplit("/", 1)[-1]
            response: dict[str, Any] = {}
            for pid_str in ids_part.split(","):
                pid = int(pid_str)
                response[str(pid)] = {
                    "warehouses": {
                        str(WAREHOUSE_ID): {
                            "inStock": self.warehouse_stock.get(pid, 0)
                        }
                    }
                }
            return response
        return None

    def post(self, path: str, *, json: Any = None) -> Any:
        self.post_calls.append((path, json))
        if path == f"/warehouse-service/warehouse/{WAREHOUSE_ID}/stock-correction":
            for c in json["corrections"]:
                self.warehouse_stock[c["productId"]] = (
                    self.warehouse_stock.get(c["productId"], 0) + c["quantity"]
                )
        return None


def _now() -> datetime:
    return FIXED_NOW


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sync_sets_stock_for_feed_skus(config: TitanSafetyConfig) -> None:
    xlsx = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest size 03", 35),
            ("TC01-10", "Quest size 10", 237),
        ]
    )
    path = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-stock.xlsx"
    sftp = FakeSftp({path: xlsx})
    bp = FakeBrightpearl(
        sku_to_pid={"TC01-3": 900, "TC01-10": 901},
        warehouse_stock={900: 0, 901: 100},
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    changes = {u.sku: u for u in summary.changed}
    assert changes["TC01-3"].previous_quantity == 0
    assert changes["TC01-3"].new_quantity == 35
    assert changes["TC01-10"].previous_quantity == 100
    assert changes["TC01-10"].new_quantity == 237

    # Single batched POST with both corrections.
    assert len(bp.post_calls) == 1
    corrections = bp.post_calls[0][1]["corrections"]
    by_pid = {c["productId"]: c for c in corrections}
    assert by_pid[900]["quantity"] == 35
    assert by_pid[901]["quantity"] == 137  # 237 - 100
    assert by_pid[900]["locationId"] == LOCATION_ID
    assert by_pid[900]["cost"] == {"value": "0.00", "currency": "GBP"}


def test_sync_normalizes_titan_sku_for_lookup(
    config: TitanSafetyConfig,
) -> None:
    # TC01-03 in the feed should be looked up as TC01-3 in Brightpearl.
    xlsx = _xlsx_bytes([HEADER, ("TC01-03", "Quest", 35)])
    path = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-stock.xlsx"
    sftp = FakeSftp({path: xlsx})
    bp = FakeBrightpearl(
        sku_to_pid={"TC01-3": 900}, warehouse_stock={900: 0}
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    sku_search_calls = [
        c for c in bp.get_calls
        if c[0] == "/product-service/product-search"
    ]
    assert sku_search_calls[0][1]["SKU"] == "TC01-3"


def test_sync_skips_unknown_sku_but_applies_the_rest(
    config: TitanSafetyConfig,
) -> None:
    xlsx = _xlsx_bytes(
        [
            HEADER,
            ("TC01-03", "Quest", 35),
            ("TC99-99", "Unknown", 10),
        ]
    )
    path = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-stock.xlsx"
    sftp = FakeSftp({path: xlsx})
    bp = FakeBrightpearl(
        sku_to_pid={"TC01-3": 900},  # TC99-99 not in BP
        warehouse_stock={900: 0},
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert summary.skipped_unknown_sku == ["TC99-99"]
    assert len(summary.changed) == 1
    assert summary.changed[0].sku == "TC01-3"


def test_sync_no_op_when_levels_already_correct(
    config: TitanSafetyConfig,
) -> None:
    xlsx = _xlsx_bytes([HEADER, ("TC01-03", "Quest", 35)])
    path = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-stock.xlsx"
    sftp = FakeSftp({path: xlsx})
    bp = FakeBrightpearl(
        sku_to_pid={"TC01-3": 900}, warehouse_stock={900: 35}
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert summary.changed == []
    assert bp.post_calls == []


def test_sync_archives_processed_file(config: TitanSafetyConfig) -> None:
    xlsx = _xlsx_bytes([HEADER, ("TC01-03", "Quest", 35)])
    src = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-stock.xlsx"
    sftp = FakeSftp({src: xlsx})
    bp = FakeBrightpearl(
        sku_to_pid={"TC01-3": 900}, warehouse_stock={900: 0}
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert len(sftp.renames) == 1
    src_path, dst_path = sftp.renames[0]
    assert src_path == src
    assert dst_path.startswith(
        "/home/wbysltd/SupplierStockFeeds/TitanSafety/processed/"
    )


def test_sync_picks_latest_when_multiple_files(
    config: TitanSafetyConfig,
) -> None:
    older_xlsx = _xlsx_bytes([HEADER, ("TC01-03", "Quest", 10)])
    newer_xlsx = _xlsx_bytes([HEADER, ("TC01-03", "Quest", 50)])
    older = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261123080000-old.xlsx"
    newer = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-new.xlsx"
    sftp = FakeSftp({older: older_xlsx, newer: newer_xlsx})
    bp = FakeBrightpearl(
        sku_to_pid={"TC01-3": 900}, warehouse_stock={900: 0}
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.filename and summary.filename.endswith("new.xlsx")
    # Target reflects the newer file (50), not the older (10).
    assert summary.changed[0].new_quantity == 50


def test_sync_ignores_non_xlsx_files(config: TitanSafetyConfig) -> None:
    sftp = FakeSftp(
        {
            "/home/wbysltd/SupplierStockFeeds/TitanSafety/README.txt": b"notes",
            "/home/wbysltd/SupplierStockFeeds/TitanSafety/data.csv": b"a,b,c",
        }
    )
    bp = FakeBrightpearl()
    summary = run_stock_sync(bp, sftp, config, now=_now)
    assert summary.filename is None
    assert summary.ok
    assert bp.post_calls == []


def test_sync_reports_run_failure_on_bad_xlsx(
    config: TitanSafetyConfig,
) -> None:
    path = "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-broken.xlsx"
    sftp = FakeSftp({path: b"not a workbook"})
    bp = FakeBrightpearl()

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok is False
    assert "Could not open XLSX" in (summary.error or "")
    # File isn't archived -- stays for inspection.
    assert sftp.renames == []


def test_sync_ensures_processed_folder_exists(
    config: TitanSafetyConfig,
) -> None:
    sftp = FakeSftp({})
    bp = FakeBrightpearl()
    run_stock_sync(bp, sftp, config, now=_now)
    assert config.stock_processed_path in sftp.ensured
