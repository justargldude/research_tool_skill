"""Topic Profiler module for Stage [0] (v6 spec §1)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

# Global profile cache keyed by (registry_version, normalized_topic).
# Bounded (insertion-order eviction) — hygiene bound, not a spec threshold.
_PROFILE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_PROFILE_CACHE_MAX = 512


def _cache_put(key: tuple[str, str], value: dict[str, Any]) -> None:
    if len(_PROFILE_CACHE) >= _PROFILE_CACHE_MAX:
        _PROFILE_CACHE.pop(next(iter(_PROFILE_CACHE)))
    _PROFILE_CACHE[key] = value


def load_registry(path: str | Path) -> dict:
    """Load domain registry from a YAML file."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_topic(raw: str) -> tuple[str, str]:
    """Normalize topic string: NFKC, lowercase, trimmed (stripping version prefix).

    Returns a tuple of (normalized_text, sha1_cache_key).
    """
    text = unicodedata.normalize("NFKC", str(raw)).strip().lower()
    text = re.sub(r"^v\d+(\.\d+)?[\s/_\-:]+", "", text)
    cache_key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return text, cache_key


def is_fresh(updated_at: datetime, now: datetime, ttl_days: float | int) -> bool:
    """Check if record age is within TTL days (exact boundary age == ttl is fresh)."""
    age_seconds = (now - updated_at).total_seconds()
    ttl_seconds = float(ttl_days) * 86400.0
    return age_seconds <= ttl_seconds


def fan_out(queries: list[str], sources: list[str]) -> list[tuple[str, str]]:
    """Cross-product M x N of queries and sources."""
    out: list[tuple[str, str]] = []
    for q in queries:
        for s in sources:
            out.append((q, s))
    return out


def rrf_fuse(ranked_lists: list[list[Any]], k: int = 60) -> list:
    """Reciprocal Rank Fusion (RRF) over multiple ranked lists.

    Pure math formula: sum(1.0 / (k + rank)) with rank starting at 1.
    Preserves original order when a single list is provided.
    """
    if not ranked_lists:
        return []
    if not any(ranked_lists):
        return []

    if len(ranked_lists) == 1:
        seen = set()
        res = []
        for item in ranked_lists[0]:
            if item not in seen:
                seen.add(item)
                res.append(item)
        return res

    scores: dict[Any, float] = {}
    for r_list in ranked_lists:
        for rank, item in enumerate(r_list, start=1):
            scores[item] = scores.get(item, 0.0) + (1.0 / (k + rank))

    sorted_items = sorted(scores.keys(), key=lambda x: (-scores[x], str(x)))
    return sorted_items


def pseudo_relevance(query: str, docs: list[str], k: int | None = None) -> str:
    """Pseudo-relevance round 2 expansion (0 token LLM).

    Keeps seed terms and expands only with terms co-occurring in top-k relevant docs.
    """
    query_norm = unicodedata.normalize("NFKC", str(query)).strip().lower()
    seed_terms = query_norm.split()
    seed_set = set(seed_terms)

    matching_docs = []
    for doc in docs:
        doc_clean = unicodedata.normalize("NFKC", str(doc)).strip().lower()
        if any(term in doc_clean for term in seed_terms):
            matching_docs.append(doc_clean)

    if k is not None and k > 0:
        top_docs = matching_docs[:k]
    else:
        top_docs = matching_docs

    counts: dict[str, int] = {}
    for doc in top_docs:
        words = re.findall(r"\b[a-zA-Z0-9_\-]+\b", doc)
        doc_words_seen = set()
        for w in words:
            w_clean = w.strip("-_").lower()
            if w_clean and w_clean not in seed_set and len(w_clean) > 1 and w_clean not in doc_words_seen:
                doc_words_seen.add(w_clean)
                counts[w_clean] = counts.get(w_clean, 0) + 1

    candidate_terms = sorted(counts.keys(), key=lambda w: (-counts[w], w))
    expanded = list(seed_terms) + candidate_terms
    return " ".join(expanded)


expand_query = pseudo_relevance
pseudo_relevance_expand = pseudo_relevance
second_round_query = pseudo_relevance
round_two_query = pseudo_relevance


def profile_topic(topic: str, registry: dict, profiler: Callable[[str], dict]) -> dict:
    """Profile a topic against domain registry or fallback to injected profiler with caching."""
    version = str(registry.get("version", "1"))
    norm_topic, _ = normalize_topic(topic)
    cache_key = (version, norm_topic)

    if cache_key in _PROFILE_CACHE:
        return _PROFILE_CACHE[cache_key]

    entries = {k: v for k, v in registry.items() if isinstance(v, dict)}
    for domain_name, entry in entries.items():
        domain_norm, _ = normalize_topic(domain_name)
        synonyms = entry.get("synonyms") or []
        synonyms_norm = [normalize_topic(s)[0] for s in synonyms]
        if norm_topic == domain_norm or norm_topic in synonyms_norm:
            profile = {
                "domain": domain_name,
                "sources": list(entry.get("sources") or []),
                "seed_queries": list(entry.get("seed_queries") or []),
                "variants": list(synonyms),
                "cache_hit": True,
            }
            _cache_put(cache_key, profile)
            return profile

    res = profiler(topic)
    profile = dict(res)
    profile["cache_hit"] = False

    _cache_put(cache_key, dict(profile, cache_hit=True))
    return profile
