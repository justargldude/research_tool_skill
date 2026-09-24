"""Shared runtime for the scout v6 runner scripts.

run_pipeline.py (offline demo), run_research.py (live research) and
.agent/skills/scout-research/scripts/run_skill_pipeline.py (agent skill)
used to carry ~2.1k lines of copy-pasted orchestration helpers. Both the
KeyError patch (sid lookup vs strip_boilerplate) and the registry-path fix
landed in only ONE copy — this module is the single source for everything
the runners share: transport with per-host pacing, deterministic stubs,
entity shaping, registry resolution, and validated artifact writers.

Production injectables (LLM callables, fixture transports) stay owned by
the runners; only the genuinely common machinery lives here.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from scout.config import EntityArtifact, FindingArtifact
from scout.evidence import cadence_commits


# ---------------------------------------------------------------- credentials

def load_gateway_key(path: Path | str) -> str:
    """Key for OmniRoute gateway calls. The path may point at either a bare
    key file OR the dsh credentials YAML (the historical default) — reading
    a multi-line YAML raw as the Bearer key breaks every gateway call.
    Falls back to the OMNIROUTE_API_KEY env var, then empty string."""
    p = Path(path).expanduser()
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        if "OMNIROUTE_API_KEY" in text:
            data = yaml.safe_load(text) or {}
            key = (data.get("refs") or {}).get("OMNIROUTE_API_KEY")
            if key:
                return str(key)
        elif text:
            return text
    import os

    return os.environ.get("OMNIROUTE_API_KEY", "")


# ------------------------------------------------------- registry resolution

def resolve_registry_path(hint: Path | str | None = None) -> Path | None:
    """Locate scout/domain_registry.yaml from any cwd or import context.

    Each runner used to re-implement this (two copies without an existence
    guard, one with); a miss here silently disabled Stage [0] cache hits.
    """
    roots: list[Path] = []
    if hint is not None:
        roots.append(Path(hint))
    pkg_dir = Path(__file__).resolve().parent  # <repo>/scout
    roots += [pkg_dir, pkg_dir.parent, Path.cwd()]
    for root in roots:
        if root.is_file():
            return root
        base = root if root.name == "scout" else root / "scout"
        reg = base / "domain_registry.yaml"
        if reg.exists():
            return reg
    return None


# ------------------------------------------------------------ record shaping

def gh_slug(record: dict) -> str | None:
    """owner/repo slug from a GitHub html_url, lowercased; None otherwise."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)", str(record.get("html_url", "")))
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def rebuild_abstract(item: dict) -> str:
    """Rebuild OpenAlex abstract text from its inverted word index."""
    inv = item.get("abstract_inverted_index")
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def entities_from_clusters(res: Any) -> list[dict]:
    """Shape resolve() clusters into entity dicts, deduped by entity_id.

    First named record wins as display name; records of clusters sharing an
    entity_id are merged (same convention the runners hardcoded).
    """
    entities: list[dict] = []
    for cluster in res.clusters:
        name = next((c.get("name") for c in cluster if c.get("name")), "unknown")
        eid = next((c.get("domain") for c in cluster if c.get("domain")), name)
        entities.append({"entity_id": eid, "name": name, "records": list(cluster)})
    deduped: dict[str, dict] = {}
    for e in entities:
        if e["entity_id"] not in deduped:
            deduped[e["entity_id"]] = e
        else:
            deduped[e["entity_id"]]["records"].extend(e["records"])
    return list(deduped.values())


# ---------------------------------------------------- deterministic stubs

def _tokens(text: str) -> dict[str, float]:
    d: dict[str, float] = {}
    for t in re.findall(r"[a-z0-9]+", str(text).lower()):
        d[t] = d.get(t, 0.0) + 1.0
    return d


def similarity_stub(a: dict, b: dict) -> float:
    """Token-cosine on name+description (stands in for a pinned embedder)."""
    ta = _tokens(str(a.get("name", "")) + " " + str(a.get("description", "")))
    tb = _tokens(str(b.get("name", "")) + " " + str(b.get("description", "")))
    dot = sum(v * tb.get(k, 0.0) for k, v in ta.items())
    na = math.sqrt(sum(v * v for v in ta.values()))
    nb = math.sqrt(sum(v * v for v in tb.values()))
    return dot / (na * nb) if na and nb else 0.0


def embedder_stub(text: str) -> list[float]:
    """64-dim hashed bag-of-words (deterministic; no embedding model pinned)."""
    vec = [0.0] * 64
    for t in re.findall(r"[a-z0-9]+", str(text).lower()):
        vec[hash(t) % 64] += 1.0
    return vec


