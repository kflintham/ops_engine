from __future__ import annotations

import pytest

from ops_engine.integrations.gardiner_brothers_jit.config import GbrJitConfig


_BASE_ENV = {
    "GBR_JIT_SUPPLIER_CONTACT_ID": "4242",
    "GBR_JIT_PRICE_LIST_ID": "7",
    "GBR_JIT_STATUS_ID_REQUEST_SENT": "101",
    "GBR_JIT_STATUS_ID_PENDING": "102",
    "GBR_JIT_STATUS_ID_ACKNOWLEDGED": "132",
    "GBR_JIT_STATUS_ID_ORDER_FULFILLED": "136",
    "GBR_JIT_STATUS_ID_CANCELLED": "134",
    "GBR_JIT_ORDERS_PATH": "/JIT/Orders/",
    "GBR_JIT_NOTIFICATIONS_PATH": "/JIT/Notifications/",
}


def test_from_env_loads_all_required_fields() -> None:
    cfg = GbrJitConfig.from_env(_BASE_ENV)
    assert cfg.gardiners_jit_supplier_contact_id == 4242
    assert cfg.gardiners_price_list_id == 7
    assert cfg.status_id_request_sent == 101
    assert cfg.status_id_pending == 102
    assert cfg.status_id_acknowledged == 132
    assert cfg.status_id_order_fulfilled == 136
    assert cfg.status_id_cancelled == 134
    assert cfg.orders_remote_path == "/JIT/Orders/"
    assert cfg.notifications_remote_path == "/JIT/Notifications/"
    assert cfg.notifications_processed_path == "/JIT/Notifications/processed/"
    assert cfg.file_name_template == "{order_reference}-{timestamp}.csv"


def test_from_env_uses_explicit_processed_path_when_set() -> None:
    env = {**_BASE_ENV, "GBR_JIT_NOTIFICATIONS_PROCESSED_PATH": "/archive/jit/"}
    assert GbrJitConfig.from_env(env).notifications_processed_path == "/archive/jit/"


def test_from_env_accepts_custom_file_name_template() -> None:
    env = {**_BASE_ENV, "GBR_JIT_FILE_NAME_TEMPLATE": "{order_reference}.csv"}
    assert GbrJitConfig.from_env(env).file_name_template == "{order_reference}.csv"


def test_from_env_lists_all_missing_vars() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        GbrJitConfig.from_env({})
    message = str(excinfo.value)
    for name in _BASE_ENV:
        assert name in message


def test_from_env_rejects_non_integer_ids() -> None:
    env = {**_BASE_ENV, "GBR_JIT_SUPPLIER_CONTACT_ID": "not-a-number"}
    with pytest.raises(RuntimeError, match="Invalid integer"):
        GbrJitConfig.from_env(env)
