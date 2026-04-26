from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ops_engine.core.brightpearl import (
    BrightpearlClient,
    BrightpearlConfig,
    BrightpearlError,
)


# ---------------------------------------------------------------------------
# Fakes -- no real HTTP traffic in any of these tests.
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    text: str = ""

    @property
    def content(self) -> bytes:
        return b"" if self.body is None else b"not-empty"

    def json(self) -> Any:
        if self.body is None:
            raise ValueError("no body")
        return self.body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError("FakeSession ran out of responses")
        return self._responses.pop(0)


@pytest.fixture
def config() -> BrightpearlConfig:
    return BrightpearlConfig(
        account_code="wbys",
        datacenter="use1",
        app_ref="wbys_ops_engine",
        account_token="secret-token",
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_base_url() -> None:
    cfg = BrightpearlConfig(
        account_code="wbys",
        datacenter="use1",
        app_ref="app",
        account_token="token",
    )
    assert cfg.base_url == "https://use1.brightpearlconnect.com/public-api/wbys"


def test_config_from_env_happy_path() -> None:
    env = {
        "BRIGHTPEARL_ACCOUNT_CODE": "wbys",
        "BRIGHTPEARL_DATACENTER": "use1",
        "BRIGHTPEARL_APP_REF": "app",
        "BRIGHTPEARL_ACCOUNT_TOKEN": "token",
    }
    cfg = BrightpearlConfig.from_env(env)
    assert cfg.account_code == "wbys"
    assert cfg.datacenter == "use1"
    assert cfg.app_ref == "app"
    assert cfg.account_token == "token"


def test_config_from_env_reports_missing_vars() -> None:
    with pytest.raises(RuntimeError, match="BRIGHTPEARL_ACCOUNT_CODE"):
        BrightpearlConfig.from_env({"BRIGHTPEARL_DATACENTER": "use1"})


# ---------------------------------------------------------------------------
# Client -- URLs, headers, success path
# ---------------------------------------------------------------------------


def test_get_builds_url_and_unwraps_response_envelope(
    config: BrightpearlConfig,
) -> None:
    session = FakeSession(
        [FakeResponse(status_code=200, body={"response": {"id": 123}})]
    )
    client = BrightpearlClient(config, session=session)

    result = client.get("/order-service/order/123")

    assert result == {"id": 123}
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == (
        "https://use1.brightpearlconnect.com/public-api/wbys/order-service/order/123"
    )


def test_request_sends_auth_headers(config: BrightpearlConfig) -> None:
    session = FakeSession([FakeResponse(status_code=200, body={"response": []})])
    client = BrightpearlClient(config, session=session)

    client.get("/anything")

    headers = session.calls[0]["headers"]
    assert headers["brightpearl-app-ref"] == "wbys_ops_engine"
    assert headers["brightpearl-account-token"] == "secret-token"
    assert headers["accept"] == "application/json"


def test_path_without_leading_slash_still_works(config: BrightpearlConfig) -> None:
    session = FakeSession([FakeResponse(status_code=200, body={"response": {}})])
    client = BrightpearlClient(config, session=session)

    client.get("product-service/product/1")

    assert session.calls[0]["url"].endswith("/public-api/wbys/product-service/product/1")


def test_response_without_envelope_returned_as_is(config: BrightpearlConfig) -> None:
    session = FakeSession([FakeResponse(status_code=200, body=[1, 2, 3])])
    client = BrightpearlClient(config, session=session)
    assert client.get("/x") == [1, 2, 3]


def test_empty_response_returns_none(config: BrightpearlConfig) -> None:
    session = FakeSession([FakeResponse(status_code=204, body=None)])
    client = BrightpearlClient(config, session=session)
    assert client.post("/x", json={"a": 1}) is None


# ---------------------------------------------------------------------------
# Errors and retries
# ---------------------------------------------------------------------------


def test_4xx_raises_brightpearl_error_with_body(config: BrightpearlConfig) -> None:
    session = FakeSession(
        [
            FakeResponse(
                status_code=404,
                body={"errors": [{"message": "Order not found"}]},
                reason="Not Found",
            )
        ]
    )
    client = BrightpearlClient(config, session=session)

    with pytest.raises(BrightpearlError) as excinfo:
        client.get("/order-service/order/999")

    assert excinfo.value.status == 404
    assert excinfo.value.body == {"errors": [{"message": "Order not found"}]}
    # The error message must include the body so operators can debug.
    assert "Order not found" in str(excinfo.value)


def test_brightpearl_error_truncates_very_long_bodies(
    config: BrightpearlConfig,
) -> None:
    long_body = {"detail": "x" * 5000}
    session = FakeSession(
        [FakeResponse(status_code=500, body=long_body, reason="Internal Server Error")]
    )
    client = BrightpearlClient(
        config, session=session, max_retries=0, sleep=lambda _s: None
    )
    with pytest.raises(BrightpearlError) as excinfo:
        client.get("/x")
    assert "..." in str(excinfo.value)


def test_429_is_retried_honouring_retry_after(config: BrightpearlConfig) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            FakeResponse(status_code=429, headers={"Retry-After": "2"}),
            FakeResponse(status_code=200, body={"response": {"ok": True}}),
        ]
    )
    client = BrightpearlClient(
        config,
        session=session,
        max_retries=2,
        sleep=sleeps.append,
    )

    result = client.get("/anything")

    assert result == {"ok": True}
    assert sleeps == [2.0]
    assert len(session.calls) == 2


def test_5xx_is_retried_with_exponential_backoff(config: BrightpearlConfig) -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            FakeResponse(status_code=503),
            FakeResponse(status_code=503),
            FakeResponse(status_code=200, body={"response": 1}),
        ]
    )
    client = BrightpearlClient(
        config,
        session=session,
        max_retries=3,
        sleep=sleeps.append,
    )

    assert client.get("/x") == 1
    assert sleeps == [1.0, 2.0]  # 2**0, 2**1


def test_retry_gives_up_after_max_retries(config: BrightpearlConfig) -> None:
    responses = [FakeResponse(status_code=503, reason="Service Unavailable")] * 4
    client = BrightpearlClient(
        config,
        session=FakeSession(responses),
        max_retries=3,
        sleep=lambda _s: None,
    )

    with pytest.raises(BrightpearlError) as excinfo:
        client.get("/x")
    assert excinfo.value.status == 503
