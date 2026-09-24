"""Machine-readable contracts for scout-pipeline (v5 spec).

Single source of truth: pydantic models below.
JSON Schema files in schemas/ are generated from these models
(pydantic model_json_schema) — do NOT hand-edit schemas/.
Examples in examples/ must parse with these models (tests enforce).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- band config

BandName = Literal["leader", "contender", "candidate", "unverified"]


class BandThreshold(BaseModel):
    name: BandName
    min_percentile: float = Field(ge=0, le=100)
    description: str = ""


class DeadThresholds(BaseModel):
    code_months_no_release: int = 24
    product_months_inactive: int = 12
    forum_weeks_inactive: int = 8


class Halflife(BaseModel):
    """Half-life h for w = 2^(-dt/h), per signal family (v5 §4)."""

    code_months: float = 6.0
    forum_months: float = 3.0
    citation_months: float = 24.0


class Winsorize(BaseModel):
    lower_pct: float = 1.0
    upper_pct: float = 99.0


class SlotWeights(BaseModel):
    usage: float = 0.5
    recency: float = 0.3
    network: float = 0.2


class BandConfig(BaseModel):
    """Band + hysteresis + N/dead + decay + slot weights (v5 §5)."""

    version: str = "1"
    bands: list[BandThreshold] = Field(min_length=4, max_length=4)
    hysteresis_points: float = 5.0
    dead: DeadThresholds = DeadThresholds()
    halflife: Halflife = Halflife()
    winsorize: Winsorize = Winsorize()
    slot_weights: SlotWeights = SlotWeights()


# --------------------------------------------------------------- coverage log

class StageCounters(BaseModel):
    stage: str  # e.g. "[0]", "[1]", "[2]", "[3]", "[4]", "[5]", "[6]", "[7]", "[8]"
    rest_calls: int = 0
    search_calls: int = 0  # GitHub /search/* quota (30/min) — NEVER merge with rest_calls
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    cache_hit: bool = False
    skipped_by_hash: bool = False
    issues_truncated_count: int = 0
    comments_truncated_count: int = 0
    filtered_count: int = 0
    hits_304: int = 0
    hits_403_429: int = 0
    cost_usd: float = 0.0


class CoverageLog(BaseModel):
    """Per-run cost/quality ledger (v5 §8). One file per run."""

    run_id: str
    topic_raw: str
    topic_normalized: str
    topic_key_sha1: str
    started_at: datetime
    price_date: str  # YYYY-MM-DD — prices pinned at run day, never reuse old prices
    stages: list[StageCounters] = Field(default_factory=list)
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0


# ------------------------------------------------------------- golden sample

class GoldenSample(BaseModel):
    """One labeled snippet. Ground truth MUST come from branch-A raw
    random/stratified sample, NEVER from branch-B candidates (v5 §6 C1)."""

    topic: str
    entity_id: str
    source: str  # e.g. "github-issue", "hn-comment", "readme"
    snippet_id: str
    snippet_text: str
    is_third_party_evidence: bool
    sample_source: Literal["branch_A_raw_random"] = "branch_A_raw_random"
    branch_B_kept: bool | None = None  # filled when B runs over the same sample
    annotator: str = ""
    annotated_at: datetime | None = None


# ----------------------------------------------------------- snapshot config

class SnapshotConfig(BaseModel):
    """3-tier recrawl intervals + snapshot configuration (v6 §8)."""

    active_interval_days: int = 7
    stable_interval_days: int = 60
    dead_frozen: bool = True
    dead_interval_days: int | None = None


# --------------------------------------------------------- capability config

class CapabilityConfig(BaseModel):
    """AST heading-slicer + capability audit configuration (v6 §7)."""

    keep: list[str] = Field(
        default_factory=lambda: [
            "features",
            "capabilities",
            "architecture",
            "limitations",
            "comparison",
            "benchmarks",
        ]
    )
    drop: list[str] = Field(
        default_factory=lambda: [
            "installation",
            "quickstart",
            "license",
            "contributing",
            "sponsors",
            "badges",
        ]
    )
    fallback_words: int = 500


# ---------------------------------------------------------- discovery config

class DiscoveryConfig(BaseModel):
    """Caps + throttle for Stage [1] Discovery (v6 §2)."""

    max_issues: int = 30
    max_comments: int = 10
    max_chars: int = 1500
    throttle_search_rate: int = 30  # Search quota <= 30/minute
    max_query_chars: int = 256
    max_query_clauses: int = 5
    max_results_per_search: int = 1000
    max_repos_per_query: int = 4000


# ------------------------------------------------ entity resolution config

class EntityResolutionConfig(BaseModel):
    """Entity resolution thresholds for Stage [2] (v6 §3)."""

    auto_threshold: float = 0.92
    gray_lo: float = 0.70


# --------------------------------------------------------- profiler config

class ProfilerConfig(BaseModel):
    """Topic profiler configuration for Stage [0] (v6 §1)."""

    ttl_days: int = 90
    rrf_k: int = 60
    seed_queries_fan_out: int = 5


# ----------------------------------------------------- text mining config

class TextMiningConfig(BaseModel):
    """Text-mining configuration for Stage [5] (v6 §6)."""

    top_k: int = 30
    min_cosine: float = 0.35
    facet_search: bool = True
    facets_enabled: bool = True
    enable_facet_search: bool = True
    facet_search_on: bool = True

    def __init__(self, **data: Any):
        flag_val = None
        for k in ("facet_search", "facets_enabled", "enable_facet_search", "facet_search_on"):
            if k in data:
                flag_val = data[k]
                break
        if flag_val is not None:
            data["facet_search"] = flag_val
            data["facets_enabled"] = flag_val
            data["enable_facet_search"] = flag_val
            data["facet_search_on"] = flag_val
        super().__init__(**data)


# ------------------------------------------------------------- run artifacts

CapabilityLabel = Literal[
    "yes", "no", "partial", "unclear", "supported", "unsupported", "unknown"
]


class AxisAssessmentRecord(BaseModel):
    """One capability-audit axis as persisted in entities.json (mirrors
    scout.capability.AxisAssessment; defined here to avoid a circular import)."""

    axis: str
    classification: CapabilityLabel


class CapabilityAuditRecord(BaseModel):
    axes: list[AxisAssessmentRecord] = Field(default_factory=list)


class EntityArtifact(BaseModel):
    """One row of entities.json — a resolved entity with its [4]-[6] outputs.

    Contract for all runners (run_pipeline, run_research, skill runner);
    schemas/entities.schema.json is generated from this model.
    """

    entity_id: str
    name: str
    band: BandName | None = None
    score_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    n_records: int | None = None
    competitors: dict[str, int] = Field(default_factory=dict)
    capability: CapabilityAuditRecord | None = None
    facet_titles: list[str] = Field(default_factory=list)


class FindingArtifact(BaseModel):
    """One row of findings.json — third-party evidence with a verbatim quote.

    Runners MUST drop findings whose quote is not verbatim in the source
    snippets (the skill runner already enforces this at extraction time).
    """

    entity: str
    tool: str = ""
    claim: str = ""
    quote: str = ""
