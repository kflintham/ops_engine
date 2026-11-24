from __future__ import annotations

import pytest

from ops_engine.integrations.titan_safety.config import TitanSafetyConfig


BASE_ENV = {
    "TITAN_SAFETY_WAREHOUSE_ID": "15",
    "TITAN_SAFETY_LOCATION_ID": "27",
    "TITAN_SAFETY_STOCK_PATH": "/home/wbysltd/SupplierStockFeeds/TitanSafety/",
    "TITAN_SAFETY_IMAP_HOST": "imap.gmail.com",
    "TITAN_SAFETY_IMAP_USERNAME": "wbysstockfeed@gmail.com",
    "TITAN_SAFETY_IMAP_PASSWORD": "secret-app-password",
}


def test_from_env_reads_required_vars() -> None:
    cfg = TitanSafetyConfig.from_env(dict(BASE_ENV))
    assert cfg.warehouse_id == 15
    assert cfg.location_id == 27
    assert cfg.stock_remote_path == (
        "/home/wbysltd/SupplierStockFeeds/TitanSafety/"
    )
    assert cfg.stock_processed_path == (
        "/home/wbysltd/SupplierStockFeeds/TitanSafety/processed/"
    )
    assert cfg.file_name_prefix == "TITAN_STOCK_"
    assert cfg.currency_code == "GBP"
    assert cfg.imap_host == "imap.gmail.com"
    assert cfg.imap_port == 993
    assert cfg.imap_username == "wbysstockfeed@gmail.com"
    assert cfg.imap_password == "secret-app-password"
    assert cfg.imap_folder == "INBOX"
    assert cfg.imap_sender_filter == ""


def test_from_env_honours_optional_overrides() -> None:
    env = dict(BASE_ENV)
    env["TITAN_SAFETY_CURRENCY_CODE"] = "EUR"
    env["TITAN_SAFETY_STOCK_PROCESSED_PATH"] = "/archive/titan/"
    env["TITAN_SAFETY_FILE_NAME_PREFIX"] = "T_"
    env["TITAN_SAFETY_IMAP_PORT"] = "1993"
    env["TITAN_SAFETY_IMAP_FOLDER"] = "Titan"
    env["TITAN_SAFETY_IMAP_SENDER_FILTER"] = "@titansafety.com"
    cfg = TitanSafetyConfig.from_env(env)
    assert cfg.currency_code == "EUR"
    assert cfg.stock_processed_path == "/archive/titan/"
    assert cfg.file_name_prefix == "T_"
    assert cfg.imap_port == 1993
    assert cfg.imap_folder == "Titan"
    assert cfg.imap_sender_filter == "@titansafety.com"


@pytest.mark.parametrize(
    "missing",
    [
        "TITAN_SAFETY_WAREHOUSE_ID",
        "TITAN_SAFETY_LOCATION_ID",
        "TITAN_SAFETY_STOCK_PATH",
        "TITAN_SAFETY_IMAP_HOST",
        "TITAN_SAFETY_IMAP_USERNAME",
        "TITAN_SAFETY_IMAP_PASSWORD",
    ],
)
def test_from_env_errors_on_missing_required(missing: str) -> None:
    env = dict(BASE_ENV)
    env.pop(missing)
    with pytest.raises(RuntimeError, match=missing):
        TitanSafetyConfig.from_env(env)


def test_from_env_errors_on_non_integer_warehouse() -> None:
    env = dict(BASE_ENV)
    env["TITAN_SAFETY_WAREHOUSE_ID"] = "fifteen"
    with pytest.raises(RuntimeError, match="Invalid integer"):
        TitanSafetyConfig.from_env(env)


def test_from_env_errors_on_non_integer_imap_port() -> None:
    env = dict(BASE_ENV)
    env["TITAN_SAFETY_IMAP_PORT"] = "not-a-port"
    with pytest.raises(RuntimeError, match="IMAP_PORT"):
        TitanSafetyConfig.from_env(env)
