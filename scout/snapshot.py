"""Stage [8] Snapshot/diff + re-crawl 3 tầng + wake-on-spike (v6 spec §8).

Zero network (transport injected), clock injected via `now`, deterministic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from scout.config import SnapshotConfig


def _as_naive_utc(dt: datetime) -> datetime:
    """Normalize API timestamps (often '...Z' -> tz-aware) to naive UTC so
    arithmetic with naive `now` never raises."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def normalize_for_hash(text: str, patterns: list[str]) -> str:
    """Xóa MỌI lần xuất hiện khớp các regex pattern khỏi nội dung trước khi hash."""
    result = text
    for pat in patterns:
        result = re.sub(pat, "", result)
    return result


def content_hash(s: str) -> str:
    """Sha256 hexdigest của s (utf-8)."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def entity_fingerprint(records: list[dict[str, Any]]) -> dict[str, str]:
    """Tạo fingerprint: dict entity_id -> content_hash(content)."""
    return {
        r["entity_id"]: content_hash(r.get("content") or "")
        for r in records
        if "entity_id" in r
    }


def changed(
    previous: dict[str, str], current: dict[str, str]
) -> dict[str, bool]:
    """Phát hiện thay đổi: True khi content đổi HOẶC entity mới; entity biến mất -> BỎ khỏi kết quả."""
    res: dict[str, bool] = {}
    for eid, cur_hash in current.items():
        if eid not in previous:
            res[eid] = True
        else:
            res[eid] = cur_hash != previous[eid]
    return res


def conditional_request_helpers(headers: dict[str, str]) -> dict[str, Any]:
    """Helper cho conditional request: if_none_match (ETag) và if_modified_since (Last-Modified)."""
    etag = None
    last_mod = None
    for k, v in headers.items():
        lk = k.lower()
        if lk == "etag":
            etag = v
        elif lk == "last-modified":
            last_mod = v
    return {
        "if_none_match": etag,
        "if_modified_since": last_mod,
    }


def is_not_modified(response_or_status: Any) -> bool:
    """True CHỈ với HTTP status 304 Not Modified."""
    if hasattr(response_or_status, "status_code"):
        code = response_or_status.status_code
    elif hasattr(response_or_status, "status"):
        code = response_or_status.status
    else:
        code = response_or_status
    try:
        return int(code) == 304
    except (ValueError, TypeError):
        return False


def recrawl_priority(churn_history: list[datetime], now: datetime) -> float:
    """Điểm ưu tiên re-crawl theo churn gần đây (churn gần+nhiều > xa+ít > rỗng)."""
    if not churn_history:
        return 0.0
    score = 0.0
    for dt in churn_history:
        days = max(0.0, (now - dt).total_seconds() / 86400.0)
        score += 2.0 ** (-days / 30.0)
    return score


def recrawl_plan(
    entity: dict[str, Any],
    now: datetime,
    cfg: SnapshotConfig | None = None,
) -> dict[str, Any]:
    """Phân loại re-crawl 3 tầng: active, stable, dead/frozen (spec §8)."""
    if cfg is None:
        cfg = SnapshotConfig()

    if entity.get("dead"):
        return {
            "tier": "dead",
            "interval_days": cfg.dead_interval_days,
            "frozen": True,
        }

    date_keys = (
        "last_release",
        "released_at",
        "last_commit",
        "last_activity",
        "last_change",
    )
    latest_dt: datetime | None = None
    for k in date_keys:
        val = entity.get(k)
        if isinstance(val, datetime):
            if latest_dt is None or val > latest_dt:
                latest_dt = val
        elif isinstance(val, str):
            try:
                parsed = _as_naive_utc(datetime.fromisoformat(val))
                if latest_dt is None or parsed > latest_dt:
                    latest_dt = parsed
            except ValueError:
                pass

    if latest_dt is not None:
        days_ago = (_as_naive_utc(now) - latest_dt).total_seconds() / 86400.0
        # Release / activity trong tháng (<= 30 ngày) -> active
        if days_ago <= 30.0:
            return {
                "tier": "active",
                "interval_days": cfg.active_interval_days,
            }

    return {
        "tier": "stable",
        "interval_days": cfg.stable_interval_days,
    }


recrawl_tier = recrawl_plan
crawl_tier = recrawl_plan
next_recrawl = recrawl_plan


def recrawl_interval_days(
    tier_or_entity: Any,
    now: datetime | None = None,
    cfg: SnapshotConfig | None = None,
) -> float | None:
    """Trả về interval days tương ứng theo tier hoặc entity."""
    if cfg is None:
        cfg = SnapshotConfig()

    if isinstance(tier_or_entity, dict):
        if now is None:
            now = datetime.now()
        plan = recrawl_plan(tier_or_entity, now, cfg)
        return plan.get("interval_days")

    t = str(tier_or_entity).lower()
    if "frozen" in t or "dead" in t:
        return cfg.dead_interval_days
    if "stable" in t:
        return float(cfg.stable_interval_days)
    if "active" in t:
        return float(cfg.active_interval_days)
    return None


tier_interval_days = recrawl_interval_days
interval_days = recrawl_interval_days
crawl_interval = recrawl_interval_days


def wake_on_spike(
    topic_or_frozen: Any,
    frozen_or_topic: Any,
    transport: Any,
) -> dict[str, bool]:
    """Wake-on-spike: đúng 1 call topic-level; CHỈ entity được nhắc tên trong hit title mới woken."""
    if isinstance(topic_or_frozen, str):
        topic = topic_or_frozen
        frozen_entities = frozen_or_topic
    else:
        frozen_entities = topic_or_frozen
        topic = frozen_or_topic

    resp = transport(topic)
    body = resp.json() if hasattr(resp, "json") else resp
    hits = body.get("hits", []) if isinstance(body, dict) else []
    titles = [str(h.get("title", "")) for h in hits if isinstance(h, dict)]
    combined_titles = " ".join(titles).lower()

    woken_map: dict[str, bool] = {}
    for ent in frozen_entities:
        eid = ent.get("entity_id", "")
        name = ent.get("name", "")
        mentioned = False

        if name and name.lower() in combined_titles:
            mentioned = True
        elif eid and eid.lower() in combined_titles:
            mentioned = True

        if eid:
            woken_map[eid] = mentioned
        if name:
            woken_map[name] = mentioned

    return woken_map


wake_frozen_entities = wake_on_spike
spike_wake = wake_on_spike
detect_spike = wake_on_spike
