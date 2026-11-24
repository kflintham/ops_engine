"""IMAP inbox poller for Titan Safety.

Connects to the dedicated Gmail inbox that receives forwarded emails
from ``stockfeed@wbys.co.uk``, pulls out every xlsx attachment on
unread messages, and uploads each one onto the WBYS SFTP under the
Titan Safety stock folder. Marks the processed emails as seen so we
don't re-process them next tick.

The IMAP session is provided via a ``connect`` callable so tests can
inject a fake. The default connects to the real IMAP server with SSL.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from typing import Any, Callable, Protocol

from ops_engine.core.sftp import SftpClient

from .config import TitanSafetyConfig


_LOGGER = logging.getLogger("ops_engine.titan_safety.inbox_poller")
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"
_XLSX_EXTENSIONS = (".xlsx", ".xlsm")


@dataclass(frozen=True)
class SavedAttachment:
    remote_path: str
    message_id: str
    filename: str
    bytes_written: int


@dataclass
class PollResult:
    saved: list[SavedAttachment] = field(default_factory=list)
    skipped_without_attachment: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (msg_id, err)

    @property
    def ok(self) -> bool:
        return not self.errors


class _ImapSession(Protocol):
    def select(self, mailbox: str) -> Any: ...
    def search(self, charset: str | None, *criteria: str) -> Any: ...
    def fetch(self, message_set: str, message_parts: str) -> Any: ...
    def store(self, message_set: str, command: str, flags: str) -> Any: ...
    def logout(self) -> Any: ...


ConnectFn = Callable[[TitanSafetyConfig], _ImapSession]
ClockFn = Callable[[], datetime]


def run_inbox_poll(
    sftp: SftpClient,
    config: TitanSafetyConfig,
    *,
    connect: ConnectFn | None = None,
    now: ClockFn | None = None,
) -> PollResult:
    clock = now if now is not None else _default_clock
    connector = connect if connect is not None else _imaplib_connect

    sftp.ensure_dir(config.stock_remote_path)

    session = connector(config)
    result = PollResult()
    try:
        session.select(config.imap_folder)
        message_ids = _search_unseen_with_attachment(session, config)
        _LOGGER.info(
            "Gmail inbox: %d unread message(s) to consider",
            len(message_ids),
        )
        for raw_id in message_ids:
            _process_one(session, sftp, config, raw_id, clock, result)
    finally:
        try:
            session.logout()
        except Exception:  # noqa: BLE001 -- logout errors are not fatal
            _LOGGER.debug("IMAP logout raised; ignoring", exc_info=True)
    return result


def _search_unseen_with_attachment(
    session: _ImapSession, config: TitanSafetyConfig
) -> list[bytes]:
    # Gmail's IMAP supports the standard UNSEEN criterion. We don't ask
    # IMAP to filter by attachment because support varies; we filter
    # per-message after fetching.
    criteria: list[str] = ["UNSEEN"]
    if config.imap_sender_filter:
        # IMAP search FROM accepts a substring.
        criteria.extend(["FROM", config.imap_sender_filter])
    typ, data = session.search(None, *criteria)
    if typ != "OK":
        raise RuntimeError(f"IMAP SEARCH failed: {typ}")
    if not data or not data[0]:
        return []
    return data[0].split()


def _process_one(
    session: _ImapSession,
    sftp: SftpClient,
    config: TitanSafetyConfig,
    raw_id: bytes,
    clock: ClockFn,
    result: PollResult,
) -> None:
    msg_id = raw_id.decode("ascii", errors="replace")
    try:
        typ, payload = session.fetch(raw_id, "(RFC822)")
        if typ != "OK" or not payload:
            raise RuntimeError(f"FETCH returned {typ!r}")
        raw_bytes = _first_message_bytes(payload)
        if raw_bytes is None:
            raise RuntimeError("FETCH returned no message body")
        message = email.message_from_bytes(raw_bytes)
        attachments = _xlsx_attachments(message)
        if not attachments:
            result.skipped_without_attachment.append(msg_id)
            _LOGGER.info("Message %s has no xlsx attachment; skipping", msg_id)
            # Mark seen anyway so we don't look at it again.
            session.store(raw_id, "+FLAGS", "\\Seen")
            return
        for filename, body in attachments:
            saved = _save_attachment(
                sftp, config, filename, body, msg_id, clock
            )
            result.saved.append(saved)
            _LOGGER.info(
                "Saved %s (%d bytes) from message %s to %s",
                saved.filename,
                saved.bytes_written,
                msg_id,
                saved.remote_path,
            )
        session.store(raw_id, "+FLAGS", "\\Seen")
    except Exception as exc:  # noqa: BLE001 -- boundary catch per message
        _LOGGER.exception("Failed to process message %s", msg_id)
        result.errors.append((msg_id, _error_message(exc)))


def _xlsx_attachments(message: Message) -> list[tuple[str, bytes]]:
    attachments: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        lower = filename.lower()
        if not any(lower.endswith(ext) for ext in _XLSX_EXTENSIONS):
            continue
        body = part.get_payload(decode=True)
        if not body:
            continue
        attachments.append((filename, body))
    return attachments


def _save_attachment(
    sftp: SftpClient,
    config: TitanSafetyConfig,
    source_filename: str,
    body: bytes,
    message_id: str,
    clock: ClockFn,
) -> SavedAttachment:
    timestamp = clock().strftime(_TIMESTAMP_FORMAT)
    safe = _safe_for_filename(source_filename)
    remote_name = f"{config.file_name_prefix}{timestamp}-{safe}"
    remote_path = _join(config.stock_remote_path, remote_name)
    sftp.upload_bytes(body, remote_path)
    return SavedAttachment(
        remote_path=remote_path,
        message_id=message_id,
        filename=remote_name,
        bytes_written=len(body),
    )


# ---------------------------------------------------------------------------
# IMAP connection (default: real Gmail over SSL)
# ---------------------------------------------------------------------------


def _imaplib_connect(config: TitanSafetyConfig) -> _ImapSession:
    client = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
    client.login(config.imap_username, config.imap_password)
    return client


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_for_filename(name: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", name).strip("_")
    return cleaned or "titan.xlsx"


def _first_message_bytes(payload: Any) -> bytes | None:
    """Pull raw message bytes out of an IMAP FETCH response.

    imaplib returns an awkward shape: ``[(b'1 (RFC822 {1234}', b'...body...'),
    b')']`` or similar. We look for the first bytes element long enough to
    be the message.
    """
    for entry in payload:
        if isinstance(entry, tuple):
            for item in entry:
                if isinstance(item, (bytes, bytearray)) and item.startswith((b"From ", b"Delivered-To:", b"Return-Path:", b"Received:", b"X-", b"MIME-Version", b"Content-")):
                    return bytes(item)
            # Fall back to the longest bytes entry in the tuple.
            byte_entries = [i for i in entry if isinstance(i, (bytes, bytearray))]
            if byte_entries:
                return bytes(max(byte_entries, key=len))
        elif isinstance(entry, (bytes, bytearray)) and len(entry) > 64:
            return bytes(entry)
    return None


def _join(folder: str, filename: str) -> str:
    if folder.endswith("/"):
        return folder + filename
    return folder + "/" + filename


def _default_clock() -> datetime:
    return datetime.now(tz=timezone.utc)


def _error_message(exc: BaseException) -> str:
    name = type(exc).__name__
    return f"{name}: {exc}" if str(exc) else name
