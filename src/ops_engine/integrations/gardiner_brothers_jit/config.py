"""Runtime configuration for the Gardiner Brothers JIT integration.

Values that never come from Brightpearl at runtime and therefore need to be
captured once per environment (dev / staging / prod). All values are loaded
from environment variables so secrets / account-specific IDs live in a
gitignored .env.local rather than in the repo.

See the repo-root ``.env.example`` for a template of the variables, and
``docs/gardiner-brothers-jit/field-mapping.md`` for what each value is used
for and how to obtain it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


_ENV_PREFIX = "GBR_JIT"


@dataclass(frozen=True)
class GbrJitConfig:
    # Brightpearl IDs -- resolved once from the BP account.
    gardiners_jit_supplier_contact_id: int
    gardiners_price_list_id: int
    status_id_request_sent: int
    status_id_pending: int
    # Inbound status transitions, driven by Gardiners notification files.
    status_id_acknowledged: int
    status_id_order_fulfilled: int
    status_id_cancelled: int

    # SFTP folder conventions (agree with Gardiners before go-live).
    orders_remote_path: str
    notifications_remote_path: str
    # Where processed notification files are moved to after we apply them.
    # Defaults to ``<notifications_remote_path>/processed/``.
    notifications_processed_path: str = ""

    # File naming -- applied to the outbound CSV before upload. Supported
    # placeholders are ``{order_reference}`` and ``{timestamp}``.
    file_name_template: str = "{order_reference}-{timestamp}.csv"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GbrJitConfig":
        env = env if env is not None else os.environ

        missing: list[str] = []

        def _required_int(name: str) -> int:
            full = f"{_ENV_PREFIX}_{name}"
            raw = env.get(full)
            if not raw:
                missing.append(full)
                return 0
            try:
                return int(raw)
            except ValueError as exc:
                raise RuntimeError(f"Invalid integer for {full}: {raw!r}") from exc

        def _required_str(name: str) -> str:
            full = f"{_ENV_PREFIX}_{name}"
            value = env.get(full) or ""
            if not value:
                missing.append(full)
            return value

        supplier_id = _required_int("SUPPLIER_CONTACT_ID")
        price_list_id = _required_int("PRICE_LIST_ID")
        request_sent = _required_int("STATUS_ID_REQUEST_SENT")
        pending = _required_int("STATUS_ID_PENDING")
        acknowledged = _required_int("STATUS_ID_ACKNOWLEDGED")
        order_fulfilled = _required_int("STATUS_ID_ORDER_FULFILLED")
        cancelled = _required_int("STATUS_ID_CANCELLED")
        orders_path = _required_str("ORDERS_PATH")
        notifications_path = _required_str("NOTIFICATIONS_PATH")

        if missing:
            raise RuntimeError(
                "Missing GBR JIT environment variables: " + ", ".join(missing)
            )

        template = env.get(f"{_ENV_PREFIX}_FILE_NAME_TEMPLATE") or cls.file_name_template
        processed_path = (
            env.get(f"{_ENV_PREFIX}_NOTIFICATIONS_PROCESSED_PATH")
            or _default_processed_path(notifications_path)
        )

        return cls(
            gardiners_jit_supplier_contact_id=supplier_id,
            gardiners_price_list_id=price_list_id,
            status_id_request_sent=request_sent,
            status_id_pending=pending,
            status_id_acknowledged=acknowledged,
            status_id_order_fulfilled=order_fulfilled,
            status_id_cancelled=cancelled,
            orders_remote_path=orders_path,
            notifications_remote_path=notifications_path,
            notifications_processed_path=processed_path,
            file_name_template=template,
        )


def _default_processed_path(notifications_path: str) -> str:
    """Default to a 'processed' subfolder of the notifications folder."""
    base = notifications_path.rstrip("/")
    return f"{base}/processed/"
