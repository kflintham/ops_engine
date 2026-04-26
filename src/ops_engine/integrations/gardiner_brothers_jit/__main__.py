"""Command-line entry point for the GBR JIT integration.

Usage::

    python -m ops_engine.integrations.gardiner_brothers_jit <command> [options]

Commands:

- ``discover``         -- print a paste-ready .env.local snippet of the
                          Brightpearl IDs the integration needs
                          (supplier, price list, two PO statuses).
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
import logging
import sys

from ops_engine.core.brightpearl import BrightpearlClient, BrightpearlConfig
from ops_engine.core.sftp import SftpClient, SftpConfig

from . import discovery
from .config import GbrJitConfig
from .outbound import run_outbound
from .setup import ensure_remote_folders


_SFTP_PREFIX = "GBR_SFTP"


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
