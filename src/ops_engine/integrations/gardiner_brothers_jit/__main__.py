"""Command-line entry point for the GBR JIT integration.

Usage::

    python -m ops_engine.integrations.gardiner_brothers_jit <command> [options]

Commands:

- ``discover``         -- print a paste-ready .env.local snippet of the
                          Brightpearl IDs the integration needs
                          (supplier, price list, two PO statuses).
- ``dump``             -- print the raw JSON Brightpearl returns for the
                          price-list and order-status endpoints. Useful
                          when ``discover`` reports NOT FOUND and we need
                          to see what shape the response actually has.
- ``setup-folders``    -- create ``/JIT/Orders/`` and ``/JIT/Notifications/``
                          on the SFTP server (idempotent).
- ``outbound``         -- find any POs on ``GBR JIT - Request Sent`` for
                          the JIT supplier, build their CSVs, upload to
                          SFTP, and transition them to ``GBR JIT - Pending``.

All commands read their config from environment variables; see
``.env.example`` at the repo root.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from ops_engine.core.brightpearl import BrightpearlClient, BrightpearlConfig
from ops_engine.core.sftp import SftpClient, SftpConfig

from . import discovery
from .config import GbrJitConfig
from .outbound import run_outbound
from .setup import ensure_remote_folders


_SFTP_PREFIX = "GBR_SFTP"
_DUMP_ENDPOINTS = (
    "/product-service/price-list",
    "/order-service/order-status",
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ops_engine.integrations.gardiner_brothers_jit",
        description="Operate the Gardiner Brothers JIT integration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser(
        "discover",
        help="Print Brightpearl IDs needed for .env.local",
    )
    p_discover.set_defaults(func=_cmd_discover)

    p_dump = sub.add_parser(
        "dump",
        help=(
            "Dump raw JSON from Brightpearl for the discovery endpoints "
            "(for debugging when discover can't find an entry by name)"
        ),
    )
    p_dump.set_defaults(func=_cmd_dump)

    p_dump_path = sub.add_parser(
        "dump-path",
        help="Dump raw JSON from an arbitrary Brightpearl path (for debugging)",
    )
    p_dump_path.add_argument(
        "path",
        help="Brightpearl path, e.g. /product-service/product/53095",
    )
    p_dump_path.set_defaults(func=_cmd_dump_path)

    p_list_sftp = sub.add_parser(
        "list-sftp",
        help="List files in an SFTP directory (for debugging)",
    )
    p_list_sftp.add_argument(
        "path",
        nargs="?",
        default="/",
        help="Remote path to list (default: /)",
    )
    p_list_sftp.set_defaults(func=_cmd_list_sftp)

    p_setup = sub.add_parser(
        "setup-folders",
        help="Create the JIT folders on the SFTP server",
    )
    p_setup.set_defaults(func=_cmd_setup_folders)

    p_outbound = sub.add_parser(
        "outbound",
        help="Send pending GBR JIT purchase orders to Gardiners",
    )
    p_outbound.set_defaults(func=_cmd_outbound)

    return parser.parse_args(argv)


def _cmd_discover(_args: argparse.Namespace) -> int:
    bp_cfg = BrightpearlConfig.from_env()
    bp = BrightpearlClient(bp_cfg)
    result = discovery.discover(bp)
    sys.stdout.write(discovery.format_env_snippet(result))
    return 0 if result.is_complete else 2


def _cmd_dump(_args: argparse.Namespace) -> int:
    bp_cfg = BrightpearlConfig.from_env()
    bp = BrightpearlClient(bp_cfg)
    for path in _DUMP_ENDPOINTS:
        sys.stdout.write(f"\n=== GET {path} ===\n")
        try:
            response = bp.get(path)
            sys.stdout.write(json.dumps(response, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001 -- this is a diagnostics tool
            sys.stdout.write(f"ERROR: {type(exc).__name__}: {exc}")
        sys.stdout.write("\n")
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


def _cmd_setup_folders(_args: argparse.Namespace) -> int:
    cfg = GbrJitConfig.from_env()
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)
    with SftpClient(sftp_cfg) as sftp:
        result = ensure_remote_folders(sftp, cfg)
    for path in result.ensured:
        sys.stdout.write(f"OK  {path}\n")
    return 0


def _cmd_outbound(_args: argparse.Namespace) -> int:
    cfg = GbrJitConfig.from_env()
    bp_cfg = BrightpearlConfig.from_env()
    sftp_cfg = SftpConfig.from_env(prefix=_SFTP_PREFIX)

    bp = BrightpearlClient(bp_cfg)
    with SftpClient(sftp_cfg) as sftp:
        summary = run_outbound(bp, sftp, cfg)

    for ok in summary.successes:
        sys.stdout.write(
            f"OK   PO {ok.order_id} ({ok.order_reference}) -> {ok.remote_path}\n"
        )
    for fail in summary.failures:
        sys.stderr.write(f"FAIL PO {fail.order_id}: {fail.error}\n")

    return 0 if not summary.failures else 1


if __name__ == "__main__":
    sys.exit(main())
