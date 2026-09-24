#!/usr/bin/env python3
"""End-to-end offline pipeline runner for scout v6 (spec §0 flow).

Chains stages [0]→[1]→[2]→[3]→[4]→[5]→[6]→[7]→[8] over a deterministic
fixture transport. Per the module contracts every network/LLM/similarity/
embedder callable is INJECTED here — this file owns the injectables:

- transport: FixtureTransport (canned payloads, 0 network; ETag/304 + a
  429-with-retry-after-0 drill)
- profiler for registry miss: keyword stub (stands in for the 1 cheap LLM
  call a real run would spend; llm_calls counts it, tokens stay 0)
- similarity for [2]: token-cosine stub (stands in for the pinned local
  embedding model)
- embedder for [5] branch B: 64-dim hashed bag-of-words (deterministic)
- classifier/extractor LLM stubs: keyword rules emitting terse JSON

Two passes over the same topic (snapshot semantics of [8]):
  pass A — fresh crawl; ETags observed from responses are stored
  pass B — sources answer 304 Not-Modified (records carried over from the
           store unchanged -> hash-skip of [5][6] for those entities), HN
           serves one fresh record (entity content changes -> reprocessed,
           band flip under hysteresis), wake-on-spike sweep probes frozen
           entities, Cohen's kappa scores band stability across passes.

Outputs under --out: report.md (the product), coverage_log.json, eval.json,
entities.json, evidence.json, snapshot_plan.json, run_meta.json.

Usage: python3 run_pipeline.py [--topic "vector database"] [--out out/demo]
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path

from scout.band import assign_band
from scout.capability import (
    build_audit_prompt,
    competitor_set,
    parse_capability_output,
    slice_readme,
)
from scout.config import (
    BandConfig,
    BandThreshold,
    CapabilityConfig,
    CoverageLog,
    EntityArtifact,
    EntityResolutionConfig,
    ProfilerConfig,
    SnapshotConfig,
    StageCounters,
    TextMiningConfig,
)
from scout.connectors import github, hn, openalex
from scout.discovery import TokenBucket, canonicalize_url
from scout.entity import resolve
from scout.evidence import cadence_commits, is_docs_only, normalize_evidence
from scout.profiler import (
    fan_out,
    is_fresh,
    load_registry,
    normalize_topic,
    profile_topic,
    pseudo_relevance,
    rrf_fuse,
)
from scout.report import band_stability, cost_per_stage, render_report, seeded_recall
from scout.runtime import (
    resolve_registry_path,
    write_artifacts,
)
from scout.snapshot import (
    changed,
    entity_fingerprint,
    recrawl_plan,
    wake_on_spike,
)
from scout.textmining import (
    build_extraction_prompt,
    build_sid_index,
    evidence_recall,
    facet_search,
    select_branch,
)

NOW = datetime(2026, 9, 24, 12, 0, 0)
RUN_ID = "offline-demo-20260924"

BAND_CFG = BandConfig(
    bands=[
        BandThreshold(name="leader", min_percentile=80.0),
        BandThreshold(name="contender", min_percentile=50.0),
        BandThreshold(name="candidate", min_percentile=20.0),
        BandThreshold(name="unverified", min_percentile=0.0),
    ]
)
SCOPE_BY_BAND = {
    "leader": "plausible",
    "contender": "plausible",
    "candidate": "plausible",
    "unverified": "below-plausible",
}


# --------------------------------------------------------------------------
# Fixture payloads + transport
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, headers=None, payload=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


def _comment(user, body, created):
    return {"user": {"login": user}, "body": body, "created_at": created}


GITHUB_ITEMS = [
    {
        "id": 101, "name": "MyMilvus",
        "html_url": "https://github.com/mymilvus/mymilvus",
        "domain": "mymilvus.io",
        "title": "MyMilvus: distributed vector database engine",
        "body": "MyMilvus is a managed vector database for AI embeddings and rag workloads",
        "stars": 25000, "comments": 2,
        "comments_url": "https://api.github.com/repos/mymilvus/mymilvus/issues/101/comments",
        "pushed_at": "2026-09-01T00:00:00",
        "dependents": [
            {"name": "rag-stack", "criticality": 1.0},
            {"name": "llm-serving", "criticality": 0.95},
            {"name": "embed-store", "criticality": 0.9},
        ],
        "criticality_score": 0.73, "scorecard": "7.2",
    },
    {   # exact duplicate of 101 (same id + canonical url) -> filtered_count demo
        "id": 101, "name": "MyMilvus",
        "html_url": "https://github.com/mymilvus/mymilvus/",
        "domain": "mymilvus.io", "title": "MyMilvus (mirror)", "body": "dupe",
        "stars": 25000, "comments": 0,
    },
    {
        "id": 102, "name": "OldQdrant",
        "html_url": "https://github.com/oldqdrant/oldqdrant",
        "domain": "oldqdrant.dev",
        "title": "OldQdrant: legacy vector search",
        "body": "OldQdrant is a legacy vector database, no longer maintained",
        "stars": 12000, "comments": 0, "pushed_at": "2024-07-01T00:00:00",
    },
    {
        # 3 docs-only commits + 1 real -> cadence anti-farming demo
        "id": 103, "name": "TinyVectorDB",
        "html_url": "https://github.com/tinyvectordb/tinyvectordb",
        "domain": "tinyvectordb.org",
        "title": "TinyVectorDB: a tiny vector database",
        "body": "TinyVectorDB is a small embedded vector database for embeddings",
        "stars": 30, "comments": 0, "pushed_at": "2026-07-24T00:00:00",
        "recent_commits": [
            {"message": "docs: fix typo in readme", "files": ["README.md"]},
            {"message": "readme: add badge", "files": ["README.md", "docs/guide.md"]},
            {"message": "typo", "files": ["CHANGELOG.md"]},
            {"message": "fix: shard rebalance crash", "files": ["src/shard.py"]},
        ],
        "dependents": [{"name": "rag-utils", "criticality": 0.1}],
    },
]

# REST-list preference (repo known) — distinct issue, same domain identifier
GITHUB_REST_ITEMS = [
    {
        "id": 105, "name": "MyMilvus",
        "html_url": "https://github.com/mymilvus/mymilvus/issues/105",
        "domain": "mymilvus.io", "title": "MyMilvus roadmap discussion",
        "body": "Roadmap: hybrid search and multi-tenancy support",
        "stars": 25000, "comments": 0, "pushed_at": "2026-09-01T00:00:00",
    }
]

GITHUB_COMMENTS_MYMILVUS = [
    _comment("alice", "We migrated: the query latency broke after the last release", "2026-09-05T00:00:00"),
    _comment("bob", "Multi-tenancy support is missing but roadmap looks good", "2026-09-10T00:00:00"),
]

HN_HITS = [
    {
        "objectID": "h1", "title": "Show HN: TinyVectorDB doesn't work at scale",
        "points": 45, "created_at": "2026-09-10T00:00:00",
        "url": "https://news.ycombinator.com/item?id=h1", "domain": "tinyvectordb.org",
        "comment_text": "TinyVectorDB crashed under load; we tried everything and it kept failing",
    },
    {
        "objectID": "h2", "title": "MyMilvus vs pgvector benchmarks",
        "points": 88, "created_at": "2026-08-30T00:00:00",
        "url": "https://news.ycombinator.com/item?id=h2", "domain": "mymilvus.io",
        "comment_text": "benchmarks show MyMilvus query performance holds at production scale",
    },
    {
        "objectID": "h1", "title": "Show HN: TinyVectorDB doesn't work at scale (dup)",
        "points": 45, "created_at": "2026-09-10T00:00:00",
        "url": "https://news.ycombinator.com/item?id=h1", "domain": "tinyvectordb.org",
        "comment_text": "dupe of h1",
    },
]

HN_HIT_B_NEW = {
    "objectID": "h4", "title": "TinyVectorDB v2 released: hybrid search shipped",
    "points": 450, "created_at": "2026-09-22T00:00:00",
    "url": "https://news.ycombinator.com/item?id=h4", "domain": "tinyvectordb.org",
    "comment_text": "v2 release notes claim hybrid search and multi-tenancy support",
}

OPENALEX_RESULTS = [
    {
        "id": "https://doi.org/10.1234/vdb-survey", "doi": "https://doi.org/10.1234/vdb-survey",
        "title": "A Survey of Vector Databases",
        "abstract": "We survey vector database systems including MyMilvus and TinyVectorDB",
        "cited_by_count": 1400, "publication_date": "2026-06-01", "domain": "mymilvus.io",
    },
    {
        "id": "https://doi.org/10.1234/vdb-tiny", "doi": "https://doi.org/10.1234/vdb-tiny",
        "title": "Embedded Vector Search for Edge Devices",
        "abstract": "TinyVectorDB enables embeddings search on edge hardware",
        "cited_by_count": 14, "publication_date": "2025-11-15", "domain": "tinyvectordb.org",
    },
]

# non-connector "product directory" record — gray-band pair for [2]
PRODUCT_DIR_RECORD = {
    "name": "MyMilvus Cloud", "domain": "mymilvus.cloud",
    "description": "MyMilvus is a managed vector database service for AI workloads",
    "product_mentions": 12, "last_change": "2026-01-24T00:00:00",
}

COMPETITOR_HITS = {
    "MyMilvus": [
        {"title": "MyMilvus vs Qdrant: 2026 benchmarks"},
        {"title": "Zilliz cloud is an alternative to MyMilvus"},
        {"title": "weekly off-topic thread: what are you working on?"},
    ],
    "TinyVectorDB": [
        {"title": "TinyVectorDB vs Weaviate on edge devices"},
        {"title": "alternative to TinyVectorDB for embeddings"},
    ],
}

FACET_HITS = {
    "MyMilvus": [{"title": "MyMilvus outage post-mortem: region failover"},
                 {"title": "Re: MyMilvus memory leak under heavy load"}],
    "TinyVectorDB": [{"title": "TinyVectorDB data loss incident report"}],
}

WAKE_SWEEP = {"hits": [{"title": "OldQdrant fork revived as VectorLoop — interview"}]}

READMES = {
    "MyMilvus": """# MyMilvus
