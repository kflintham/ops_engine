"""Command-line entry point for the Bobcat stock-feed integration.

Usage::

    python -m ops_engine.integrations.bobcat <command> [options]

Commands:

- ``setup-folders`` -- create the Bobcat stock folder (and its processed
                       subfolder) on the SFTP server (idempotent).
- ``list-sftp``     -- list files in an SFTP directory (debugging).
- ``dump-path``     -- dump raw JSON from an arbitrary Brightpearl path
                       (debugging).
- ``sync``          -- find the latest Bobcat stock CSV on SFTP, parse it,
                       and apply the stock levels to the Bobcat warehouse
                       in Brightpearl.

All commands read their config from environment variables; see
``.env.example`` at the repo root.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from ops_engine.core.brightpearl import BrightpearlClient, BrightpearlConfig
from ops_engine.core.sftp import SftpClient, SftpConfig

from .config import BobcatConfig
from .setup import ensure_remote_folders
from .stock_sync import run_stock_sync


_SFTP_PREFIX = "BOBCAT_SFTP"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ops_engine.integrations.bobcat",
        description="Operate the Bobcat stock-feed integration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser(
        "setup-folders",
        help="Create the Bobcat SFTP folders",
    )
    p_setup.set_defaults(func=_cmd_setup_folders)

    p_list = sub.add_parser(
        "list-sftp",
        help="List files in an SFTP directory (for debugging)",
    )
    p_list.add_argument(
        "path",
        nargs="?",
        default="/",
        help="Remote path to list (default: /)",
    )
    p_list.set_defaults(func=_cmd_list_sftp)

    p_dump = sub.add_parser(
        "dump-path",
        help="Dump raw JSON from an arbitrary Brightpearl path (for debugging)",
    )
    p_dump.add_argument("path", help="Brightpearl path, e.g. /warehouse-service/warehouse/14/stock")
    p_dump.set_defaults(func=_cmd_dump_path)

    p_locations = sub.add_parser(
        "list-locations",
        help="List the location IDs inside the Bobcat warehouse (for .env.local)",
    )
    p_locations.set_defaults(func=_cmd_list_locations)

    p_sync = sub.add_parser(
        "sync",
        help="Apply the latest Bobcat stock CSV to Brightpearl",
    )
    p_sync.set_defaults(func=_cmd_sync)

    return parser.parse_args(argv)


def _cmd_setup_folders(_args: argparse.Namespace) -> int:
    cfg = BobcatConfig.from_env()
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)
    with SftpClient(sftp_cfg) as sftp:
        result = ensure_remote_folders(sftp, cfg)
    for path in result.ensured:
        sys.stdout.write(f"OK  {path}\n")
    return 0


def _cmd_list_sftp(args: argparse.Namespace) -> int:
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)
    with SftpClient(sftp_cfg) as sftp:
        try:
            entries = sorted(sftp.list_dir(args.path))
        except Exception as exc:  # noqa: BLE001 -- diagnostics tool
            sys.stderr.write(f"ERROR listing {args.path}: {exc}\n")
            return 1
    sys.stdout.write(f"Contents of {args.path}:\n")
    if not entries:
        sys.stdout.write("  (empty)\n")
    for name in entries:
        sys.stdout.write(f"  {name}\n")
    return 0


def _cmd_dump_path(args: argparse.Namespace) -> int:
    bp_cfg = BrightpearlConfig.from_env()
    bp = BrightpearlClient(bp_cfg)
    sys.stdout.write(f"\n=== GET {args.path} ===\n")
    try:
        response = bp.get(args.path)
        sys.stdout.write(json.dumps(response, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001 -- diagnostics tool
        sys.stdout.write(f"ERROR: {type(exc).__name__}: {exc}")
    sys.stdout.write("\n")
    return 0


def _cmd_list_locations(_args: argparse.Namespace) -> int:
    """Print the locations inside the Bobcat warehouse.

    Stock corrections target a specific location (shelf/bin) inside a
    warehouse. A simple Virtual warehouse usually has one default
    location; its ID is what BOBCAT_LOCATION_ID in .env.local should be
    set to.
    """
    bp_cfg = BrightpearlConfig.from_env()
    warehouse_id_raw = os.environ.get("BOBCAT_WAREHOUSE_ID")
    if not warehouse_id_raw:
        sys.stderr.write("BOBCAT_WAREHOUSE_ID is not set\n")
        return 2
    path = f"/warehouse-service/warehouse/{warehouse_id_raw}/location"
    bp = BrightpearlClient(bp_cfg)
    sys.stdout.write(f"\n=== GET {path} ===\n")
    try:
        response = bp.get(path)
    except Exception as exc:  # noqa: BLE001 -- diagnostics tool
        sys.stderr.write(f"ERROR: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(response, indent=2, default=str))
    sys.stdout.write("\n")
    return 0


def _cmd_sync(_args: argparse.Namespace) -> int:
    cfg = BobcatConfig.from_env()
    bp_cfg = BrightpearlConfig.from_env()
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)

    bp = BrightpearlClient(bp_cfg)
    with SftpClient(sftp_cfg) as sftp:
        summary = run_stock_sync(bp, sftp, cfg)

    if summary.filename is None:
        sys.stdout.write("No Bobcat stock file found.\n")
        return 0

    sys.stdout.write(f"File: {summary.filename}\n")
    for u in summary.changed:
        sys.stdout.write(
            f"OK   {u.sku} (pid={u.product_id}): "
            f"{u.previous_quantity} -> {u.new_quantity} ({u.delta:+d})\n"
        )
    unchanged = len(summary.updates) - len(summary.changed)
    if unchanged:
        sys.stdout.write(f"SKIP {unchanged} SKU(s) already at target level\n")
    for sku in summary.skipped_unknown_sku:
        sys.stderr.write(f"SKIP {sku}: not found in Brightpearl\n")
    for sku, err in summary.skipped_update_errors:
        sys.stderr.write(f"FAIL {sku}: {err}\n")
    if summary.error:
        sys.stderr.write(f"RUN FAILED: {summary.error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
