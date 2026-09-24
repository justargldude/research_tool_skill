#!/usr/bin/env python3
"""Live research run of the scout v6 pipeline (model agy/gemini-3.8-flash-high).

Same stage chain [0]→[8] as run_pipeline.py, but the injected callables are
REAL: LLM callables go through the OmniRoute gateway (OpenAI-compatible) with
model agy/gemini-3.8-flash-high, and transport hits the real free APIs
(GitHub issues search + REST enrichment, HN Algolia, OpenAlex). No auth keys
for the APIs (unauth limits, paced inside the transport); the research topic
is outside the domain registry so [0] spends the 1 real LLM profile call.

Honest gaps (documented, not hidden):
- similarity/embedder remain deterministic token-cosine stubs (no embedding
  model pinned in this environment) — gray-zone merging quality is limited.
- single pass: no A/B kappa; golden recall is planted via a cheap regex
  criterion (mentions of established tools), from branch-A raw (C1-safe).
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
    DiscoveryConfig,
    EntityArtifact,
    EntityResolutionConfig,
    FindingArtifact,
    ProfilerConfig,
    SnapshotConfig,
    StageCounters,
    TextMiningConfig,
)
from scout.connectors import github, hn, openalex
from scout.discovery import TokenBucket
from scout.entity import resolve
from scout.evidence import is_docs_only, normalize_evidence
from scout.profiler import fan_out, load_registry, normalize_topic, profile_topic
from scout.report import cost_per_stage, render_report, seeded_recall
from scout.runtime import (
    HttpTransport,
    build_signals,
    embedder_stub,
    entities_from_clusters,
    gh_slug,
    load_gateway_key,
    rebuild_abstract,
    resolve_registry_path,
    similarity_stub,
    snippets_of,
    write_artifacts,
)
from scout.snapshot import entity_fingerprint, recrawl_plan, wake_on_spike
from scout.textmining import (
    build_extraction_prompt,
    build_sid_index,
    evidence_recall,
    facet_search,
    lookup_sid,
    select_branch,
)

TOPIC = "tìm tool download cắt ghép trực tiếp video youtube livestream dài"
NOW = datetime(2026, 9, 24, 14, 0, 0)
MODEL = "agy/gemini-3.8-flash-high"
GW = "http://127.0.0.1:20128/v1"
OUT = Path("out/research-youtube")
AXES = ["livestream download (HLS/DASH/live)",
        "clip or segment without full download",
        "youtube access resilience (bot-detection, cookies)"]

BAND_CFG = BandConfig(
    bands=[
        BandThreshold(name="leader", min_percentile=80.0),
        BandThreshold(name="contender", min_percentile=50.0),
        BandThreshold(name="candidate", min_percentile=20.0),
        BandThreshold(name="unverified", min_percentile=0.0),
    ]
)
SCOPE_BY_BAND = {"leader": "plausible", "contender": "plausible",
                 "candidate": "plausible", "unverified": "below-plausible"}
GOLDEN_TOOL_RE = re.compile(r"\b(yt-?dlp|streamlink|youtube-?dl)\b", re.I)


# --------------------------------------------------------------------------
# LLM via OmniRoute (real model)
# --------------------------------------------------------------------------

class LLMError(Exception):
    pass


def _llm_raw(user: str, system: str, timeout: int = 180) -> tuple[str, dict]:
    key = load_gateway_key(Path.home() / ".dsh/.credentials.yaml")
    body = {"model": MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(
        f"{GW}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    data = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    content = data["choices"][0]["message"]["content"]
    return content, (data.get("usage") or {})


def llm_json(system: str, user: str, ledger: dict) -> dict:
    """One real LLM call; parse terse JSON out of fences/prose. Usage lands
    in the ledger (tokens_in/out, llm_calls); failures raise LLMError."""
    content, usage = _llm_raw(user, system)
    ledger["llm_calls"] += 1
    ledger["tokens_in"] += int(usage.get("prompt_tokens") or 0)
    ledger["tokens_out"] += int(usage.get("completion_tokens") or 0)
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise LLMError(f"no JSON object in response: {content[:120]!r}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"bad JSON: {e}: {content[:160]!r}") from e


# --------------------------------------------------------------------------
# Real HTTP transport: shared scout.runtime.HttpTransport (per-host pacing
# + wake_on_spike topic-string contract). See scout/runtime.py.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Fixtures-free record shaping
# --------------------------------------------------------------------------

def profile_llm(topic: str, ledger: dict) -> dict:
    try:
        out = llm_json(
            "You are a research topic profiler. Output ONLY a JSON object, no markdown.",
            f'Topic (Vietnamese): "{topic}"\n'
            'Return JSON: {"domain": <short english slug for the tool domain>, '
            '"sources": <subset of ["github","hn","openalex"] best for finding such tools>, '
            '"seed_queries": <3-5 English web-search queries>, '
            '"variants": <2-4 alternative product names / synonyms>}. '
            "Translate the intent into English search-engine queries.",
            ledger)
        assert isinstance(out.get("seed_queries"), list) and out["seed_queries"]
        return {"domain": str(out.get("domain") or "generated"),
                "sources": [s for s in out.get("sources", ["github", "hn"])
                            if s in ("github", "hn", "openalex")] or ["github", "hn"],
                "seed_queries": [str(q) for q in out["seed_queries"]][:5],
                "variants": [str(v) for v in out.get("variants", [])]}
    except Exception as e:
        ledger.setdefault("llm_errors", []).append(f"profiler: {e}")
        return {"domain": "generated", "sources": ["github", "hn"],
                "seed_queries": [topic], "variants": []}


def extractor_llm(prompt: str, ledger: dict) -> str:
    out = llm_json(
        "You extract third-party evidence claims. Output ONLY valid JSON.",
        prompt, ledger)
    return json.dumps(out)


def classifier_llm(prompt: str, ledger: dict) -> str:
    out = llm_json(
        "You are a strict capability auditor. Output ONLY valid JSON, no markdown.",
        prompt, ledger)
    return json.dumps(out)


def main() -> None:
    global MODEL, GW  # --model/--gateway-url override the module defaults
    ap = argparse.ArgumentParser(description="Live scout v6 research run")
    ap.add_argument("--topic", default=TOPIC)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--gateway-url", default=GW)
    ap.add_argument("--llm-entities", type=int, default=3,
                    help="[5]/[6] LLM budget: top-N plausible entities")
    ap.add_argument("--facet-entities", type=int, default=2)
    args = ap.parse_args()
    MODEL, GW = args.model, args.gateway_url
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = {"llm_calls": 0, "tokens_in": 0, "tokens_out": 0, "llm_errors": []}
    stages = {s: StageCounters(stage=s) for s in
              ("[0]", "[1]", "[2]", "[3]", "[4]", "[5]", "[6]", "[7]", "[8]")}
    notes: list[str] = []
    eval_info: dict = {"notes": notes, "band_flips": "n/a (single pass)"}

    transport = HttpTransport(note=notes, user_agent="scout-pipeline-research/0.1")
    dcfg = DiscoveryConfig(max_issues=5, max_comments=5, max_chars=1500)
    cap_cfg = CapabilityConfig()
    er_cfg = EntityResolutionConfig()
    bucket = TokenBucket(rate_per_minute=10, clock=time.monotonic)
    etags: dict = {}

    # ---------------- [0] profiler (registry MISS expected -> 1 real LLM) --
    reg_path = resolve_registry_path()
    registry = load_registry(reg_path) if reg_path else {}
    norm_topic, topic_key = normalize_topic(args.topic)
    profile = profile_topic(args.topic, registry, lambda t: profile_llm(t, ledger))
    stages["[0]"].llm_calls = ledger["llm_calls"]
    stages["[0]"].cache_hit = profile["cache_hit"]
    stages["[0]"].tokens_in = ledger["tokens_in"]
    stages["[0]"].tokens_out = ledger["tokens_out"]
    print(f"[0] domain={profile['domain']} hit={profile['cache_hit']} "
          f"queries={profile['seed_queries']}", flush=True)

    # ---------------- [1] discovery (real APIs) ----------------------------
    records: list[dict] = []
    combos = fan_out(profile["seed_queries"][:3], profile["sources"])
    for q, source in combos:
        if not bucket.take():
            break  # search quota pacing (10/min unauth)
        etag_q = {"etag": etags[source]} if etags.get(source) else {}
        if source == "github":
            recs, c = github.search(q, {"comments": ">5", **etag_q}, dcfg, transport)
        elif source == "hn":
            recs, c = hn.search(q, {"points": 5, **etag_q}, dcfg, transport)
        else:
            recs, c = openalex.search(q, {"cited_by_count": 5,
                                          "from_date": "2025-09-24",
                                          "mailto": "research-tool-skill@example.org",
                                          **etag_q}, dcfg, transport)
        for f in ("rest_calls", "search_calls", "issues_truncated_count",
                  "comments_truncated_count", "filtered_count",
                  "hits_304", "hits_403_429"):
            setattr(stages["[1]"], f, getattr(stages["[1]"], f) + int(getattr(c, f) or 0))
        records.extend(recs)
    if "github" in profile["sources"]:
        etags["github"] = '"live-run-1"'
    if "openalex" in profile["sources"]:
        etags["openalex"] = '"live-run-1"'

    # shape real payloads into the record fields the pipeline expects
    for r in records:
        if "abstract_inverted_index" in r:
            r["abstract"] = rebuild_abstract(r)
        slug = gh_slug(r)
        if slug:
            r["name"] = r.get("name") or slug
            r["domain"] = slug
        r["description"] = (r.get("body") or r.get("comment_text")
                            or r.get("abstract") or r.get("title") or "")[:400]

    # REST enrichment: repo metadata + real commit cadence (anti-farming data)
    repos: dict[str, dict] = {}
    for r in records:
        slug = gh_slug(r)
        if not slug or slug in repos or len(repos) >= 4:
            continue
        try:
            meta = transport(f"https://api.github.com/repos/{slug}").json()
            if meta.get("stargazers_count") is None:
                continue
            commits = transport(
                f"https://api.github.com/repos/{slug}/commits?per_page=30").json()
            enriched: list[dict] = []
            verify_left = 5
            for cm in (commits or [])[:30]:
                msg = str((cm.get("commit") or {}).get("message", ""))
                entry = {"message": msg}
                if is_docs_only(entry) and verify_left > 0 and cm.get("sha"):
                    full = transport(
                        f"https://api.github.com/repos/{slug}/commits/{cm['sha']}").json()
                    entry["files"] = [f.get("filename", "") for f in full.get("files", [])]
                    verify_left -= 1
                enriched.append(entry)
            repos[slug] = {"stars": int(meta["stargazers_count"]),
                           "pushed_at": meta["pushed_at"],
                           "recent_commits": enriched}
            for r2 in records:
                if gh_slug(r2) == slug:
                    r2["stars"] = repos[slug]["stars"]
                    r2["pushed_at"] = repos[slug]["pushed_at"]
                    r2["recent_commits"] = repos[slug]["recent_commits"]
            stages["[1]"].rest_calls += 2 + (5 - verify_left)
        except Exception as e:
            eval_info["notes"].append(f"enrich {slug} failed: {e}")
    print(f"[1] records={len(records)} repos_enriched={len(repos)} "
          f"search={stages['[1]'].search_calls} rest={stages['[1]'].rest_calls}", flush=True)

    # ---------------- [2] entity resolution -------------------------------
    res = resolve(records, er_cfg, similarity_stub, adjudicator=None)
    j = res.journal
    eval_info["resolution_journal"] = {f: getattr(j, f) for f in
                                       ("pairs_total", "pairs_gray",
                                        "merges_auto", "merges_llm")}
    entities = entities_from_clusters(res)
    print(f"[2] entities={len(entities)} journal={eval_info['resolution_journal']}", flush=True)

    # ---------------- [3] evidence + [4] band ------------------------------
    all_signals = []
    per_entity = {}
    for e in entities:
        sigs = build_signals(e["records"], NOW)
        per_entity[e["entity_id"]] = sigs
        all_signals.extend(sigs)
    rows = normalize_evidence(all_signals, BAND_CFG)
    idx = 0
    rows_by_entity = {}
    for e in entities:
        n = len(per_entity[e["entity_id"]])
        rows_by_entity[e["entity_id"]] = rows[idx: idx + n]
        idx += n
    band_results = {}
    scores = {}
    for e in entities:
        erows = rows_by_entity[e["entity_id"]]
        dead = any(r["dead"] for r in erows)
        best = max((r["score"] for r in erows), default=0.0)
        pct = round(min(max(best, 0.0), 1.0) * 100.0, 2)
        scores[e["entity_id"]] = pct
        band, _ = assign_band(pct, None, BAND_CFG, dead=dead)
        band_results[e["entity_id"]] = band
    stages["[3]"].llm_calls = 0
    print(f"[4] bands={ {e['name']: b for e, b in zip(entities, band_results.values())} }",
          flush=True)

    # ---------------- [5] text-mining (real extractor) + facet -------------
    entity_order = sorted(entities, key=lambda e: -scores[e["entity_id"]])
    plausible = [e for e in entity_order
                 if SCOPE_BY_BAND[band_results[e["entity_id"]]] == "plausible"]
    golden_ids: set = set()
    caught_b: set = set()
    findings_all = []
    for e in plausible[:args.llm_entities]:  # research budget: top-N entities
        texts = snippets_of(e["records"])[:25]
        if not texts:
            continue
        sid_of = build_sid_index(texts)
        golden_ids.update(h for t, h in sid_of.items() if GOLDEN_TOOL_RE.search(t))
        branch_b = select_branch(texts, "B", TextMiningConfig(),
                                 embedder=embedder_stub, axis_query=AXIS_QUERY)
        if branch_b:
            prompt = build_extraction_prompt(
                branch_b, '{"findings": [{"tool": str, "claim": str, "quote": str}]}')
            try:
                out = json.loads(extractor_llm(prompt, ledger))
                for f in out.get("findings", []):
                    findings_all.append({"entity": e["name"], **f})
                stages["[5]"].llm_calls += 1
            except Exception as ex:
                eval_info["notes"].append(f"extractor {e['name']}: {ex}")
        # branch_b texts are strip_boilerplate()-normalized; raw-keyed lookup
        # here was the KeyError that crashed the previous live run
        caught_b.update(sid for sid in (lookup_sid(sid_of, t) for t in branch_b) if sid)
        stages["[5]"].tokens_in = ledger["tokens_in"] - stages["[0]"].tokens_in
        stages["[5]"].tokens_out = ledger["tokens_out"] - stages["[0]"].tokens_out
    golden = [{"snippet_id": g, "is_third_party_evidence": True} for g in sorted(golden_ids)]
    if golden:
        eval_info["evidence_recall_branch_B"] = evidence_recall(golden, caught_b)
    for e in plausible[:args.facet_entities]:  # facet cap: N entities x 6 search calls
        f_findings, f_c = facet_search({"name": e["name"], "scope": "plausible"},
                                       TextMiningConfig(), transport)
        stages["[5]"].search_calls += f_c.search_calls
        e["facet_titles"] = [f["title"] for f in f_findings]
    print(f"[5] findings={len(findings_all)} facet_calls={stages['[5]'].search_calls}",
          flush=True)

    # ---------------- [6] capability audit (real classifier) + competitor --
    readmes: dict[str, str] = {}
    for e in plausible[:args.llm_entities]:
        slug = gh_slug(e["records"][0]) if e["records"] else None
        if not slug:
            continue
        req = urllib.request.Request(
            f"https://api.github.com/repos/{slug}/readme",
            headers={"Accept": "application/vnd.github.raw+json",
                     "User-Agent": "scout-pipeline-research/0.1"})
        try:
            readmes[e["name"]] = urllib.request.urlopen(req, timeout=20).read().decode(
                "utf-8", "ignore")[:8000]
            stages["[1]"].rest_calls += 1
        except Exception as ex:
            eval_info["notes"].append(f"readme {slug}: {ex}")
    for e in plausible[:args.llm_entities]:
        readme = readmes.get(e["name"])
        if readme:
            sliced = slice_readme(readme, cap_cfg)
            try:
                report = parse_capability_output(classifier_llm(
                    build_audit_prompt(sliced, AXES), ledger))
                e["capability"] = report.model_dump()
                stages["[6]"].llm_calls += 1
            except Exception as ex:
                eval_info["notes"].append(f"capability {e['name']}: {ex}")
    for e in plausible[:args.facet_entities]:
        try:
            e["competitors"] = competitor_set({"name": e["name"], "scope": "plausible"},
                                              transport)
            stages["[6]"].search_calls += 2
        except Exception as ex:
            eval_info["notes"].append(f"competitor {e['name']}: {ex}")
    # [5] tokens were snapshotted per-entity during the [5] loop (before any
    # [6] LLM call); [6] gets the remainder — the old recompute ran AFTER the
    # [6] audit, so [5] swallowed [6]'s audit tokens and [6] reported zeros
    stages["[6]"].tokens_in = (ledger["tokens_in"] - stages["[0]"].tokens_in
                               - stages["[5]"].tokens_in)
    stages["[6]"].tokens_out = (ledger["tokens_out"] - stages["[0]"].tokens_out
                                - stages["[5]"].tokens_out)
    # [6] LLM usage: split from [5] — recompute simply (llm totals minus [0])
    stages["[6]"].llm_calls = ledger["llm_calls"] - stages["[0]"].llm_calls - stages["[5]"].llm_calls
    print(f"[6] capability_entities={sum(1 for e in entities if e.get('capability'))}", flush=True)

    # ---------------- [8] snapshot: fingerprint + recrawl + wake -----------
    fps = entity_fingerprint([{"entity_id": e["entity_id"],
                               "content": json.dumps(
                                   sorted(json.dumps(r, sort_keys=True) for r in e["records"]),
                                   sort_keys=True)} for e in entities])
    plans = {}
    for e in entities:
        rep = next((r for r in e["records"] if "pushed_at" in r), None)
        ent = {"dead": any(r["dead"] for r in rows_by_entity[e["entity_id"]]),
               "last_release": rep["pushed_at"] if rep else None}
        plans[e["name"]] = recrawl_plan(ent, NOW, SnapshotConfig())
    frozen = [{"entity_id": e["entity_id"], "name": e["name"]}
              for e in entities if band_results[e["entity_id"]] == "unverified"]
    woken = wake_on_spike(args.topic[:80], frozen, transport)
    stages["[8]"].search_calls += 1
    snapshot_plan = {"recrawl": plans,
                     "woken": {k: v for k, v in woken.items() if v}}
    print(f"[8] tiers={ {k: v.get('tier') for k, v in plans.items()} }", flush=True)

    # ---------------- [7] report + eval ------------------------------------
    coverage = CoverageLog(run_id="live-research-20260924", topic_raw=args.topic,
                           topic_normalized=norm_topic, topic_key_sha1=topic_key,
                           started_at=NOW, price_date="2026-09-24",
                           stages=list(stages.values()))
    coverage.total_tokens_in = ledger["tokens_in"]
    coverage.total_tokens_out = ledger["tokens_out"]
    eval_info["seeded_recall"] = seeded_recall(
        sorted(golden_ids), sorted(golden_ids & caught_b)) if golden_ids else None
    eval_info["cost_per_stage"] = cost_per_stage(coverage)

    name_bands = {e["name"]: band_results[e["entity_id"]] for e in entities}
    # pass the full entity data (score/capability/competitors) — the old
    # name-only call starved render_report of everything it could show
    rich_entities = [{"name": e["name"], "score_pct": scores[e["entity_id"]],
                      "capability": e.get("capability"),
                      "competitors": e.get("competitors")}
                     for e in entities]
    report_md = render_report(rich_entities, name_bands, coverage,
                              findings=findings_all)

    entity_rows = [
        EntityArtifact(entity_id=e["entity_id"], name=e["name"],
                       band=band_results[e["entity_id"]],
                       score_pct=scores[e["entity_id"]],
                       n_records=len(e["records"]),
                       competitors=e.get("competitors") or {},
                       capability=e.get("capability"),
                       facet_titles=e.get("facet_titles") or [])
        for e in entities]
    finding_rows = [FindingArtifact(**f) for f in findings_all if f.get("quote")]
    write_artifacts(out_dir, entity_rows, finding_rows)

    (out_dir / "report.md").write_text(report_md, encoding="utf-8")
    (out_dir / "coverage_log.json").write_text(coverage.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "eval.json").write_text(json.dumps(eval_info, indent=2, default=str),
                                       encoding="utf-8")
    (out_dir / "evidence.json").write_text(json.dumps(rows_by_entity, indent=2, default=str),
                                           encoding="utf-8")
    (out_dir / "snapshot_plan.json").write_text(json.dumps(snapshot_plan, indent=2, default=str),
                                                encoding="utf-8")
    (out_dir / "run_meta.json").write_text(json.dumps({
        "mode": "LIVE-research", "model": MODEL, "topic": args.topic,
        "llm_calls": ledger["llm_calls"],
        "tokens_in": ledger["tokens_in"], "tokens_out": ledger["tokens_out"],
        "llm_errors": ledger["llm_errors"],
        "http_calls": transport.count,
        "gh_enriched_repos": list(repos),
        "notes": eval_info["notes"],
        "honest_gaps": [
            "similarity/embedder are deterministic token-cosine stubs (no pinned embedding model)",
            "single pass — no A/B kappa",
            "golden recall planted by regex on established tool names (branch-A raw, C1-safe)",
        ],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport -> {out_dir/'report.md'}", flush=True)
    try:
        from make_gui_report import render as render_gui
        (out_dir / "report.html").write_text(render_gui(out_dir), encoding="utf-8")
        print(f"report -> {out_dir/'report.html'}", flush=True)
    except Exception as exc:  # GUI là phần phụ, đừng làm hỏng run
        print(f"warn: khong sinh duoc report.html ({exc})", flush=True)
    print(f"LLM: {ledger['llm_calls']} calls, {ledger['tokens_in']} in / "
          f"{ledger['tokens_out']} out tokens; HTTP calls: {transport.count}", flush=True)


AXIS_QUERY = "youtube livestream download clip segment trim without full download"


if __name__ == "__main__":
    main()
