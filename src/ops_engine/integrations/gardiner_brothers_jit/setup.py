"""One-off setup operations for the GBR JIT integration.

Currently just ensures the SFTP folder structure Gardiners and the app
agree on actually exists on the WBYS-owned SFTP server. Run this once
per environment before the outbound pipeline does its first upload.
"""
from __future__ import annotations

from dataclasses import dataclass

from ops_engine.core.sftp import SftpClient

from .config import GbrJitConfig


@dataclass(frozen=True)
class FolderSetupResult:
    ensured: tuple[str, ...]


def ensure_remote_folders(
    sftp: SftpClient, config: GbrJitConfig
) -> FolderSetupResult:
    paths = (config.orders_remote_path, config.notifications_remote_path)
    for path in paths:
        sftp.ensure_dir(path)
    return FolderSetupResult(ensured=paths)
