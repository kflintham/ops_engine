"""Runtime configuration for the Titan Safety stock-feed integration.

Titan email a daily stock XLSX to ``stockfeed@wbys.co.uk`` (a Zoho alias
on Katie's mailbox), which Zoho auto-forwards to a dedicated Gmail
account. ops_engine polls that Gmail inbox, pulls the attachment onto
WBYS's SFTP, and syncs the stock levels into Brightpearl warehouse 15
(the Virtual "Titan Safety" warehouse).

All account-specific values live in environment variables. See the
repo-root ``.env.example`` for a template.

SKU quirk: Titan ship SKUs like ``TC01-03`` / ``TC01-04``; Brightpearl
stores the same products as ``TC01-3`` / ``TC01-4`` (no leading zero
on single-digit sizes). The parser strips the leading zero from the
last ``-NN`` segment before SKU lookup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


_ENV_PREFIX = "TITAN_SAFETY"
_IMAP_ENV_PREFIX = "TITAN_SAFETY_IMAP"


@dataclass(frozen=True)
class TitanSafetyConfig:
    # Brightpearl --------------------------------------------------------
    warehouse_id: int
    location_id: int
    currency_code: str

    # SFTP folder Titan's forwarded attachments land in once the poller
    # has saved them. Reused by the stock-sync step the same way Bobcat
    # uses /home/wbysltd/SupplierStockFeeds/Bobcat/.
    stock_remote_path: str
    stock_processed_path: str
    file_name_prefix: str

    # Gmail IMAP ---------------------------------------------------------
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    imap_folder: str
    # Optional filter -- when set, only emails from senders whose address
    # contains this string are picked up. Leave empty to process any
    # email in the inbox with an xlsx attachment (safer when Katie's
    # Zoho filter already restricts what reaches the Gmail inbox).
    imap_sender_filter: str

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "TitanSafetyConfig":
        env = env if env is not None else os.environ

        missing: list[str] = []

        def _required_int(prefix: str, name: str) -> int:
            full = f"{prefix}_{name}"
            raw = env.get(full)
            if not raw:
                missing.append(full)
                return 0
            try:
                return int(raw)
            except ValueError as exc:
                raise RuntimeError(
                    f"Invalid integer for {full}: {raw!r}"
                ) from exc

        def _required_str(prefix: str, name: str) -> str:
            full = f"{prefix}_{name}"
            value = env.get(full) or ""
            if not value:
                missing.append(full)
            return value

        warehouse_id = _required_int(_ENV_PREFIX, "WAREHOUSE_ID")
        location_id = _required_int(_ENV_PREFIX, "LOCATION_ID")
        stock_path = _required_str(_ENV_PREFIX, "STOCK_PATH")

        imap_host = _required_str(_IMAP_ENV_PREFIX, "HOST")
        imap_username = _required_str(_IMAP_ENV_PREFIX, "USERNAME")
        imap_password = _required_str(_IMAP_ENV_PREFIX, "PASSWORD")

        if missing:
            raise RuntimeError(
                "Missing Titan Safety environment variables: "
                + ", ".join(missing)
            )

        currency_code = env.get(f"{_ENV_PREFIX}_CURRENCY_CODE") or "GBP"
        processed_path = (
            env.get(f"{_ENV_PREFIX}_STOCK_PROCESSED_PATH")
            or _default_processed_path(stock_path)
        )
        file_prefix = (
            env.get(f"{_ENV_PREFIX}_FILE_NAME_PREFIX") or "TITAN_STOCK_"
        )

        imap_port_raw = env.get(f"{_IMAP_ENV_PREFIX}_PORT") or "993"
        try:
            imap_port = int(imap_port_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid integer for {_IMAP_ENV_PREFIX}_PORT: "
                f"{imap_port_raw!r}"
            ) from exc

        imap_folder = env.get(f"{_IMAP_ENV_PREFIX}_FOLDER") or "INBOX"
        imap_sender_filter = env.get(f"{_IMAP_ENV_PREFIX}_SENDER_FILTER") or ""

        return cls(
            warehouse_id=warehouse_id,
            location_id=location_id,
            currency_code=currency_code,
            stock_remote_path=stock_path,
            stock_processed_path=processed_path,
            file_name_prefix=file_prefix,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_username=imap_username,
            imap_password=imap_password,
            imap_folder=imap_folder,
            imap_sender_filter=imap_sender_filter,
        )


def _default_processed_path(stock_path: str) -> str:
    base = stock_path.rstrip("/")
    return f"{base}/processed/"
