"""Discovery core utilities: TokenBucket, canonicalize_url, and telemetry counters."""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Callable

from pydantic import BaseModel, Field


class DiscoveryCounters(BaseModel):
    """Telemetry counters for Stage [1] Discovery (v6 §2)."""

    rest_calls: int = 0
    search_calls: int = 0
    issues_truncated_count: int = 0
    comments_truncated_count: int = 0
    filtered_count: int = 0
    hits_304: int = 0
    hits_403_429: int = 0
    # P0-3a: raw hits captured before dedup and truncate (artifact raw_hits.json)
    raw_hits: list[dict] = Field(default_factory=list)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def __setitem__(self, item: str, value: Any) -> None:
        setattr(self, item, value)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


class TokenBucket:
    """Leaky/token-bucket rate limiter with continuous refill and injectable clock."""

    def __init__(
        self,
        *args: Any,
        rate: float | int | None = None,
        rate_per_minute: float | int | None = None,
        capacity: float | int | None = None,
        calls_per_minute: float | int | None = None,
        clock: Callable[[], float] | None = None,
        **kwargs: Any,
    ) -> None:
        r = None
        if len(args) >= 1:
            r = args[0]
        if len(args) >= 2 and clock is None:
            clock = args[1]

        final_rate = (
            rate
            if rate is not None
            else rate_per_minute
            if rate_per_minute is not None
            else capacity
            if capacity is not None
            else calls_per_minute
            if calls_per_minute is not None
            else r
            if r is not None
            else 30.0
        )
        self.capacity = float(final_rate)
        self.rate_per_second = float(final_rate) / 60.0
        self.tokens = float(final_rate)
        self.clock = clock if clock is not None else time.time
        self.last_update = float(self.clock())

    def take(self, tokens: float = 1.0) -> bool:
        now = float(self.clock())
        elapsed = max(0.0, now - self.last_update)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
        self.last_update = now
        if self.tokens >= tokens - 1e-9:
            self.tokens = max(0.0, self.tokens - tokens)
            return True
        return False

    acquire = take
    consume = take
    allow = take


def canonicalize_url(url: str) -> str:
    """Canonicalize URL: lowercase scheme & host, strip utm_*, strip trailing slash, sort params."""
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    path = parsed.path.rstrip("/")
    if not path and not netloc:
        path = parsed.path

    raw_params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered_params = [
        (k, v) for k, v in raw_params if not k.lower().startswith("utm_")
    ]
    filtered_params.sort(key=lambda kv: (kv[0], kv[1]))
    query = urllib.parse.urlencode(filtered_params)

    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def get_response_status(resp: Any) -> int:
    if hasattr(resp, "status"):
        return int(resp.status)
    if hasattr(resp, "status_code"):  # requests/httpx-style wrappers
        return int(resp.status_code)
    if isinstance(resp, dict):
        return int(resp.get("status", 200))
    try:
        return int(resp["status"])
    except Exception:
        return 200


def get_response_headers(resp: Any) -> dict:
    if hasattr(resp, "headers"):
        return resp.headers or {}
    if isinstance(resp, dict):
        return resp.get("headers", {})
    try:
        return resp["headers"] or {}
    except Exception:
        return {}


def get_response_json(resp: Any) -> Any:
    if isinstance(resp, list):
        return resp  # transport already decoded a JSON array body
    if hasattr(resp, "json"):
        if callable(resp.json):
            return resp.json()
        return resp.json
    if isinstance(resp, dict):
        if "json" in resp:
            val = resp["json"]
            return val() if callable(val) else val
        if "body" in resp:
            return resp["body"]
        return resp
    try:
        val = resp["json"]
        return val() if callable(val) else val
    except Exception:
        return {}


def request_with_resilience(
    transport: Callable,
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    counters: DiscoveryCounters | None = None,
    is_search: bool = True,
    max_retries: int = 3,
) -> tuple[Any, int]:
    params = params or {}
    headers = headers or {}

    resp = None
    status = 200
    for attempt in range(max_retries):
        if counters is not None:
            if is_search:
                counters.search_calls += 1
            else:
                counters.rest_calls += 1
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if headers:
            kwargs["headers"] = headers

        resp = transport(url, **kwargs)
        status = get_response_status(resp)

        if status == 304:
            if counters is not None:
                counters.hits_304 += 1
            return resp, 304

        if status in (403, 429):
            if counters is not None:
                counters.hits_403_429 += 1
            resp_headers = get_response_headers(resp)
            delay = 0.0
            for k, v in resp_headers.items():
                if k.lower() == "retry-after":
                    try:
                        delay = float(v)
                    except Exception:
                        pass
                elif k.lower() == "x-ratelimit-reset":
                    try:
                        reset_time = float(v)
                        now = time.time()
                        if reset_time > now:
                            delay = max(0.0, reset_time - now)
                    except Exception:
                        pass

            if delay > 0:
                time.sleep(delay)
            continue

        return resp, status

    return resp, status
