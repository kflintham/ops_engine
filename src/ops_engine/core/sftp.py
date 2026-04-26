"""SFTP client for ops_engine integrations.

Generic, integration-agnostic wrapper around paramiko's SFTPClient. It
handles authentication, session lifecycle, and the file operations our
integrations need (upload, download, list, remove, rename).

Each integration supplies its own credentials via environment variables
with an integration-specific prefix, e.g. ``GBR_SFTP_HOST`` /
``GBR_SFTP_USERNAME`` / ``GBR_SFTP_PASSWORD``. See ``.env.example``.

Tests inject a fake session via the ``connect`` parameter so no real
network or paramiko code is exercised.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


DEFAULT_PORT = 22


@dataclass(frozen=True)
class SftpConfig:
    host: str
    username: str
    password: str | None = None
    key_path: str | None = None
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(
        cls,
        prefix: str = "SFTP",
        env: Mapping[str, str] | None = None,
    ) -> "SftpConfig":
        env = env if env is not None else os.environ

        host = env.get(f"{prefix}_HOST") or ""
        username = env.get(f"{prefix}_USERNAME") or ""
        password = env.get(f"{prefix}_PASSWORD") or None
        key_path = env.get(f"{prefix}_KEY_PATH") or None
        port_raw = env.get(f"{prefix}_PORT") or str(DEFAULT_PORT)

        missing: list[str] = []
        if not host:
            missing.append(f"{prefix}_HOST")
        if not username:
            missing.append(f"{prefix}_USERNAME")
        if not password and not key_path:
            missing.append(f"{prefix}_PASSWORD or {prefix}_KEY_PATH")
        if missing:
            raise RuntimeError(
                "Missing SFTP environment variables: " + ", ".join(missing)
            )

        try:
            port = int(port_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid {prefix}_PORT value: {port_raw!r}"
            ) from exc

        return cls(
            host=host,
            username=username,
            password=password,
            key_path=key_path,
            port=port,
        )


class _SftpSession(Protocol):
    def putfo(self, fl: Any, remotepath: str) -> Any: ...
    def getfo(self, remotepath: str, fl: Any) -> Any: ...
    def listdir(self, path: str = ".") -> list[str]: ...
    def remove(self, path: str) -> None: ...
    def rename(self, oldpath: str, newpath: str) -> None: ...
    def close(self) -> None: ...


ConnectFn = Callable[[SftpConfig], _SftpSession]


class SftpClient:
    def __init__(
        self,
        config: SftpConfig,
        *,
        connect: ConnectFn | None = None,
    ) -> None:
        self._config = config
        self._connect = connect if connect is not None else _paramiko_connect
        self._session: _SftpSession | None = None

    def __enter__(self) -> "SftpClient":
        self._open()
        return self

    def __exit__(self, *_excinfo: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def upload_bytes(self, data: bytes, remote_path: str) -> None:
        self._open().putfo(io.BytesIO(data), remote_path)

    def upload_text(
        self, text: str, remote_path: str, encoding: str = "utf-8"
    ) -> None:
        self.upload_bytes(text.encode(encoding), remote_path)

    def download_bytes(self, remote_path: str) -> bytes:
        buffer = io.BytesIO()
        self._open().getfo(remote_path, buffer)
        return buffer.getvalue()

    def download_text(self, remote_path: str, encoding: str = "utf-8") -> str:
        return self.download_bytes(remote_path).decode(encoding)

    def list_dir(self, remote_path: str = ".") -> list[str]:
        return list(self._open().listdir(remote_path))

    def remove(self, remote_path: str) -> None:
        self._open().remove(remote_path)

    def rename(self, source: str, destination: str) -> None:
        self._open().rename(source, destination)

    def _open(self) -> _SftpSession:
        if self._session is None:
            self._session = self._connect(self._config)
        return self._session


def _paramiko_connect(config: SftpConfig) -> _SftpSession:
    # Lazy import so tests that supply their own ``connect`` don't need
    # paramiko installed in the environment.
    import paramiko  # type: ignore[import-untyped]

    transport = paramiko.Transport((config.host, config.port))
    try:
        if config.key_path:
            pkey = paramiko.RSAKey.from_private_key_file(config.key_path)
            transport.connect(username=config.username, pkey=pkey)
        else:
            transport.connect(username=config.username, password=config.password)
        sftp = paramiko.SFTPClient.from_transport(transport)
    except Exception:
        transport.close()
        raise

    if sftp is None:
        transport.close()
        raise RuntimeError("paramiko could not open an SFTP channel")

    return _ParamikoSession(sftp, transport)


class _ParamikoSession:
    """Bundle paramiko's SFTPClient and Transport so close() cleans up both."""

    def __init__(self, sftp: Any, transport: Any) -> None:
        self._sftp = sftp
        self._transport = transport

    def putfo(self, fl: Any, remotepath: str) -> Any:
        return self._sftp.putfo(fl, remotepath)

    def getfo(self, remotepath: str, fl: Any) -> Any:
        return self._sftp.getfo(remotepath, fl)

    def listdir(self, path: str = ".") -> list[str]:
        return self._sftp.listdir(path)

    def remove(self, path: str) -> None:
        self._sftp.remove(path)

    def rename(self, oldpath: str, newpath: str) -> None:
        self._sftp.rename(oldpath, newpath)

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._transport.close()