def months_before(now: datetime, iso: str) -> float:
    """Age in 30.44-day months; malformed/unknown dates count as 36 months."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return max(0.0, (now - dt).total_seconds()) / (86400.0 * 30.44)
    except Exception:
        return 36.0


# --------------------------------------------------------- signal extraction

def snippets_of(entity_records: list[dict]) -> list[str]:
    """Flatten an entity's records into minable text snippets (branch-A raw)."""
    out = []
    for r in entity_records:
        if "stars" in r:
            out.extend(c.get("body") or "" for c in r.get("comments") or [])
            out.append(r.get("body") or "")
        elif "points" in r:
            out.append(r.get("comment_text") or r.get("title") or "")
        elif "cited_by_count" in r:
            out.append(r.get("abstract") or r.get("title") or "")
        elif r.get("title"):
            out.append(r.get("title"))
    return [s for s in out if s]


def build_signals(entity_records: list[dict], now: datetime) -> list[dict]:
    """Turn raw records into [3] evidence signals (code/forum/citation)."""
    signals = []
    for r in entity_records:
        if "stars" in r and "pushed_at" in r:
            commits = r.get("recent_commits") or []
            sig = {"signal_type": "code", "primary_count": float(r["stars"]),
                   "dt_months": round(months_before(now, r["pushed_at"]), 2)}
            if commits:
                sig["real_commits"] = len(cadence_commits(commits))
            signals.append(sig)
        elif "points" in r:
            created = r.get("created_at")
            if not created and r.get("created_at_i"):
                # Algolia always carries the unix timestamp even when the
                # ISO string is absent (OCR round-2 finding)
                created = datetime.fromtimestamp(
                    int(r["created_at_i"]), tz=timezone.utc).isoformat()
            signals.append({"signal_type": "forum_issue",
                            "primary_count": float(r["points"]),
                            "dt_months": round(months_before(now, created or ""), 2)})
        elif "cited_by_count" in r:
            signals.append({"signal_type": "citation",
                            "primary_count": float(r["cited_by_count"]),
                            "dt_months": round(months_before(now, r.get("publication_date", "")), 2)})
    return signals


# ---------------------------------------------------------------- transport

class Resp:
    """Minimal response object shared by the real transport and tests."""

    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = headers
        self._body = body
        self._json = None

    def json(self):
        if self._json is None:
            try:
                self._json = json.loads(self._body or b"{}")
            except json.JSONDecodeError:
                self._json = {}
        return self._json


class HttpTransport:
    """urllib transport with per-host pacing and retry-friendly error mapping.

    wake_on_spike contract: transport(topic) with a bare topic string (no
    '://') performs one topic-level HN Algolia sweep — runners used to lose
    that sweep silently when their copy lacked the mapping.
    """

    def __init__(self, note: list[str] | None = None,
                 user_agent: str = "scout-pipeline/0.2 (shared runtime)"):
        self.note = note if note is not None else []
        self.last: dict[str, float] = {}
        self.count = 0
        self.user_agent = user_agent

    def __call__(self, url: str, **kwargs) -> Resp:
        if "://" not in url:
            url = ("https://hn.algolia.com/api/v1/search?query="
                   + urllib.parse.quote(url))
        params = kwargs.get("params") or {}
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("User-Agent", self.user_agent)
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params)}"
        self.count += 1
        host = urllib.parse.urlsplit(url).netloc
        wait = 6.5 if ("api.github.com" in host and "/search" in url) else \
            1.2 if "api.github.com" in host else 0.3
        delta = time.monotonic() - self.last.get(host, 0.0)
        if delta < wait:
            time.sleep(wait - delta)
        self.last[host] = time.monotonic()

        req = urllib.request.Request(url, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=20)
            try:
                return Resp(r.status, dict(r.headers.items()), r.read())
            finally:
                close = getattr(r, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as e:
            return Resp(e.code, dict(e.headers.items()) if e.headers else {}, e.read())
        except Exception as e:
            self.note.append(f"transport {host}: {e}")
            return Resp(599, {}, b"{}")


# ----------------------------------------------------------- artifact writer

def write_artifacts(out_dir: Path | str,
                    entities: list[EntityArtifact],
                    findings: list[FindingArtifact]) -> None:
    """Persist entities.json / findings.json through the pydantic contracts.

    The artifacts used to be free-form dicts json.dumps'ed per runner and
    drifted (run_pipeline omitted score_pct/n_records and never wrote
    findings.json). Writing through the models makes the shape identical and
    validated; schemas/*.schema.json are generated from the same models.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "entities.json").write_text(
        json.dumps([e.model_dump() for e in entities], indent=2, ensure_ascii=False),
        encoding="utf-8")
    (out_dir / "findings.json").write_text(
        json.dumps([f.model_dump() for f in findings], indent=2, ensure_ascii=False),
        encoding="utf-8")
