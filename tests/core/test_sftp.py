from __future__ import annotations

from typing import Any

import pytest

from ops_engine.core.sftp import SftpClient, SftpConfig


# ---------------------------------------------------------------------------
# Fake SFTP session
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.listings: dict[str, list[str]] = {}
        self.closed = False
        self.operations: list[tuple[str, tuple[Any, ...]]] = []

    # Protocol methods -------------------------------------------------------

    def putfo(self, fl: Any, remotepath: str) -> None:
        self.operations.append(("putfo", (remotepath,)))
        self.files[remotepath] = fl.read()

    def getfo(self, remotepath: str, fl: Any) -> None:
        self.operations.append(("getfo", (remotepath,)))
        if remotepath not in self.files:
            raise FileNotFoundError(remotepath)
        fl.write(self.files[remotepath])

    def listdir(self, path: str = ".") -> list[str]:
        self.operations.append(("listdir", (path,)))
        return list(self.listings.get(path, []))

    def remove(self, path: str) -> None:
        self.operations.append(("remove", (path,)))
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def rename(self, oldpath: str, newpath: str) -> None:
        self.operations.append(("rename", (oldpath, newpath)))
        if oldpath not in self.files:
            raise FileNotFoundError(oldpath)
        self.files[newpath] = self.files.pop(oldpath)

    def close(self) -> None:
        self.operations.append(("close", ()))
        self.closed = True


@pytest.fixture
def config() -> SftpConfig:
    return SftpConfig(
        host="sftp.example.com",
        username="wbysltd",
        password="pw",
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(config: SftpConfig, session: FakeSession) -> SftpClient:
    return SftpClient(config, connect=lambda _cfg: session)


# ---------------------------------------------------------------------------
# SftpConfig
# ---------------------------------------------------------------------------


def test_config_from_env_with_prefix_and_password() -> None:
    env = {
        "GBR_SFTP_HOST": "ec2-host",
        "GBR_SFTP_USERNAME": "wbysltd",
        "GBR_SFTP_PASSWORD": "secret",
    }
    cfg = SftpConfig.from_env(prefix="GBR_SFTP", env=env)
    assert cfg.host == "ec2-host"
    assert cfg.username == "wbysltd"
    assert cfg.password == "secret"
    assert cfg.port == 22
    assert cfg.key_path is None


def test_config_from_env_with_custom_port() -> None:
    env = {
        "SFTP_HOST": "h",
        "SFTP_USERNAME": "u",
        "SFTP_PASSWORD": "p",
        "SFTP_PORT": "2222",
    }
    assert SftpConfig.from_env(env=env).port == 2222


def test_config_from_env_accepts_key_path_instead_of_password() -> None:
    env = {
        "SFTP_HOST": "h",
        "SFTP_USERNAME": "u",
        "SFTP_KEY_PATH": "/home/app/.ssh/id_rsa",
    }
    cfg = SftpConfig.from_env(env=env)
    assert cfg.key_path == "/home/app/.ssh/id_rsa"
    assert cfg.password is None


def test_config_from_env_requires_host_username_and_secret() -> None:
    with pytest.raises(RuntimeError, match="SFTP_HOST"):
        SftpConfig.from_env(env={})
    with pytest.raises(RuntimeError, match="PASSWORD or"):
        SftpConfig.from_env(env={"SFTP_HOST": "h", "SFTP_USERNAME": "u"})


def test_config_from_env_rejects_non_numeric_port() -> None:
    env = {
        "SFTP_HOST": "h",
        "SFTP_USERNAME": "u",
        "SFTP_PASSWORD": "p",
        "SFTP_PORT": "not-a-port",
    }
    with pytest.raises(RuntimeError, match="Invalid SFTP_PORT"):
        SftpConfig.from_env(env=env)


# ---------------------------------------------------------------------------
# SftpClient
# ---------------------------------------------------------------------------


def test_upload_text_encodes_utf8(
    client: SftpClient, session: FakeSession
) -> None:
    client.upload_text("SKU,Quantity\r\nABC,1\r\n", "/JIT/Orders/test.csv")
    assert session.files["/JIT/Orders/test.csv"] == b"SKU,Quantity\r\nABC,1\r\n"


def test_upload_bytes_stores_raw(
    client: SftpClient, session: FakeSession
) -> None:
    client.upload_bytes(b"\x00\x01\x02", "/x.bin")
    assert session.files["/x.bin"] == b"\x00\x01\x02"


def test_download_text_roundtrip(
    client: SftpClient, session: FakeSession
) -> None:
    session.files["/JIT/Notifications/a.csv"] = "hello\r\n".encode("utf-8")
    assert client.download_text("/JIT/Notifications/a.csv") == "hello\r\n"


def test_download_bytes_roundtrip(
    client: SftpClient, session: FakeSession
) -> None:
    session.files["/x.bin"] = b"\xff\xfe\xfd"
    assert client.download_bytes("/x.bin") == b"\xff\xfe\xfd"


def test_list_dir_returns_filenames(
    client: SftpClient, session: FakeSession
) -> None:
    session.listings["/JIT/Notifications"] = ["a.csv", "b.csv"]
    assert client.list_dir("/JIT/Notifications") == ["a.csv", "b.csv"]


def test_remove_deletes_file(
    client: SftpClient, session: FakeSession
) -> None:
    session.files["/f"] = b"x"
    client.remove("/f")
    assert "/f" not in session.files


def test_rename_moves_file(client: SftpClient, session: FakeSession) -> None:
    session.files["/from"] = b"x"
    client.rename("/from", "/to")
    assert "/from" not in session.files
    assert session.files["/to"] == b"x"


def test_connect_is_lazy(config: SftpConfig) -> None:
    calls = {"n": 0}

    def connect(_cfg: SftpConfig) -> FakeSession:
        calls["n"] += 1
        return FakeSession()

    client = SftpClient(config, connect=connect)
    assert calls["n"] == 0
    client.upload_text("x", "/y")
    assert calls["n"] == 1
    # Subsequent operations reuse the session.
    client.upload_text("x", "/z")
    assert calls["n"] == 1


def test_close_closes_underlying_session(
    client: SftpClient, session: FakeSession
) -> None:
    client.upload_text("x", "/y")
    assert not session.closed
    client.close()
    assert session.closed


def test_context_manager_closes_session(config: SftpConfig) -> None:
    session = FakeSession()
    with SftpClient(config, connect=lambda _c: session) as client:
        client.upload_text("x", "/y")
    assert session.closed


def test_context_manager_closes_on_exception(config: SftpConfig) -> None:
    session = FakeSession()
    with pytest.raises(RuntimeError):
        with SftpClient(config, connect=lambda _c: session) as client:
            client.upload_text("x", "/y")
            raise RuntimeError("boom")
    assert session.closed


def test_close_is_idempotent(client: SftpClient) -> None:
    client.close()
    client.close()  # must not raise
