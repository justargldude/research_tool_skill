"""Stage [3] Evidence Normalizer (v6 spec §4).

Zero token, zero network, deterministic evidence normalizer module.
Handles percentile calculation, winsorizing, decay weight, dead marking,
evidence score computation, nullable OSSF signal surfacing, StarScout
two-layer filtering, docs-only commit anti-farming, and 1-hop authority weighting.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Sequence

from scout.config import BandConfig


def ecdf_percentile(values: Sequence[float], x: float) -> float:
    """Tỷ lệ phần tử <= x trong values (biên đếm cả bằng, duplicate từng cái đếm); batch rỗng -> 0.0."""
    if not values:
        return 0.0
    return sum(1 for v in values if v <= x) / len(values)


def _percentile_val(sorted_vals: list[float], pct: float) -> float:
    """Nội suy phân vị chuẩn theo vị trí."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    k = (pct / 100.0) * (n - 1)
    i = int(math.floor(k))
    j = int(math.ceil(k))
    w = k - i
    return float(sorted_vals[i] * (1.0 - w) + sorted_vals[j] * w)


def winsorize(
    values: Sequence[float], lower_pct: float = 1.0, upper_pct: float = 99.0
) -> list[float]:
    """Clamp 2 đầu THEO VỊ TRÍ (không đổi thứ tự, không mutate input, giá trị trong thân giữ nguyên)."""
    if not values:
        return []
    sorted_vals = sorted(values)
    low_val = _percentile_val(sorted_vals, lower_pct)
    high_val = _percentile_val(sorted_vals, upper_pct)
    out: list[float] = []
    for v in values:
        if v < low_val:
            out.append(low_val)
        elif v > high_val:
            out.append(high_val)
        else:
            out.append(v)
    return out


def decay_weight(dt_months: float, halflife: float) -> float:
    """Hệ số suy giảm theo chu kỳ bán rã: w = 2^(-dt/h)."""
    if halflife <= 0:
        return 0.0
    return float(2.0 ** (-float(dt_months) / float(halflife)))


def mark_dead(signal: dict[str, Any], cfg: BandConfig) -> bool:
    """Xác định tín hiệu dead qua ngưỡng BandConfig.dead.

    Ngưỡng:
    - code: code_months_no_release
    - product: product_months_inactive
    - forum_issue/forum: forum_weeks_inactive (quy đổi ra tháng: 52 tuần = 12 tháng)
    - citation: không bao giờ dead
    """
    st = str(signal.get("signal_type", ""))
    dt_months = float(signal.get("dt_months", 0.0))

    if st == "citation":
        return False
    if st == "code":
        return dt_months > cfg.dead.code_months_no_release
    if st == "product":
        return dt_months > cfg.dead.product_months_inactive
    if st in ("forum_issue", "forum"):
        forum_months_threshold = cfg.dead.forum_weeks_inactive * 12.0 / 52.0
        return dt_months > forum_months_threshold
    return False


def normalize_evidence(
    signals: list[dict[str, Any]], cfg: BandConfig
) -> list[dict[str, Any]]:
    """Chuẩn hóa batch bằng chứng thô sang phân vị và điểm số theo BandConfig."""
    if not signals:
        return []

    # Thu thập toàn bộ các key OSSF/Scorecard xuất hiện trong batch
    ossf_pattern = re.compile(r"critic|score_?card|ossf|chaoss", re.I)
    ossf_keys: set[str] = set()
    for s in signals:
        for k in s:
            if ossf_pattern.search(str(k)):
                ossf_keys.add(str(k))

    primary_counts = [float(s.get("primary_count", 0.0)) for s in signals]
    w_usage = cfg.slot_weights.usage
    w_recency = cfg.slot_weights.recency
    w_network = cfg.slot_weights.network

    results: list[dict[str, Any]] = []
    for s in signals:
        st = str(s.get("signal_type", ""))
        dt_months = float(s.get("dt_months", 0.0))
        p_count = float(s.get("primary_count", 0.0))

        usage_pct = ecdf_percentile(primary_counts, p_count)

        # Lấy chu kỳ bán rã h theo họ signal_type từ BandConfig.halflife
        if st in ("forum_issue", "forum"):
            h = cfg.halflife.forum_months
        elif st == "citation":
            h = cfg.halflife.citation_months
        elif st == "code":
            h = cfg.halflife.code_months
        else:
            h = getattr(cfg.halflife, f"{st}_months", cfg.halflife.code_months)

        recency_w = decay_weight(dt_months, h)

        # Xử lý slot network (nếu signal không mang dữ liệu network -> term = 0.0)
        network_term = 0.0
        if s.get("network") is not None:
            network_term = float(s["network"])
        elif s.get("dependents") is not None:
            network_term = float(dependents_authority(s["dependents"]))
        elif s.get("authority") is not None:
            network_term = float(s["authority"])

        score = usage_pct * w_usage + recency_w * w_recency + network_term * w_network
        dead = mark_dead(s, cfg)

        row: dict[str, Any] = {
            "signal_type": st,
            "usage_pct": usage_pct,
            "recency_w": recency_w,
            "score": score,
            "dead": dead,
        }

        # Surfacing các tín hiệu OSSF nullable (nếu có key xuất hiện trong batch hoặc signal)
        for k in ossf_keys:
            row[k] = s.get(k, None)
        for k, v in s.items():
            if ossf_pattern.search(str(k)):
                row[k] = v

        results.append(row)

    return results