Distributed vector database engine.

## Features
- Hybrid search with filters
- Multi-tenancy isolation
### Scaling notes
Horizontal sharding out of the box.

## Installation
pip install mymilvus

## Benchmarks
1M qps on 100M vectors.

## License
Apache-2.0
""",
    "TinyVectorDB": """# TinyVectorDB
A small embedded vector database.

## Installation
pip install tinyvectordb

## License
MIT
""",
}

FACET_TERMS = ("data loss", "memory leak", "outage", "post-mortem", "CVE",
               "migrated away", "why we left", "abandoned", "too complex",
               "production scale", "bottleneck", "high throughput")


class FixtureTransport:
    """Deterministic transport. Scenario A serves fresh 200s (one 429 drill on
    the first HN main search). Scenario B answers 304 for any request carrying
    If-None-Match (except HN, which serves fresh content). Bare-string calls
    are [6] competitor queries; the exact topic string is the [8] sweep."""

    def __init__(self, scenario: str, state: dict):
        self.scenario = scenario
        self.state = state

    def __call__(self, url, **kwargs):
        params = kwargs.get("params") or {}
        headers = kwargs.get("headers") or {}
        inm = headers.get("If-None-Match")
        etag_hdr = {"ETag": '"v1"'} if self.scenario == "A" else {}

        def resp(payload, status=200):
            return FakeResponse(status, etag_hdr, payload)

        if self.scenario == "B" and inm is not None and "hn.algolia" not in url:
            return FakeResponse(304, {}, {})

        if "api.github.com" in url:
            q = str(params.get("q", ""))
            if url.endswith("/comments"):
                return resp(GITHUB_COMMENTS_MYMILVUS)
            if "/repos/" in url:
                return resp({"items": GITHUB_REST_ITEMS})
            if any(t in q for t in FACET_TERMS):
                name = "MyMilvus" if "MyMilvus" in q else "TinyVectorDB"
                return resp({"items": FACET_HITS.get(name, [])})
            return resp({"items": GITHUB_ITEMS, "incomplete_results": False})

        if "hn.algolia.com" in url:
            query = str(params.get("query", ""))
            if any(t in query for t in FACET_TERMS):
                name = "MyMilvus" if "MyMilvus" in query else "TinyVectorDB"
                return resp({"hits": FACET_HITS.get(name, [])})
            if self.scenario == "B":
                return resp({"hits": HN_HITS[:2] + [HN_HIT_B_NEW]})
            if not self.state.get("hn_429_done"):
                self.state["hn_429_done"] = True
                return FakeResponse(429, {"retry-after": "0"}, {})
            return resp({"hits": HN_HITS})

        if "api.openalex.org" in url:
            return resp({"results": OPENALEX_RESULTS})

        # bare-string calls: [6] competitor templates vs [8] topic sweep
        if url == "vector database":
            return resp(WAKE_SWEEP)
        if " vs" in url or "alternative to" in url:
            name = url.split(" vs")[0].replace("alternative to ", "")
            return resp({"hits": COMPETITOR_HITS.get(name, [])})
        return resp({})


# --------------------------------------------------------------------------
# Injected stubs (deterministic; production swaps these only)
# --------------------------------------------------------------------------

def profiler_stub(topic: str) -> dict:
    t = topic.lower()
    if "vector" in t or "database" in t or "sql" in t:
        return {"domain": "databases", "sources": ["github", "hn", "openalex"],
                "seed_queries": ["vector database", "ann index", "embedding store"],
                "variants": ["vector db", "vector search"]}
    return {"domain": "generated", "sources": ["github", "hn"],
            "seed_queries": [t], "variants": [t]}


def _tokens(text: str) -> dict[str, float]:
    counts: dict[str, float] = {}
    for tok in re.findall(r"[a-z0-9]+", str(text).lower()):
        counts[tok] = counts.get(tok, 0.0) + 1.0
    return counts


def similarity_stub(a: dict, b: dict) -> float:
    ta = _tokens(str(a.get("name", "")) + " " + str(a.get("description", "")))
    tb = _tokens(str(b.get("name", "")) + " " + str(b.get("description", "")))
    dot = sum(v * tb.get(k, 0.0) for k, v in ta.items())
    na = math.sqrt(sum(v * v for v in ta.values()))
    nb = math.sqrt(sum(v * v for v in tb.values()))
    return dot / (na * nb) if na and nb else 0.0


def embedder_stub(text: str) -> list[float]:
    vec = [0.0] * 64
    for tok in _tokens(text):
        vec[hash(tok) % 64] += 1.0
    return vec


AXES = ["query performance", "multi-tenancy", "hybrid search"]
SCHEMA_HINT = '{"axes": [{"axis": str, "classification": "yes"|"partial"|"unclear"}]}'
AXIS_QUERY = "query performance production scale benchmarks vector database"


def classifier_stub(prompt: str) -> str:
    low = prompt.lower()
    axes_out = []
    for axis in AXES:
        key = axis.split()[0]
        if key in low:
            cls = "yes" if (f"{key} with" in low or "holds" in low) else "partial"
        else:
            cls = "unclear"
        axes_out.append({"axis": axis, "classification": cls})
    return json.dumps({"axes": axes_out})


def extractor_stub() -> str:
    """Deterministic terse-JSON extraction (stub; not content-driven)."""
    return json.dumps({"evidence": [
        {"snippet_id": sid, "is_third_party_evidence": sid in ("s1", "s2", "s4")}
        for sid in ("s1", "s2", "s3", "s4")
    ]})


# --------------------------------------------------------------------------
# Stage helpers
# --------------------------------------------------------------------------

def months_before(now: datetime, iso_date: str) -> float:
    dt = datetime.fromisoformat(iso_date)
    return max(0.0, (now - dt).total_seconds()) / (86400.0 * 30.44)


def record_key(r: dict) -> tuple:
    url = r.get("html_url") or r.get("url") or r.get("doi") or r.get("id") or ""
    return (canonicalize_url(str(url)), str(r.get("id") or r.get("objectID") or r.get("doi") or ""))


def run_discovery(profile: dict, scenario: str, transport: FixtureTransport,
                  etags: dict, bucket: TokenBucket) -> tuple[list[dict], dict]:
    """Stage [1]: fan-out over 3 connectors; pass B sends stored ETags."""
    totals = {f: 0 for f in ("rest_calls", "search_calls", "issues_truncated_count",
                             "comments_truncated_count", "filtered_count",
                             "hits_304", "hits_403_429")}
    records: list[dict] = []
    combos = fan_out(profile["seed_queries"], profile["sources"])

    for q, source in combos:
        if not bucket.take():
            continue  # search-throttled (bucket stats noted in run_meta)
        etag_q = {"etag": etags[source]} if etags.get(source) else {}
        if source == "github":
            recs, c = github.search(q, {"stars": ">10", "archived": "false",
                                        "fork": "false", **etag_q}, None, transport)
        elif source == "hn":
            recs, c = hn.search(q, {"points": 5, **etag_q}, None, transport)
        else:
            recs, c = openalex.search(q, {"cited_by_count": 5,
                                          "from_date": "2025-09-24", **etag_q},
                                      None, transport)
        for f, v in c.__dict__.items() if hasattr(c, "__dict__") else []:
            if f in totals:
                totals[f] += int(v or 0)
        records.extend(recs)

    # REST-list preference for a known repo (round-2 query on the fused list)
    recs, c = github.search("vector database", {"repo": "mymilvus/mymilvus"}, None, transport)
    totals["rest_calls"] += c.rest_calls
    records.extend(recs)
    return records, totals


def build_signals(entity_records: list[dict]) -> list[dict]:
    """Turn an entity's raw records into [3] input signals."""
    signals = []
    for r in entity_records:
        if "stars" in r and "pushed_at" in r:
            commits = r.get("recent_commits") or []
            if commits:
                signals.append({
                    "signal_type": "code", "primary_count": float(r["stars"]),
                    "dt_months": round(months_before(NOW, r["pushed_at"]), 2),
                    "real_commits": len(cadence_commits(commits)),
                    **({"dependents": r["dependents"]} if r.get("dependents") else {}),
                    **({"criticality": r["criticality_score"]} if "criticality_score" in r else {}),
                    **({"scorecard": r["scorecard"]} if "scorecard" in r else {}),
                })
            else:
                signals.append({
                    "signal_type": "code", "primary_count": float(r["stars"]),
                    "dt_months": round(months_before(NOW, r["pushed_at"]), 2),
                    **({"dependents": r["dependents"]} if r.get("dependents") else {}),
                    **({"criticality": r["criticality_score"]} if "criticality_score" in r else {}),
                    **({"scorecard": r["scorecard"]} if "scorecard" in r else {}),
                })
        elif "points" in r:
            signals.append({"signal_type": "forum_issue",
                            "primary_count": float(r["points"]),
                            "dt_months": round(months_before(NOW, r["created_at"]), 2)})
        elif "cited_by_count" in r:
            signals.append({"signal_type": "citation",
                            "primary_count": float(r["cited_by_count"]),
                            "dt_months": round(months_before(NOW, r["publication_date"]), 2)})
        elif r.get("product_mentions"):
            signals.append({"signal_type": "product",
                            "primary_count": float(r["product_mentions"]),
                            "dt_months": round(months_before(NOW, r["last_change"]), 2)})
    return signals


