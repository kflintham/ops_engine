from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

import pytest

from ops_engine.integrations.titan_safety.config import TitanSafetyConfig
from ops_engine.integrations.titan_safety.inbox_poller import run_inbox_poll


FIXED_NOW = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def config() -> TitanSafetyConfig:
    return TitanSafetyConfig(
        warehouse_id=15,
        location_id=27,
        currency_code="GBP",
        stock_remote_path="/home/wbysltd/SupplierStockFeeds/TitanSafety/",
        stock_processed_path=(
            "/home/wbysltd/SupplierStockFeeds/TitanSafety/processed/"
        ),
        file_name_prefix="TITAN_STOCK_",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="wbysstockfeed@gmail.com",
        imap_password="secret",
        imap_folder="INBOX",
        imap_sender_filter="",
    )


def _email_with_attachment(
    filename: str, body: bytes, subject: str = "Titan stock"
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "titan@example.com"
    msg["To"] = "stockfeed@wbys.co.uk"
    msg.set_content("See attached.")
    msg.add_attachment(
        body,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
    return msg.as_bytes()


def _email_no_attachment(subject: str = "Notes") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "someone@example.com"
    msg["To"] = "stockfeed@wbys.co.uk"
    msg.set_content("No attachment here.")
    return msg.as_bytes()


class FakeImap:
    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self.messages = dict(messages)
        self.selected: str | None = None
        self.searched: list[tuple[Any, ...]] = []
        self.stored_flags: list[tuple[bytes, str, str]] = []
        self.logged_out = False

    def select(self, mailbox: str) -> Any:
        self.selected = mailbox
        return "OK", [b"OK"]

    def search(self, charset: Any, *criteria: str) -> Any:
        self.searched.append(tuple(criteria))
        return "OK", [b" ".join(self.messages.keys())]

    def fetch(self, message_set: bytes, message_parts: str) -> Any:
        return "OK", [(b"1 (RFC822 {1234}", self.messages[message_set])]

    def store(self, message_set: bytes, command: str, flags: str) -> Any:
        self.stored_flags.append((message_set, command, flags))
        return "OK", [b"OK"]

    def logout(self) -> Any:
        self.logged_out = True
        return "BYE", [b"OK"]


class FakeSftp:
    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}
        self.ensured: list[str] = []

    def ensure_dir(self, path: str) -> None:
        self.ensured.append(path)

    def upload_bytes(self, data: bytes, remote_path: str) -> None:
        self.uploads[remote_path] = data


def _now() -> datetime:
    return FIXED_NOW


def test_saves_xlsx_attachment_to_sftp(config: TitanSafetyConfig) -> None:
    body = b"PK\x03\x04" + b"\0" * 20  # fake binary content
    imap = FakeImap({b"7": _email_with_attachment("stock.xlsx", body)})
    sftp = FakeSftp()

    result = run_inbox_poll(
        sftp, config, connect=lambda _: imap, now=_now
    )

    assert result.ok
    assert len(result.saved) == 1
    saved = result.saved[0]
    assert saved.message_id == "7"
    assert saved.remote_path.startswith(
        "/home/wbysltd/SupplierStockFeeds/TitanSafety/TITAN_STOCK_20261124080000-"
    )
    assert saved.remote_path.endswith("stock.xlsx")
    assert sftp.uploads[saved.remote_path] == body
    # Message marked as seen.
    assert imap.stored_flags == [(b"7", "+FLAGS", "\\Seen")]
    assert imap.logged_out


def test_marks_seen_and_records_when_no_xlsx_attached(
    config: TitanSafetyConfig,
) -> None:
    imap = FakeImap({b"3": _email_no_attachment()})
    sftp = FakeSftp()

    result = run_inbox_poll(
        sftp, config, connect=lambda _: imap, now=_now
    )

    assert result.ok
    assert result.saved == []
    assert result.skipped_without_attachment == ["3"]
    assert imap.stored_flags == [(b"3", "+FLAGS", "\\Seen")]


def test_applies_sender_filter_when_set(config: TitanSafetyConfig) -> None:
    filtered = TitanSafetyConfig(
        **{**config.__dict__, "imap_sender_filter": "titan@example.com"}
    )
    imap = FakeImap({b"1": _email_with_attachment("stock.xlsx", b"PK\x03\x04")})
    sftp = FakeSftp()

    run_inbox_poll(sftp, filtered, connect=lambda _: imap, now=_now)

    assert imap.searched == [("UNSEEN", "FROM", "titan@example.com")]


def test_ensures_target_folder_exists(config: TitanSafetyConfig) -> None:
    imap = FakeImap({})
    sftp = FakeSftp()
    run_inbox_poll(sftp, config, connect=lambda _: imap, now=_now)
    assert config.stock_remote_path in sftp.ensured


def test_handles_multiple_messages_independently(
    config: TitanSafetyConfig,
) -> None:
    imap = FakeImap(
        {
            b"1": _email_with_attachment("a.xlsx", b"PK\x03\x04"),
            b"2": _email_no_attachment(),
            b"3": _email_with_attachment("b.xlsx", b"PK\x05\x06"),
        }
    )
    sftp = FakeSftp()

    result = run_inbox_poll(
        sftp, config, connect=lambda _: imap, now=_now
    )

    assert result.ok
    assert {s.message_id for s in result.saved} == {"1", "3"}
    assert result.skipped_without_attachment == ["2"]
    # All three got +FLAGS Seen (so we don't look at them again).
    seen_flags = [m for m, cmd, _ in imap.stored_flags if cmd == "+FLAGS"]
    assert set(seen_flags) == {b"1", b"2", b"3"}


def test_records_error_when_fetch_fails(config: TitanSafetyConfig) -> None:
    class BrokenImap(FakeImap):
        def fetch(self, message_set: bytes, message_parts: str) -> Any:
            return "NO", [b"fetch failed"]

    imap = BrokenImap({b"1": _email_with_attachment("a.xlsx", b"PK\x03\x04")})
    sftp = FakeSftp()

    result = run_inbox_poll(
        sftp, config, connect=lambda _: imap, now=_now
    )

    assert not result.ok
    assert len(result.errors) == 1
    assert result.errors[0][0] == "1"
