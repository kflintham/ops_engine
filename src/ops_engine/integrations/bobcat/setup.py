"""One-off setup for the Bobcat stock-feed integration.

Ensures the SFTP folders the pipeline reads from and writes to actually
exist. Run once per environment before the first sync.
"""
from __future__ import annotations

from dataclasses import dataclass

from ops_engine.core.sftp import SftpClient

from .config import BobcatConfig


@dataclass(frozen=True)
class FolderSetupResult:
    ensured: tuple[str, ...]


def ensure_remote_folders(
    sftp: SftpClient, config: BobcatConfig
) -> FolderSetupResult:
    paths = (config.stock_remote_path, config.stock_processed_path)
    for path in paths:
        sftp.ensure_dir(path)
    return FolderSetupResult(ensured=paths)
