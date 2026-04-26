"""Discover the Brightpearl IDs that need to live in .env.local.

Run once per environment to find:

- the contact ID of ``Gardiner Bros & Co (B1358)``
- the price list ID of ``Cost Price GBR (Net)``
- the status IDs of ``GBR JIT - Request Sent`` and ``GBR JIT - Pending``

The functions here just call Brightpearl, search the responses for known
strings, and return what they find. They are deliberately forgiving --
unknown response shapes are tolerated and surfaced as "not found"
rather than crashing.

Verified-against-live concerns are isolated in the Brightpearl endpoint
calls; the parsing is best-effort and reports its findings rather than
asserting them. The CLI prints a paste-ready ``.env.local`` snippet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ops_engine.core.brightpearl import BrightpearlClient


# Strings the discovery searches for. Configurable so we can tweak without
# editing parsing code if WBYS rename anything.
SUPPLIER_NAME = "Gardiner Bros & Co (B1358)"
PRICE_LIST_NAME = "Cost Price GBR (Net)"
STATUS_REQUEST_SENT_NAME = "GBR JIT - Request Sent"
STATUS_PENDING_NAME = "GBR JIT - Pending"


@dataclass(frozen=True)
class DiscoveryResult:
    supplier_contact_id: int | None
    price_list_id: int | None
    status_id_request_sent: int | None
    status_id_pending: int | None

    @property
    def is_complete(self) -> bool:
        return all(
            v is not None
            for v in (
                self.supplier_contact_id,
                self.price_list_id,
                self.status_id_request_sent,
                self.status_id_pending,
            )
        )


def discover(bp: BrightpearlClient) -> DiscoveryResult:
    return DiscoveryResult(
        supplier_contact_id=find_supplier_id(bp, SUPPLIER_NAME),
        price_list_id=find_price_list_id(bp, PRICE_LIST_NAME),
        status_id_request_sent=find_order_status_id(bp, STATUS_REQUEST_SENT_NAME),
        status_id_pending=find_order_status_id(bp, STATUS_PENDING_NAME),
    )


# ---------------------------------------------------------------------------
# Individual lookups
# ---------------------------------------------------------------------------


def find_supplier_id(bp: BrightpearlClient, exact_name: str) -> int | None:
    """Return the contact ID whose ``companyName`` exactly matches ``exact_name``.

    Tries the contact-search endpoint first (much faster than scanning every
    contact). If the search shape isn't what we expect, returns None and the
    caller can investigate manually.
    """
    response = bp.get(
        "/contact-service/contact-search",
        params={"companyName": exact_name},
    )
    return _id_for_exact_match(response, name_field="companyName", target=exact_name)


def find_price_list_id(bp: BrightpearlClient, exact_name: str) -> int | None:
    response = bp.get("/product-price-service/price-list")
    return _id_for_exact_match(
        response,
        name_field="name",
        target=exact_name,
        id_field="id",
    )


def find_order_status_id(bp: BrightpearlClient, exact_name: str) -> int | None:
    response = bp.get("/order-service/order-status")
    return _id_for_exact_match(
        response,
        name_field="name",
        target=exact_name,
        id_field="id",
    )


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _id_for_exact_match(
    response: Any,
    *,
    name_field: str,
    target: str,
    id_field: str = "contactId",
) -> int | None:
    for entry in _iter_entries(response):
        name = entry.get(name_field)
        if isinstance(name, str) and name.strip() == target:
            raw_id = entry.get(id_field) or entry.get("id")
            if isinstance(raw_id, int):
                return raw_id
            if isinstance(raw_id, str) and raw_id.strip().isdigit():
                return int(raw_id.strip())
    return None


def _iter_entries(response: Any) -> Iterable[Mapping[str, Any]]:
    """Yield dict entries from a Brightpearl response of varied shape."""
    if response is None:
        return
    if isinstance(response, list):
        for item in response:
            if isinstance(item, Mapping):
                yield item
        return
    if isinstance(response, Mapping):
        # Some endpoints wrap results in {"results": [...], "metaData": {...}}.
        results = response.get("results")
        if isinstance(results, list):
            meta_columns = _meta_column_names(response.get("metaData"))
            for row in results:
                if isinstance(row, Mapping):
                    yield row
                elif isinstance(row, list) and meta_columns:
                    yield dict(zip(meta_columns, row))
            return
        # Or as a dict keyed by ID with the entry payload as value.
        for key, value in response.items():
            if isinstance(value, Mapping):
                # Inject id-from-key if the entry doesn't already carry it.
                if "id" not in value and key.isdigit():
                    yield {**value, "id": int(key)}
                else:
                    yield value


def _meta_column_names(meta: Any) -> list[str]:
    if not isinstance(meta, Mapping):
        return []
    columns = meta.get("columns")
    if not isinstance(columns, list):
        return []
    names: list[str] = []
    for col in columns:
        if isinstance(col, Mapping):
            name = col.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


# ---------------------------------------------------------------------------
# Pretty-printing for the CLI
# ---------------------------------------------------------------------------


def format_env_snippet(result: DiscoveryResult) -> str:
    """Format the discovered IDs as a paste-ready .env.local block."""
    lines = [
        "# Discovered Brightpearl IDs for the GBR JIT integration.",
        "# Paste these into your .env.local (replacing any blank values):",
        "",
        f"GBR_JIT_SUPPLIER_CONTACT_ID={_render(result.supplier_contact_id)}",
        f"GBR_JIT_PRICE_LIST_ID={_render(result.price_list_id)}",
        f"GBR_JIT_STATUS_ID_REQUEST_SENT={_render(result.status_id_request_sent)}",
        f"GBR_JIT_STATUS_ID_PENDING={_render(result.status_id_pending)}",
    ]
    return "\n".join(lines) + "\n"


def _render(value: int | None) -> str:
    return str(value) if value is not None else "<NOT FOUND -- look up manually>"
