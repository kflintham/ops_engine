from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from ops_engine.integrations.bobcat.config import BobcatConfig
from ops_engine.integrations.bobcat.stock_sync import run_stock_sync


WAREHOUSE_ID = 14
FIXED_NOW = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)


HEADER = (
    '"Brand","EAN","Purchase Price (GBP)","RRP (GBP)","Style Code",'
    '"Colour Code","Size","Description","Style Name","+10 Available","SKU"'
)


def _row(sku: str, availability: str) -> str:
    return (
        f'Bobcat Mens,"5059529681363","54.79","100.00",BC11436,200,40,'
        f'Black Nubuck,KEYSTONE Virginia,"{availability}",{sku}'
    )


@pytest.fixture
def config() -> BobcatConfig:
    return BobcatConfig(
        warehouse_id=WAREHOUSE_ID,
        in_stock_quantity=10,
        stock_remote_path="/home/wbysltd/SupplierStockFeeds/Bobcat/",
        stock_processed_path="/home/wbysltd/SupplierStockFeeds/Bobcat/processed/",
        file_name_prefix="BC_STOCK_",
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSftp:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)
        self.ensured_dirs: list[str] = []
        self.renames: list[tuple[str, str]] = []

    def ensure_dir(self, path: str) -> None:
        self.ensured_dirs.append(path)

    def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            key[len(prefix):]
            for key in self.files
            if key.startswith(prefix) and "/" not in key[len(prefix):]
        )

    def download_text(self, path: str) -> str:
        return self.files[path]

    def rename(self, src: str, dst: str) -> None:
        self.renames.append((src, dst))
        self.files[dst] = self.files.pop(src)


