"""Entity Resolution module for Stage [2] (v6 spec §3)."""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from scout.config import EntityResolutionConfig


def identifier_keys(record: dict) -> list[str]:
    """Extract identifier keys in precedence order: PURL -> SWHID -> DOI -> ORCID -> domain (lowercase)."""
    keys: list[str] = []
    if not isinstance(record, dict):
        return keys
    rec_lower = {k.lower(): v for k, v in record.items()}
    for key in ("purl", "swhid", "doi", "orcid", "domain"):
        val = rec_lower.get(key)
        if val is not None and str(val).strip():
            if key == "domain":
                keys.append(str(val).strip().lower())
            else:
                keys.append(str(val).strip())
    return keys


def _build_context(record: dict) -> dict:
    """Build fixed 4-field context for the adjudicator, preventing field leakage."""
    return {
        "name": record.get("name", ""),
        "primary_url": record.get("primary_url", ""),
        "description": record.get("description", ""),
        "key_identifiers": identifier_keys(record),
    }


class ResolutionJournal(BaseModel):
    """Journal tracking entity resolution comparisons and decisions."""

    pairs_total: int = 0
    pairs_gray: int = 0
    merges_auto: int = 0
    merges_llm: int = 0
    fp_flags: list[str] = Field(default_factory=list)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def __setitem__(self, item: str, value: Any) -> None:
        setattr(self, item, value)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


class ResolutionResult:
    """Result object containing resolved clusters and resolution journal."""

    def __init__(self, clusters: list[list[dict]], journal: ResolutionJournal) -> None:
        self.clusters = clusters
        self.journal = journal

    def __iter__(self):
        return iter((self.clusters, self.journal))

    def __getitem__(self, idx: int):
        return (self.clusters, self.journal)[idx]

    def get(self, key: str, default: Any = None) -> Any:
        if key == "clusters":
            return self.clusters
        if key == "journal":
            return self.journal
        return default


def resolve(
    records: list[dict],
    config: EntityResolutionConfig | None,
    similarity: Callable[[dict, dict], float],
    adjudicator: Callable[[dict, dict], Any] | None = None,
) -> ResolutionResult:
    """Resolve records into entity clusters using identifier-first and similarity gating."""
    config = config or EntityResolutionConfig()
    n = len(records)
    journal = ResolutionJournal()
    journal.pairs_total = n * (n - 1) // 2

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        root_i = find(i)
        root_j = find(j)
        if root_i != root_j:
            parent[root_j] = root_i

    rec_ids = [set(identifier_keys(r)) for r in records]

    for i in range(n):
        for j in range(i + 1, n):
            rec_a = records[i]
            rec_b = records[j]

            # 1. Identifier-first: identical identifiers auto-merge regardless of cosine
            if rec_ids[i] and rec_ids[j] and rec_ids[i].intersection(rec_ids[j]):
                journal.merges_auto += 1
                union(i, j)
                continue

            # 2. Similarity comparison
            score = float(similarity(rec_a, rec_b))

            if score >= config.auto_threshold:
                journal.merges_auto += 1
                union(i, j)
            elif score >= config.gray_lo:
                journal.pairs_gray += 1
                if adjudicator is not None:
                    ctx_a = _build_context(rec_a)
                    ctx_b = _build_context(rec_b)
                    verdict = adjudicator(ctx_a, ctx_b)
                    is_same = False
                    if isinstance(verdict, dict):
                        is_same = bool(verdict.get("same"))
                    elif isinstance(verdict, bool):
                        is_same = verdict
                    if is_same:
                        journal.merges_llm += 1
                        union(i, j)

    # Group records by cluster root
    groups: dict[int, list[dict]] = {}
    for i, rec in enumerate(records):
        root = find(i)
        if root not in groups:
            groups[root] = []
        groups[root].append(rec)

    clusters = list(groups.values())
    return ResolutionResult(clusters=clusters, journal=journal)
