from __future__ import annotations

import pytest

from ops_engine.integrations.bobcat.config import BobcatConfig


BASE_ENV = {
    "BOBCAT_WAREHOUSE_ID": "14",
    "BOBCAT_LOCATION_ID": "27",
    "BOBCAT_STOCK_PATH": "/home/wbysltd/SupplierStockFeeds/Bobcat/",
}


def test_from_env_reads_minimum_required_vars() -> None:
    cfg = BobcatConfig.from_env(dict(BASE_ENV))
    assert cfg.warehouse_id == 14
    assert cfg.location_id == 27
    assert cfg.stock_remote_path == "/home/wbysltd/SupplierStockFeeds/Bobcat/"
    assert cfg.in_stock_quantity == 10
    assert cfg.currency_code == "GBP"
    assert cfg.stock_processed_path == (
        "/home/wbysltd/SupplierStockFeeds/Bobcat/processed/"
    )
    assert cfg.file_name_prefix == "BC_STOCK_"


def test_from_env_honours_overrides() -> None:
    env = dict(BASE_ENV)
    env["BOBCAT_IN_STOCK_QUANTITY"] = "25"
    env["BOBCAT_CURRENCY_CODE"] = "EUR"
    env["BOBCAT_STOCK_PROCESSED_PATH"] = "/archive/bobcat/"
    env["BOBCAT_FILE_NAME_PREFIX"] = "BOB_"
    cfg = BobcatConfig.from_env(env)
    assert cfg.in_stock_quantity == 25
    assert cfg.currency_code == "EUR"
    assert cfg.stock_processed_path == "/archive/bobcat/"
    assert cfg.file_name_prefix == "BOB_"


def test_from_env_errors_on_missing_warehouse() -> None:
    env = dict(BASE_ENV)
    env.pop("BOBCAT_WAREHOUSE_ID")
    with pytest.raises(RuntimeError, match="BOBCAT_WAREHOUSE_ID"):
        BobcatConfig.from_env(env)


def test_from_env_errors_on_missing_location_id() -> None:
    env = dict(BASE_ENV)
    env.pop("BOBCAT_LOCATION_ID")
    with pytest.raises(RuntimeError, match="BOBCAT_LOCATION_ID"):
        BobcatConfig.from_env(env)


def test_from_env_errors_on_missing_stock_path() -> None:
    env = dict(BASE_ENV)
    env.pop("BOBCAT_STOCK_PATH")
    with pytest.raises(RuntimeError, match="BOBCAT_STOCK_PATH"):
        BobcatConfig.from_env(env)


def test_from_env_errors_on_non_integer_warehouse() -> None:
    env = dict(BASE_ENV)
    env["BOBCAT_WAREHOUSE_ID"] = "fourteen"
    with pytest.raises(RuntimeError, match="Invalid integer"):
        BobcatConfig.from_env(env)


def test_from_env_errors_on_non_integer_in_stock_quantity() -> None:
    env = dict(BASE_ENV)
    env["BOBCAT_IN_STOCK_QUANTITY"] = "ten"
    with pytest.raises(RuntimeError, match="IN_STOCK_QUANTITY"):
        BobcatConfig.from_env(env)
