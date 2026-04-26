"""Brightpearl REST API client.

Generic, integration-agnostic wrapper around Brightpearl's public API. It
handles authentication, URL construction, retry on transient failures, and
unwrapping Brightpearl's ``{"response": ...}`` envelope. Integration code
(e.g. Gardiner Brothers JIT) is responsible for knowing which endpoints to
call and how to map the returned JSON into domain objects.

Credentials are loaded from environment variables (see ``.env.example``):

- ``BRIGHTPEARL_ACCOUNT_CODE`` -- the account subdomain in your Brightpearl URL
- ``BRIGHTPEARL_DATACENTER``   -- e.g. ``use1``, ``eu1``, ``ws-eu1``
- ``BRIGHTPEARL_APP_REF``      -- your registered app reference
- ``BRIGHTPEARL_ACCOUNT_TOKEN`` -- secret account/system token
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

import requests


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RETRIES = 3
MAX_BACKOFF_SECONDS = 30

_ENV_VAR_NAMES = (
    "BRIGHTPEARL_ACCOUNT_CODE",
    "BRIGHTPEARL_DATACENTER",
    "BRIGHTPEARL_APP_REF",
    "BRIGHTPEARL_ACCOUNT_TOKEN",
)


@dataclass(frozen=True)
class BrightpearlConfig:
    account_code: str
    datacenter: str
    app_ref: str
    account_token: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BrightpearlConfig":
        env = env if env is not None else os.environ
        missing = [name for name in _ENV_VAR_NAMES if not env.get(name)]
        if missing:
            raise RuntimeError(
                "Missing Brightpearl environment variables: " + ", ".join(missing)
            )
        return cls(
            account_code=env["BRIGHTPEARL_ACCOUNT_CODE"],
            datacenter=env["BRIGHTPEARL_DATACENTER"],
            app_ref=env["BRIGHTPEARL_APP_REF"],
            account_token=env["BRIGHTPEARL_ACCOUNT_TOKEN"],
        )

    @property
    def base_url(self) -> str:
        return (
            f"https://{self.datacenter}.brightpearlconnect.com"
            f"/public-api/{self.account_code}"
        )


class BrightpearlError(Exception):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(
            f"Brightpearl API error {status}: {message}{_summarise_body(body)}"
        )
        self.status = status
        self.body = body


_BODY_SUMMARY_LIMIT = 500


def _summarise_body(body: Any) -> str:
    if body is None or body == "":
        return ""
    if isinstance(body, (dict, list)):
        try:
            text = json.dumps(body)
        except (TypeError, ValueError):
            text = repr(body)
    else:
        text = str(body)
    if len(text) > _BODY_SUMMARY_LIMIT:
        text = text[:_BODY_SUMMARY_LIMIT] + "..."
    return f" -- {text}"


class BrightpearlClient:
    def __init__(
        self,
        config: BrightpearlConfig,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Any = time.sleep,
    ) -> None:
        self._config = config
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json: Any = None) -> Any:
        return self._request("POST", path, json=json)

    def put(self, path: str, *, json: Any = None) -> Any:
        return self._request("PUT", path, json=json)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        url = self._build_url(path)
        headers = self._auth_headers()

        attempt = 0
        while True:
            response = self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                timeout=self._timeout,
            )
            if self._should_retry(response) and attempt < self._max_retries:
                self._sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue
            break

        if response.status_code >= 400:
            raise BrightpearlError(
                response.status_code,
                response.reason or "",
                _safe_json(response),
            )

        if not response.content:
            return None
        payload = response.json()
        if isinstance(payload, dict) and "response" in payload:
            return payload["response"]
        return payload

    def _build_url(self, path: str) -> str:
        normalised = path if path.startswith("/") else "/" + path
        return f"{self._config.base_url}{normalised}"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "brightpearl-app-ref": self._config.app_ref,
            "brightpearl-account-token": self._config.account_token,
            "accept": "application/json",
            "content-type": "application/json",
        }

    @staticmethod
    def _should_retry(response: requests.Response) -> bool:
        return response.status_code == 429 or response.status_code >= 500

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response.headers else None
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return float(min(2**attempt, MAX_BACKOFF_SECONDS))


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
