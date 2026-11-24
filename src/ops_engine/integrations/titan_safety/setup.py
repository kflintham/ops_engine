"""One-off setup for the Titan Safety integration.

Creates the SFTP folder the inbox poller drops attachments into (and
the processed subfolder the sync archives to). Run once per
environment before the first poll / sync.
"""
from __future__ import annotations

from dataclasses import dataclass

from ops_engine.core.sftp import SftpClient

from .config import TitanSafetyConfig


@dataclass(frozen=True)
class FolderSetupResult:
    ensured: tuple[str, ...]


def ensure_remote_folders(
    sftp: SftpClient, config: TitanSafetyConfig
) -> FolderSetupResult:
    paths = (config.stock_remote_path, config.stock_processed_path)
    for path in paths:
        sftp.ensure_dir(path)
    return FolderSetupResult(ensured=paths)