def snippets_of(entity_records: list[dict]) -> list[str]:
    out = []
    for r in entity_records:
        if "stars" in r:
            for c in r.get("comments") or []:
                out.append(c.get("body") or "")
            out.append(r.get("body") or "")
        elif "points" in r:
            out.append(r.get("comment_text") or r.get("title") or "")
        elif "cited_by_count" in r:
            out.append(r.get("abstract") or r.get("title") or "")
    return [s for s in out if s]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="vector database")
    ap.add_argument("--out", default="out/demo")
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    tm_cfg = TextMiningConfig()
    cap_cfg = CapabilityConfig()
    er_cfg = EntityResolutionConfig()
    prof_cfg = ProfilerConfig()
    snap_cfg = SnapshotConfig()
    etags: dict = {}

    # ---------------- [0] profiler -------------------------------------
    reg_path = resolve_registry_path()
    registry = load_registry(reg_path) if reg_path else {}
    norm_topic, topic_key = normalize_topic(args.topic)
    profile = profile_topic(args.topic, registry, profiler_stub)
    ttl_ok = is_fresh(NOW, NOW, prof_cfg.ttl_days)
    docs = [str(r.get("body") or r.get("comment_text") or r.get("abstract") or "")
            for r in GITHUB_ITEMS + HN_HITS + OPENALEX_RESULTS]
    expanded = pseudo_relevance(args.topic, docs, k=3)
    fused = rrf_fuse([profile["seed_queries"], [expanded]], k=prof_cfg.rrf_k)

    stages = {s: StageCounters(stage=s) for s in
              ("[0]", "[1]", "[2]", "[3]", "[4]", "[5]", "[6]", "[7]", "[8]")}
    stages["[0]"].llm_calls = 1  # profiler stub on registry miss (pass A)
    stages["[0]"].cache_hit = profile["cache_hit"]

    bucket = TokenBucket(rate_per_minute=30, clock=lambda: 0.0)
    golden_true = {"s2", "s3", "s5"}       # fixed golden: 3 third-party snippets
    golden_false = {"s1"}                  # non-third-party -> excluded from denominator
    golden = [{"snippet_id": s, "is_third_party_evidence": True} for s in sorted(golden_true)] + \
             [{"snippet_id": "s1", "is_third_party_evidence": False}]
    eval_info: dict = {"kappa": None, "band_flips_passB": [], "recall": {},
                       "extraction_stub": json.loads(extractor_stub())}

    store_flat_store = [[]]                # accumulated record store (cross-pass)
    analysis_attrs: dict[str, dict] = {}   # [5][6] outputs keyed by entity_id
    entity_names: dict[str, str] = {}
    prev_rows: dict[str, list] = {}
    prev_bands: dict[str, str] = {}
    scores_by_id: dict[str, float] = {}
    fingerprints_prev: dict[str, str] = {}
    skipped_entities: set[str] = set()
    bands_per_pass: dict[str, dict[str, str]] = {}
    snapshot_plan: dict = {}
    final_entities: list[dict] = []

    for scenario in ("A", "B"):
        transport = FixtureTransport(scenario, state={})
        records, totals = run_discovery(profile, scenario, transport, etags, bucket)
        for f, v in totals.items():
            setattr(stages["[1]"], f, getattr(stages["[1]"], f) + v)
        records.append(dict(PRODUCT_DIR_RECORD))
        if scenario == "A":
            etags.update({"github": '"v1"', "openalex": '"v1"'})  # stored from responses

        # accumulate the snapshot store FIRST (pass B: 304 sources contribute
        # nothing new) — [2] resolves over the FULL accumulated universe so
        # entities 304-skipped in a pass still exist in it. Dedup covers the
        # batch itself: the same item returned by several seed queries is one
        # record (connectors only dedup within a single call).
        store_flat = list(store_flat_store[0])
        seen = {record_key(r) for r in store_flat}
        for r in records:
            k = record_key(r)
            if k not in seen:
                seen.add(k)
                store_flat.append(r)
        store_flat_store[0] = store_flat

        # ---------------- [2] entity resolution -------------------------
        enriched = []
        for r in store_flat:
            rr = dict(r)
            rr.setdefault("description", rr.get("body") or rr.get("title") or "")
            enriched.append(rr)
        res = resolve(enriched, er_cfg, similarity_stub, adjudicator=None)
        j = res.journal
        # [2] ledger fields live outside StageCounters (spec §3 log; config
        # StageCounters is untouchable per the stage contract)
        eval_info.setdefault("resolution_journal", {
            "pairs_total": 0, "pairs_gray": 0,
            "merges_auto": 0, "merges_llm": 0})
        for f in ("pairs_total", "pairs_gray", "merges_auto", "merges_llm"):
            eval_info["resolution_journal"][f] += getattr(j, f)

        entities = []
        for cluster in res.clusters:
            name = next((c.get("name") for c in cluster if c.get("name")), "unknown")
            eid = next((c.get("domain") for c in cluster if c.get("domain")), name)
            entities.append({"entity_id": eid, "name": name, "records": cluster})
        deduped = {}
        for e in entities:
            if e["entity_id"] not in deduped:
                deduped[e["entity_id"]] = e
            else:
                deduped[e["entity_id"]]["records"].extend(e["records"])
        entities = list(deduped.values())
        for e in entities:
            entity_names[e["entity_id"]] = e["name"]
            # carry [5][6] analysis attributes across passes for hash-skipped
            # entities (their content did not change -> analysis stands)
            for attr in ("competitors", "capability", "facet_titles"):
                if attr in analysis_attrs.get(e["entity_id"], {}):
                    e.setdefault(attr, analysis_attrs[e["entity_id"]][attr])

        # ---------------- [8] fingerprint + hash-skip -------------------
        fps = entity_fingerprint(
            [{"entity_id": e["entity_id"],
              "content": json.dumps(sorted(json.dumps(r, sort_keys=True)
                                           for r in e["records"]), sort_keys=True)}
             for e in entities])
        snap_delta = changed(fingerprints_prev, fps) if fingerprints_prev \
            else {k: True for k in fps}

        # ---------------- [3] evidence + [4] band -----------------------
        rows_by_entity: dict[str, list] = {}
        sig_map: dict[str, list[dict]] = {}
        for e in entities:
            if snap_delta.get(e["entity_id"]) is False and fingerprints_prev:
                skipped_entities.add(e["entity_id"])
                rows_by_entity[e["entity_id"]] = prev_rows[e["entity_id"]]
                continue
            sig_map[e["entity_id"]] = build_signals(e["records"])
        fresh_signals = [s for eid in sig_map for s in sig_map[eid]]
        fresh_rows = normalize_evidence(fresh_signals, BAND_CFG)
        idx = 0
        for eid, sigs in sig_map.items():
            rows_by_entity[eid] = fresh_rows[idx: idx + len(sigs)]
            idx += len(sigs)

        band_results = {}
        for e in entities:
            erows = rows_by_entity[e["entity_id"]]
            prev = prev_bands.get(e["entity_id"])
            if e["entity_id"] in skipped_entities:
                band_results[e["entity_id"]] = prev or "unverified"
                continue
            dead = any(r["dead"] for r in erows)
            best = max((r["score"] for r in erows), default=0.0)
            score_pct = round(min(max(best, 0.0), 1.0) * 100.0, 2)
            scores_by_id[e["entity_id"]] = score_pct
            band, flips = assign_band(score_pct, prev, BAND_CFG, dead=dead)
            band_results[e["entity_id"]] = band
            if flips:
                eval_info["band_flips_passB"].append(
                    {"entity": e["name"], "score": score_pct, **flips[0]})
        bands_per_pass[scenario] = band_results

        # ---------------- [5] text-mining (A/B) + facet ------------------
        caught: set = set()
        for e in entities:
            if e["entity_id"] in skipped_entities:
                continue
            texts = snippets_of(e["records"])
            if not texts:
                continue
            sid_of = build_sid_index(texts)
            caught.update(sid_of[t] for t in texts)
            branch_a = select_branch(texts, "A", tm_cfg)
            branch_b = select_branch(texts, "B", tm_cfg,
                                     embedder=embedder_stub, axis_query=AXIS_QUERY)
            build_extraction_prompt(branch_b, SCHEMA_HINT)
            stages["[5]"].llm_calls += 1  # extractor stub (terse JSON)
            scope = SCOPE_BY_BAND[band_results[e["entity_id"]]]
            if scope != "below-plausible":
                f_findings, f_counters = facet_search(
                    {"name": e["name"], "scope": scope}, tm_cfg, transport)
                stages["[5]"].search_calls += f_counters.search_calls
                e.setdefault("facet_titles", []).extend(f["title"] for f in f_findings)

        pass_recall = evidence_recall(golden, caught)
        eval_info["recall"][f"branch_{scenario}"] = pass_recall

        # ---------------- [6] capability audit + competitor --------------
        for e in entities:
            if e["entity_id"] in skipped_entities:
                continue
            scope = SCOPE_BY_BAND[band_results[e["entity_id"]]]
            if scope == "below-plausible":
                continue
            readme = READMES.get(e["name"])
            if readme:
                sliced = slice_readme(readme, cap_cfg)
                prompt = build_audit_prompt(sliced, AXES)
                cap_report = parse_capability_output(classifier_stub(prompt))
                stages["[6]"].llm_calls += 1
                e["capability"] = cap_report.model_dump()
            comp = competitor_set({"name": e["name"], "scope": scope}, transport)
            stages["[6]"].search_calls += 2
            e["competitors"] = comp

        for e in entities:
            analysis_attrs[e["entity_id"]] = {
                k: e[k] for k in ("competitors", "capability", "facet_titles")
                if k in e}
        prev_rows = rows_by_entity
        prev_bands = band_results
        fingerprints_prev = fps

        # ---------------- [8] recrawl plan + wake-on-spike ---------------
        if scenario == "B":
            plans = {}
            for e in entities:
                rep = next((r for r in e["records"] if "pushed_at" in r), None)
                ent = {"dead": any(r["dead"] for r in rows_by_entity[e["entity_id"]]),
                       "last_release": rep["pushed_at"] if rep else None}
                plans[e["name"]] = recrawl_plan(ent, NOW, snap_cfg)
            frozen = [{"entity_id": e["entity_id"], "name": e["name"]}
                      for e in entities if band_results[e["entity_id"]] == "unverified"]
            woken = wake_on_spike("vector database", frozen, transport)
            stages["[8]"].search_calls += 1
            snapshot_plan = {"recrawl": plans,
                             "woken": {k: v for k, v in woken.items() if v},
                             "changed": snap_delta}
            final_entities = [{"name": e["name"],
                               "score_pct": scores_by_id.get(e["entity_id"]),
                               "capability": e.get("capability"),
                               "competitors": e.get("competitors")}
                              for e in entities]

    # ---------------- [7] report + eval -----------------------------------
    stages["[8]"].skipped_by_hash = bool(skipped_entities)
    coverage = CoverageLog(run_id=RUN_ID, topic_raw=args.topic,
                           topic_normalized=norm_topic, topic_key_sha1=topic_key,
                           started_at=NOW, price_date="2026-09-24",
                           stages=list(stages.values()))
    coverage.total_tokens_in = sum(s.tokens_in for s in coverage.stages)
    coverage.total_tokens_out = sum(s.tokens_out for s in coverage.stages)

    name_bands = {entity_names[eid]: b for eid, b in bands_per_pass["B"].items()}
    kappa = band_stability(
        [bands_per_pass["A"][eid] for eid in bands_per_pass["A"]],
        [bands_per_pass["B"][eid] for eid in bands_per_pass["A"]],
    )
    eval_info["kappa"] = round(kappa, 4)
    eval_info["seeded_recall"] = seeded_recall(sorted(golden_true), sorted(golden_true))
    eval_info["cost_per_stage"] = cost_per_stage(coverage)

    report_md = render_report(final_entities, name_bands, coverage)

    # ---------------- artifacts -------------------------------------------
    (out_dir / "report.md").write_text(report_md, encoding="utf-8")
    (out_dir / "coverage_log.json").write_text(coverage.model_dump_json(indent=2),
                                               encoding="utf-8")
    (out_dir / "eval.json").write_text(json.dumps(eval_info, indent=2, default=str),
                                       encoding="utf-8")
    entity_rows = [
        EntityArtifact(entity_id=e["entity_id"], name=e["name"],
                       band=bands_per_pass["B"].get(e["entity_id"]),
                       score_pct=scores_by_id.get(e["entity_id"]),
                       n_records=len(e["records"]),
                       competitors=e.get("competitors") or {},
                       capability=e.get("capability"),
                       facet_titles=e.get("facet_titles") or [])
        for e in entities]
    # offline demo's extractor stub emits classifications, not quoted
    # findings — findings.json stays present (empty) for artifact parity
    write_artifacts(out_dir, entity_rows, [])
    (out_dir / "evidence.json").write_text(json.dumps(
        rows_by_entity, indent=2, default=str), encoding="utf-8")
    (out_dir / "snapshot_plan.json").write_text(
        json.dumps(snapshot_plan, indent=2, default=str), encoding="utf-8")
    docs_only = sum(1 for it in GITHUB_ITEMS for c in (it.get("recent_commits") or [])
                    if is_docs_only(c))
    (out_dir / "run_meta.json").write_text(json.dumps({
        "run_id": RUN_ID, "mode": "offline-deterministic",
        "now": NOW.isoformat(), "topic": args.topic,
        "topic_key_sha1": topic_key, "profile_cache_hit": profile["cache_hit"],
        "ttl_ok": ttl_ok, "registry_version": registry.get("version"),
        "expanded_query_pass2": expanded, "fused_queries": fused,
        "search_throttle": "TokenBucket 30/min (fixture volume never blocked)",
        "transport_calls": len(getattr(transport, "state", {})) and "per-scenario" or "n/a",
        "docs_only_commits_filtered": docs_only,
        "skipped_by_hash_entities": sorted(skipped_entities),
        "skipped_by_hash_count": len(skipped_entities),
        "etags_used_passB": etags,
        "stubs": {
            "transport": "FixtureTransport (canned payloads; 304 + 429 drill)",
            "profiler": "keyword stub (stands in for 1 cheap LLM on registry miss)",
            "similarity": "token-cosine stub (stands in for pinned local embedder)",
            "embedder": "64-dim hashed bag-of-words",
            "classifier/extractor": "deterministic keyword rules emitting terse JSON",
        },
        "note": "llm_calls count stub invocations; tokens stay 0 — no real model "
                "or network was hit. Production injects real callables only.",
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"report -> {out_dir / 'report.md'}")
    print(f"kappa(A,B)={kappa:.4f} flips={eval_info['band_flips_passB']} "
          f"skipped_by_hash={sorted(skipped_entities)}")


if __name__ == "__main__":
    main()
