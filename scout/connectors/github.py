"""GitHub connector for Stage [1] Discovery."""

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

    headers = {"Accept": "application/vnd.github.v3+json"}
    header_keys = {"etag", "If-None-Match", "if_none_match"}
    for k in header_keys:
        if k in qualifiers:
            headers["If-None-Match"] = str(qualifiers[k])

    if "repo" in qualifiers:
        url = f"https://api.github.com/repos/{qualifiers['repo']}/issues"
        is_search = False
        params = dict(qualifiers)
        params.pop("repo", None)
        for k in header_keys:
            params.pop(k, None)
    else:
        url = "https://api.github.com/search/issues"
        is_search = True
        q_parts = [query] if query else []
        for k, v in qualifiers.items():
            if k in header_keys:
                continue
            if isinstance(v, bool):
                q_parts.append(f"{k}:{str(v).lower()}")
            else:
                q_parts.append(f"{k}:{v}")
        params = {"q": " ".join(q_parts).strip()}

    resp, status = request_with_resilience(
        transport, url, params=params, headers=headers, counters=counters, is_search=is_search
    )

    if status == 304:
        return [], counters

    data = get_response_json(resp)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items", [])
    else:
        items = []

    # 1. Dedup by (source, canonical_url, identifier)
    seen: set[tuple[str, str, str]] = set()
    unique_items: list[dict] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        identifier = str(item.get("id", ""))
        url_raw = item.get("html_url") or item.get("url") or ""
        canon_url = canonicalize_url(url_raw)
        dedup_key = ("github", canon_url, identifier)
        if dedup_key in seen:
            counters.filtered_count += 1
            continue
        seen.add(dedup_key)
        unique_items.append(item)

    # 2. Cap issues to max_issues
    if len(unique_items) > config.max_issues:
        counters.issues_truncated_count += len(unique_items) - config.max_issues
        records_raw = unique_items[: config.max_issues]
    else:
        records_raw = unique_items

    # 3. Process records: truncate body, fetch & truncate comments
    records: list[dict] = []
    for item in records_raw:
        record = dict(item)
        body = record.get("body") or ""
        record["body"] = body[: config.max_chars]

        comments_val = record.get("comments")
        comments_url = record.get("comments_url")
        should_fetch = False
        if comments_url:
            if comments_val is None:
                should_fetch = True
            elif isinstance(comments_val, int) and comments_val > 0:
                should_fetch = True
            elif isinstance(comments_val, list) and len(comments_val) > 0:
                should_fetch = True

        comments: list[dict] = []
        if should_fetch and comments_url:
            c_resp, c_status = request_with_resilience(
                transport, comments_url, headers={"Accept": headers["Accept"]},
                counters=counters, is_search=False
            )
            c_data = get_response_json(c_resp)
            if isinstance(c_data, list):
                comments = [dict(c) for c in c_data if isinstance(c, dict)]
            elif isinstance(comments_val, list):
                comments = [dict(c) for c in comments_val if isinstance(c, dict)]
        elif isinstance(comments_val, list):
            comments = [dict(c) for c in comments_val if isinstance(c, dict)]

        if len(comments) > config.max_comments:
            counters.comments_truncated_count += len(comments) - config.max_comments
            comments = comments[: config.max_comments]

        for c in comments:
            c["body"] = (c.get("body") or "")[: config.max_chars]

        record["comments"] = comments
        records.append(record)

    return records, counters
