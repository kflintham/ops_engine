"""Command-line entry point for the Titan Safety integration.

Usage::

    python -m ops_engine.integrations.titan_safety <command> [options]

Commands:

- ``setup-folders`` -- create the Titan Safety SFTP folder (and its
                       processed subfolder). Idempotent.
- ``poll-inbox``    -- connect to Gmail via IMAP, download xlsx
                       attachments from unread emails, drop them onto
                       SFTP, and mark the emails as seen.
- ``sync``          -- find the newest xlsx on SFTP, parse it, and
                       write the stock levels to Brightpearl warehouse 15.
- ``list-sftp``     -- list files in an SFTP directory (debugging).
- ``list-locations`` -- list the locations inside the Titan Safety
                       warehouse (for BOBCAT-style one-off setup).
- ``dump-path``     -- dump raw JSON from an arbitrary Brightpearl path
                       (debugging).

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

from .config import TitanSafetyConfig
from .inbox_poller import run_inbox_poll
from .setup import ensure_remote_folders
from .stock_sync import run_stock_sync


# Titan uses the same WBYS SFTP as Bobcat and Gardiners -- the forwarded
# attachments land next to the other suppliers' feeds.
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
        prog="python -m ops_engine.integrations.titan_safety",
        description="Operate the Titan Safety stock-feed integration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser(
        "setup-folders",
        help="Create the Titan Safety SFTP folders",
    )
    p_setup.set_defaults(func=_cmd_setup_folders)

    p_poll = sub.add_parser(
        "poll-inbox",
        help="Download xlsx attachments from the dedicated Gmail inbox",
    )
    p_poll.set_defaults(func=_cmd_poll_inbox)

    p_sync = sub.add_parser(
        "sync",
        help="Apply the latest Titan Safety stock XLSX to Brightpearl",
    )
    p_sync.set_defaults(func=_cmd_sync)

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
    p_dump.add_argument(
        "path",
        help="Brightpearl path, e.g. /warehouse-service/warehouse/15/location",
    )
    p_dump.set_defaults(func=_cmd_dump_path)

    p_locations = sub.add_parser(
        "list-locations",
        help="List the location IDs inside the Titan Safety warehouse (for .env.local)",
    )
    p_locations.set_defaults(func=_cmd_list_locations)

    return parser.parse_args(argv)


def _cmd_setup_folders(_args: argparse.Namespace) -> int:
    cfg = TitanSafetyConfig.from_env()
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)
    with SftpClient(sftp_cfg) as sftp:
        result = ensure_remote_folders(sftp, cfg)
    for path in result.ensured:
        sys.stdout.write(f"OK  {path}\n")
    return 0


def _cmd_poll_inbox(_args: argparse.Namespace) -> int:
    cfg = TitanSafetyConfig.from_env()
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)
    with SftpClient(sftp_cfg) as sftp:
        result = run_inbox_poll(sftp, cfg)
    for saved in result.saved:
        sys.stdout.write(
            f"OK   message {saved.message_id} -> {saved.remote_path} "
            f"({saved.bytes_written} bytes)\n"
        )
    for msg_id in result.skipped_without_attachment:
        sys.stdout.write(f"SKIP message {msg_id}: no xlsx attachment\n")
    for msg_id, err in result.errors:
        sys.stderr.write(f"FAIL message {msg_id}: {err}\n")
    return 0 if result.ok else 1


def _cmd_sync(_args: argparse.Namespace) -> int:
    cfg = TitanSafetyConfig.from_env()
    bp_cfg = BrightpearlConfig.from_env()
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)

    bp = BrightpearlClient(bp_cfg)
    with SftpClient(sftp_cfg) as sftp:
        summary = run_stock_sync(bp, sftp, cfg)

    if summary.filename is None:
        sys.stdout.write("No Titan Safety stock file found.\n")
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
    bp_cfg = BrightpearlConfig.from_env()
    warehouse_id_raw = os.environ.get("TITAN_SAFETY_WAREHOUSE_ID")
    if not warehouse_id_raw:
        sys.stderr.write("TITAN_SAFETY_WAREHOUSE_ID is not set\n")
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


if __name__ == "__main__":
    sys.exit(main())