class FakeBrightpearl:
    """A faker tailored to the stock_sync needs.

    Pre-programmed mappings:
      - sku_to_pid: SKU resolution via product-search
      - warehouse_stock: product-availability + list_warehouse stock
    """

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
            pid = self.sku_to_pid.get(sku)
            if pid is None:
                return {"metaData": {"columns": []}, "results": []}
            return {
                "metaData": {
                    "columns": [{"name": "productId"}, {"name": "SKU"}]
                },
                "results": [[pid, sku]],
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
        if path == f"/warehouse-service/warehouse/{WAREHOUSE_ID}/stock":
            return {
                str(pid): {"inStock": qty}
                for pid, qty in self.warehouse_stock.items()
                if qty > 0
            }
        return None

    def post(self, path: str, *, json: Any = None) -> Any:
        self.post_calls.append((path, json))
        # Simulate applying the correction so downstream reads see the new
        # level (not strictly needed for these tests but makes the fake
        # realistic if we add retry logic).
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


def test_sync_updates_stock_for_in_stock_sku(config: BobcatConfig) -> None:
    path = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp({path: "\n".join([HEADER, _row("SKU-A", "Yes")])})
    bp = FakeBrightpearl(
        sku_to_pid={"SKU-A": 500},
        warehouse_stock={500: 0},
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert summary.filename == "BC_STOCK_20261103080000.csv"
    assert len(summary.changed) == 1
    assert summary.changed[0].sku == "SKU-A"
    assert summary.changed[0].previous_quantity == 0
    assert summary.changed[0].new_quantity == 10
    assert bp.post_calls[0][1]["corrections"] == [
        {
            "quantity": 10,
            "productId": 500,
            "reason": 1,
            "reference": "Bobcat daily stock feed",
        }
    ]


def test_sync_zeros_out_for_no_sku(config: BobcatConfig) -> None:
    path = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp({path: "\n".join([HEADER, _row("SKU-A", "No ")])})
    bp = FakeBrightpearl(
        sku_to_pid={"SKU-A": 500},
        warehouse_stock={500: 10},
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert len(summary.changed) == 1
    assert summary.changed[0].new_quantity == 0
    assert bp.post_calls[0][1]["corrections"][0]["quantity"] == -10


def test_sync_zeros_out_products_absent_from_feed(config: BobcatConfig) -> None:
    # Brightpearl has SKU-A (pid 500) and SKU-B (pid 501) in warehouse 14,
    # but today's feed only lists SKU-A. SKU-B should be zeroed out.
    path = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp({path: "\n".join([HEADER, _row("SKU-A", "Yes")])})
    bp = FakeBrightpearl(
        sku_to_pid={"SKU-A": 500},
        warehouse_stock={500: 0, 501: 7},  # 501 is in BP but not in feed
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    corrections = [c for (_, j) in bp.post_calls for c in j["corrections"]]
    by_pid = {c["productId"]: c["quantity"] for c in corrections}
    assert by_pid == {500: 10, 501: -7}


def test_sync_skips_unknown_sku(config: BobcatConfig) -> None:
    path = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp(
        {path: "\n".join([HEADER, _row("UNKNOWN", "Yes"), _row("SKU-A", "Yes")])}
    )
    bp = FakeBrightpearl(sku_to_pid={"SKU-A": 500}, warehouse_stock={500: 0})

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert summary.skipped_unknown_sku == ["UNKNOWN"]
    assert len(summary.changed) == 1  # only SKU-A


def test_sync_no_op_when_levels_already_correct(config: BobcatConfig) -> None:
    path = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp(
        {path: "\n".join([HEADER, _row("SKU-A", "Yes"), _row("SKU-B", "No ")])}
    )
    bp = FakeBrightpearl(
        sku_to_pid={"SKU-A": 500, "SKU-B": 501},
        warehouse_stock={500: 10, 501: 0},
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert summary.changed == []
    assert bp.post_calls == []


def test_sync_archives_processed_file(config: BobcatConfig) -> None:
    src = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp({src: "\n".join([HEADER, _row("SKU-A", "Yes")])})
    bp = FakeBrightpearl(sku_to_pid={"SKU-A": 500}, warehouse_stock={500: 0})

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert len(sftp.renames) == 1
    src_path, dst_path = sftp.renames[0]
    assert src_path == src
    assert dst_path.startswith(
        "/home/wbysltd/SupplierStockFeeds/Bobcat/processed/"
    )
    assert dst_path.endswith("BC_STOCK_20261103080000.csv")
    assert "20261103080000-" in dst_path  # timestamp prefix


def test_sync_picks_latest_when_multiple_files(config: BobcatConfig) -> None:
    older = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261102080000.csv"
    newer = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp(
        {
            older: "\n".join([HEADER, _row("OLD-SKU", "Yes")]),
            newer: "\n".join([HEADER, _row("NEW-SKU", "Yes")]),
        }
    )
    bp = FakeBrightpearl(
        sku_to_pid={"NEW-SKU": 500, "OLD-SKU": 999},
        warehouse_stock={500: 0, 999: 0},
    )

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.filename == "BC_STOCK_20261103080000.csv"
    # Only NEW-SKU should appear; OLD-SKU should NOT be zeroed because it
    # isn't in warehouse_stock (999: 0 means no stock on hand).
    changed_pids = {u.product_id for u in summary.changed}
    assert 500 in changed_pids
    assert 999 not in changed_pids


def test_sync_ignores_non_bobcat_files(config: BobcatConfig) -> None:
    sftp = FakeSftp(
        {
            "/home/wbysltd/SupplierStockFeeds/Bobcat/README.txt": "not me",
            "/home/wbysltd/SupplierStockFeeds/Bobcat/other.csv": "not me either",
        }
    )
    bp = FakeBrightpearl()
    summary = run_stock_sync(bp, sftp, config, now=_now)
    assert summary.filename is None
    assert summary.ok
    assert bp.post_calls == []


def test_sync_reports_run_failure_on_bad_csv(config: BobcatConfig) -> None:
    path = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp({path: "this is not a CSV"})
    bp = FakeBrightpearl()

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok is False
    assert "BobcatStockError" in summary.error
    # Bad file is not archived -- we want it to stay put so we can look at it.
    assert sftp.renames == []


def test_sync_ensures_processed_folder_exists(config: BobcatConfig) -> None:
    sftp = FakeSftp({})
    bp = FakeBrightpearl()
    run_stock_sync(bp, sftp, config, now=_now)
    assert config.stock_processed_path in sftp.ensured_dirs


def test_sync_continues_when_warehouse_listing_fails(
    config: BobcatConfig,
) -> None:
    # Brightpearl doesn't expose the "list warehouse contents" endpoint on
    # every account. When that lookup raises, the sync should log a warning
    # and still apply the feed SKUs rather than fail the whole run.
    path = "/home/wbysltd/SupplierStockFeeds/Bobcat/BC_STOCK_20261103080000.csv"
    sftp = FakeSftp({path: "\n".join([HEADER, _row("SKU-A", "Yes")])})
    bp = FakeBrightpearl(sku_to_pid={"SKU-A": 500}, warehouse_stock={500: 0})

    original_get = bp.get

    def failing_get(p: str, *, params: Any = None) -> Any:
        if p == f"/warehouse-service/warehouse/{WAREHOUSE_ID}/stock":
            raise RuntimeError("404 Not Found")
        return original_get(p, params=params)

    bp.get = failing_get  # type: ignore[assignment]

    summary = run_stock_sync(bp, sftp, config, now=_now)

    assert summary.ok
    assert len(summary.changed) == 1
    assert summary.changed[0].sku == "SKU-A"
