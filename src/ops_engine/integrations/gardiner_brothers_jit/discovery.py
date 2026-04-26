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
    available_price_lists: tuple[tuple[int, str], ...] = ()
    available_order_statuses: tuple[tuple[int, str], ...] = ()

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
    supplier_id = find_supplier_id(bp, SUPPLIER_NAME)
    price_list_id = find_price_list_id(bp, PRICE_LIST_NAME)
    status_request = find_order_status_id(bp, STATUS_REQUEST_SENT_NAME)
    status_pending = find_order_status_id(bp, STATUS_PENDING_NAME)

    # Only fetch the full candidate lists when needed -- saves an API call
    # when everything was found by exact match.
    available_price_lists: tuple[tuple[int, str], ...] = ()
    available_order_statuses: tuple[tuple[int, str], ...] = ()
    if price_list_id is None:
        available_price_lists = tuple(list_price_lists(bp))
    if status_request is None or status_pending is None:
        available_order_statuses = tuple(list_order_statuses(bp))

    return DiscoveryResult(
        supplier_contact_id=supplier_id,
        price_list_id=price_list_id,
        status_id_request_sent=status_request,
        status_id_pending=status_pending,
        available_price_lists=available_price_lists,
        available_order_statuses=available_order_statuses,
    )


def list_price_lists(bp: BrightpearlClient) -> list[tuple[int, str]]:
    return _all_id_name_entries(bp.get("/product-service/price-list"))


def list_order_statuses(bp: BrightpearlClient) -> list[tuple[int, str]]:
    return _all_id_name_entries(bp.get("/order-service/order-status"))


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
    response = bp.get("/product-service/price-list")
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
        name = _extract_name(entry.get(name_field))
        if name is not None and name.strip() == target:
            return _extract_id(entry, primary=id_field)
    return None


# Brightpearl uses different ID field names per service:
#  - contact-search       -> contactId
#  - product-service       -> id
#  - product-service/price-list -> id
#  - order-service/order-status -> statusId
# The helper tries the caller's preferred field first, then any of these.
_ID_FALLBACK_FIELDS = ("id", "statusId", "contactId", "priceListId")


def _extract_name(value: Any) -> str | None:
    """Brightpearl sometimes returns names as plain strings (order-status,
    contact-search) and sometimes as ``{"text": ..., "format": "PLAINTEXT"}``
    (price-list). Return the displayable string regardless."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return None


def _extract_id(entry: Mapping[str, Any], *, primary: str | None = None) -> int | None:
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
    for field in _ID_FALLBACK_FIELDS:
        if field not in candidates:
            candidates.append(field)
    for field in candidates:
        raw = entry.get(field)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
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


def _all_id_name_entries(response: Any) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for entry in _iter_entries(response):
        name = _extract_name(entry.get("name"))
        entry_id = _extract_id(entry, primary=None)
        if name is not None and entry_id is not None:
            entries.append((entry_id, name))
    return entries


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

    if result.price_list_id is None and result.available_price_lists:
        lines.append("")
        lines.append("Available price lists in your Brightpearl:")
        for id_, name in sorted(result.available_price_lists):
            lines.append(f"  {id_:>6}  {name}")

    if (
        result.status_id_request_sent is None
        or result.status_id_pending is None
    ) and result.available_order_statuses:
        lines.append("")
        lines.append("Available order statuses in your Brightpearl:")
        for id_, name in sorted(result.available_order_statuses):
            lines.append(f"  {id_:>6}  {name}")

    return "\n".join(lines) + "\n"


def _render(value: int | None) -> str:
    return str(value) if value is not None else "<NOT FOUND -- look up manually>"