def filter_starscout(
    records: list[dict[str, Any]],
    static: Any = None,
    rerun: Any = None,
    *,
    static_list: Any = None,
    rerun_list: Any = None,
    starscout_static: Any = None,
    starscout_rerun: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bộ lọc StarScout 2 lớp: static point-in-time + rerun dump tháng mới."""
    s_input = static if static is not None else (static_list if static_list is not None else starscout_static)
    r_input = rerun if rerun is not None else (rerun_list if rerun_list is not None else starscout_rerun)

    s_set = set(s_input) if s_input is not None else set()
    r_set = set(r_input) if r_input is not None else set()
    flagged_set = s_set | r_set

    kept: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    for r in records:
        repo = r if isinstance(r, str) else r.get("repo", "")
        if repo in flagged_set:
            flagged.append(r)
        else:
            kept.append(r)

    return kept, flagged


starscout_filter = filter_starscout
apply_starscout_filter = filter_starscout


def is_docs_only(commit: dict[str, Any]) -> bool:
    """Kiểm tra commit có chỉ sửa docs/readme/typo hay không (chống commit-farming)."""
    msg = str(commit.get("message", "")).strip().lower()
    msg_docs = bool(
        re.match(r"^(docs?|readme|typo)[\s\:\(\/\-\_]", msg)
        or msg in ("typo", "docs", "readme", "doc")
        or msg.startswith(("docs:", "readme:", "typo"))
    )
    if not msg_docs:
        return False

    files = commit.get("files") or commit.get("paths") or []
    if not files:
        return True

    doc_exts = {".md", ".markdown", ".rst", ".txt", ".adoc"}
    for f in files:
        if isinstance(f, dict):  # GitHub commits API: [{"filename": ...}]
            f = f.get("filename") or f.get("path") or ""
        p = Path(str(f))
        ext = p.suffix.lower()
        name = p.name.lower()
        if ext in doc_exts or name in ("readme", "changelog", "license", "notice"):
            continue
        # Bất kỳ file nào ngoài doc -> coi là code path
        return False

    return True


docs_only = is_docs_only
is_docs_commit = is_docs_only


def cadence_commits(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Loại bỏ các commit docs-only khỏi cadence đếm commit thật."""
    return [c for c in commits if not is_docs_only(c)]


effective_commits = cadence_commits
count_real_commits = cadence_commits
commits_for_cadence = cadence_commits


def dependents_authority(
    dependents: list[dict[str, Any]], k: int = 20
) -> float:
    """Tính authority score = tổng criticality của top-K dependents (K=20 < 40)."""
    if not dependents:
        return 0.0
    sorted_deps = sorted(
        dependents,
        key=lambda d: float(d.get("criticality") or 0.0),
        reverse=True,
    )
    return float(sum(float(d.get("criticality") or 0.0) for d in sorted_deps[:k]))


authority_weighted_dependents = dependents_authority
authority_score = dependents_authority
weighted_dependents = dependents_authority
