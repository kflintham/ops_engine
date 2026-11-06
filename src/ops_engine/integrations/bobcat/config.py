"""Runtime configuration for the Bobcat stock-feed integration.

Bobcat drop a daily stock CSV onto WBYS's own SFTP server. This integration
picks it up, converts the ``+10 Available`` flag into a stock level, and
pushes the levels into a dedicated Brightpearl warehouse (the virtual
"Bobcat" warehouse, ID 14 on the production account).

All account-specific values live in environment variables so secrets and
IDs stay out of the repo. See the repo-root ``.env.example`` for a
template.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


_ENV_PREFIX = "BOBCAT"


@dataclass(frozen=True)
class BobcatConfig:
    # Brightpearl warehouse the stock levels land in. The warehouse is a
    # Virtual warehouse named "Bobcat" on the WBYS production account.
    warehouse_id: int

    # Brightpearl location ID inside the warehouse. Stock corrections have
    # to target a specific location (shelf/bin). A simple warehouse has
    # one default location; look it up once via
    # ``dump-path /warehouse-service/warehouse/{wh}/location`` and pin
    # here.
    location_id: int

    # Stock level applied when the feed says "Yes" against +10 Available.
    # Bobcat guarantee at least ten units are on hand when they flag Yes.
    in_stock_quantity: int

    # Currency code for the cost value Brightpearl requires on each
    # correction. WBYS trade in GBP.
    currency_code: str

    # SFTP folder Bobcat (via WBYS) drop the daily CSV into.
    stock_remote_path: str

    # Where processed files are moved to after we apply them. Defaults to
    # ``<stock_remote_path>/processed/``.
    stock_processed_path: str = ""

    # Filename pattern Bobcat use. Used to filter the stock folder's
    # contents to just the feed files and to validate we're looking at the
    # right drop. Must match ``BC_STOCK_YYYYMMDDHHMMSS.csv`` or similar;
    # we match on the ``BC_STOCK_`` prefix and ``.csv`` suffix.
    file_name_prefix: str = "BC_STOCK_"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BobcatConfig":
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

        warehouse_id = _required_int("WAREHOUSE_ID")
        location_id = _required_int("LOCATION_ID")
        stock_path = _required_str("STOCK_PATH")

        in_stock_raw = env.get(f"{_ENV_PREFIX}_IN_STOCK_QUANTITY") or "10"
        try:
            in_stock_quantity = int(in_stock_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid integer for {_ENV_PREFIX}_IN_STOCK_QUANTITY: "
                f"{in_stock_raw!r}"
            ) from exc

        currency_code = env.get(f"{_ENV_PREFIX}_CURRENCY_CODE") or "GBP"

        if missing:
            raise RuntimeError(
                "Missing Bobcat environment variables: " + ", ".join(missing)
            )

        processed_path = (
            env.get(f"{_ENV_PREFIX}_STOCK_PROCESSED_PATH")
            or _default_processed_path(stock_path)
        )
        file_prefix = (
            env.get(f"{_ENV_PREFIX}_FILE_NAME_PREFIX") or cls.file_name_prefix
        )

        return cls(
            warehouse_id=warehouse_id,
            location_id=location_id,
            in_stock_quantity=in_stock_quantity,
            currency_code=currency_code,
            stock_remote_path=stock_path,
            stock_processed_path=processed_path,
            file_name_prefix=file_prefix,
        )


def _default_processed_path(stock_path: str) -> str:
    base = stock_path.rstrip("/")
    return f"{base}/processed/"
