from __future__ import annotations

from typing import Any

import pytest

from ops_engine.core.sftp import SftpClient, SftpConfig
from ops_engine.integrations.gardiner_brothers_jit.config import GbrJitConfig
from ops_engine.integrations.gardiner_brothers_jit.setup import ensure_remote_folders


class FakeSession:
    def __init__(self) -> None:
        self.directories: set[str] = set()
        self.operations: list[tuple[str, tuple[Any, ...]]] = []

    def putfo(self, fl: Any, remotepath: str) -> None: ...
    def getfo(self, remotepath: str, fl: Any) -> None: ...
    def listdir(self, path: str = ".") -> list[str]: return []
    def remove(self, path: str) -> None: ...
    def rename(self, oldpath: str, newpath: str) -> None: ...
    def close(self) -> None: ...

    def mkdir(self, path: str) -> None:
        self.operations.append(("mkdir", (path,)))
        self.directories.add(path)

    def stat(self, path: str) -> Any:
        self.operations.append(("stat", (path,)))
        if path in self.directories:
            return object()
        raise IOError(f"No such file: {path}")


@pytest.fixture
def config() -> GbrJitConfig:
    return GbrJitConfig(
        gardiners_jit_supplier_contact_id=4242,
        gardiners_price_list_id=7,
        status_id_request_sent=101,
        status_id_pending=102,
        orders_remote_path="/JIT/Orders/",
        notifications_remote_path="/JIT/Notifications/",
    )


def test_creates_both_folders_when_neither_exists(
    config: GbrJitConfig,
) -> None:
    session = FakeSession()
    sftp_client = SftpClient(
        SftpConfig(host="h", username="u", password="p"),
        connect=lambda _c: session,
    )

    result = ensure_remote_folders(sftp_client, config)

    assert result.ensured == ("/JIT/Orders/", "/JIT/Notifications/")
    assert "/JIT" in session.directories
    assert "/JIT/Orders" in session.directories
    assert "/JIT/Notifications" in session.directories


def test_idempotent_when_folders_already_exist(config: GbrJitConfig) -> None:
    session = FakeSession()
    session.directories.update({"/JIT", "/JIT/Orders", "/JIT/Notifications"})
    sftp_client = SftpClient(
        SftpConfig(host="h", username="u", password="p"),
        connect=lambda _c: session,
    )

    ensure_remote_folders(sftp_client, config)

    mkdirs = [op for op in session.operations if op[0] == "mkdir"]
    assert mkdirs == []
