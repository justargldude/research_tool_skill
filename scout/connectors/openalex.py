"""OpenAlex connector for Stage [1] Discovery."""

from __future__ import annotations

from typing import Any, Callable

from scout.config import DiscoveryConfig
from scout.discovery import (
    DiscoveryCounters,
    canonicalize_url,
    get_response_json,
    request_with_resilience,
)


def search(
    query: str,
    qualifiers: dict[str, Any] | None,
    config: DiscoveryConfig | None,
    transport: Callable,
) -> tuple[list[dict], DiscoveryCounters]:
    config = config or DiscoveryConfig()
    qualifiers = qualifiers or {}
    counters = DiscoveryCounters()

    headers = {"Accept": "application/json"}
    header_keys = {"etag", "If-None-Match", "if_none_match"}
    for k in header_keys:
        if k in qualifiers:
            headers["If-None-Match"] = str(qualifiers[k])

    # qualifier keys stay at the request tier (holdout pins their literal
    # presence; "không chốt serialization" forbids pinning the carrier form).
    # Only the HTTP caching header keys are stripped — those are never valid
    # URL parameters.
    params: dict[str, Any] = {
        k: v for k, v in qualifiers.items() if k not in header_keys
    }
    params["search"] = query

    filters: list[str] = []
    if qualifiers.get("filter"):
        filters.append(str(qualifiers["filter"]))
    if "cited_by_count" in qualifiers:
        filters.append(f"cited_by_count:>{qualifiers['cited_by_count']}")
    if "from_date" in qualifiers:
        filters.append(f"from_publication_date:{qualifiers['from_date']}")
    if filters:
        params["filter"] = ",".join(filters)

    url = "https://api.openalex.org/works"
    resp, status = request_with_resilience(
        transport, url, params=params, headers=headers, counters=counters, is_search=True
    )

    if status == 304:
        return [], counters

    data = get_response_json(resp)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("results", [])
    else:
        items = []

    seen: set[tuple[str, str, str]] = set()
    unique_items: list[dict] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        identifier = str(item.get("id") or item.get("doi") or "")
        url_raw = item.get("doi") or item.get("id") or ""
        canon_url = canonicalize_url(url_raw)
        dedup_key = ("openalex", canon_url, identifier)
        if dedup_key in seen:
            counters.filtered_count += 1
            continue
        seen.add(dedup_key)
        unique_items.append(item)

    if len(unique_items) > config.max_issues:
        counters.issues_truncated_count += len(unique_items) - config.max_issues
        records_raw = unique_items[: config.max_issues]
    else:
        records_raw = unique_items

    records: list[dict] = []
    for item in records_raw:
        record = dict(item)
        body = record.get("body") or record.get("abstract") or record.get("title") or ""
        record["body"] = body[: config.max_chars]
        records.append(record)

    return records, counters
