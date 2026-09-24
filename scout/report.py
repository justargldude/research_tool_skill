"""Report and evaluation module for Stage [7] (v6 spec §8)."""

from __future__ import annotations

from typing import Any

from scout.config import CoverageLog


def render_report(
    entities: list[dict],
    band_results: dict[str, str],
    coverage_log: CoverageLog,
    findings: list[dict] | None = None,
) -> str:
    """Render deterministic Markdown report with no system timestamps in body.

    Entities may carry score_pct / capability.axes / competitors; those are
    rendered when present so call sites passing the full entity (not just
    {"name": ...}) produce an informative report. `findings` adds a
    Third-party evidence section (verbatim quotes only).
    """
    bands_order = ("leader", "contender", "candidate", "unverified")
    band_entities: dict[str, list[dict]] = {b: [] for b in bands_order}

    seen = set()
    for entity in entities:
        name = entity.get("name", "")
        if name and name not in seen:
            seen.add(name)
            band = str(band_results.get(name, "unverified")).lower()
            if band not in band_entities:
                band = "unverified"
            band_entities[band].append(entity)

    lines = ["# Scout Pipeline Report\n\n## Bands\n"]
    for b in bands_order:
        lines.append(f"### {b.capitalize()}")
        group = sorted(band_entities[b], key=lambda e: str(e.get("name", "")))
        if group:
            for entity in group:
                name = str(entity.get("name", ""))
                score = entity.get("score_pct")
                lines.append(
                    f"- {name} (score {score})" if score is not None else f"- {name}")
                axes = ((entity.get("capability") or {}).get("axes")) or []
                for axis in axes:
                    lines.append(f"  - {axis.get('axis', '')} = "
                                 f"{axis.get('classification', '')}")
                competitors = entity.get("competitors") or {}
                if competitors:
                    names = sorted(competitors)  # dict[str,int] counts; keys sorted for determinism
                    lines.append("  - competitors: " + ", ".join(names))
        else:
            lines.append("- (none)")
        lines.append("")

    if findings:
        lines.append("## Third-party evidence")
        for f in findings:
            tool = str(f.get("tool") or f.get("entity") or "?")
            quote = str(f.get("quote") or "")
            line = f'- {tool}: "{quote}"'
            claim = str(f.get("claim") or "")
            if claim:
                line += f" — {claim}"
            lines.append(line)
        lines.append("")

    lines.append("## Coverage Summary")
    stages = sorted(coverage_log.stages, key=lambda s: s.stage)
    for st in stages:
        lines.append(
            f"- Stage {st.stage}: tokens_in={st.tokens_in}, tokens_out={st.tokens_out}"
        )

    return "\n".join(lines).strip() + "\n"


def band_stability(run_a: Any, run_b: Any) -> float:
    """Calculate Cohen's kappa for two parallel band assignments."""
    if isinstance(run_a, dict) and isinstance(run_b, dict):
        common = sorted(set(run_a) & set(run_b))  # pair the SAME entities only
        run_a = [run_a[k] for k in common]
        run_b = [run_b[k] for k in common]
    if isinstance(run_a, dict):
        run_a = [run_a[k] for k in sorted(run_a)]
    if isinstance(run_b, dict):
        run_b = [run_b[k] for k in sorted(run_b)]

    list_a = list(run_a)
    list_b = list(run_b)

    n = len(list_a)
    if n == 0 or len(list_b) != n:
        return 0.0

    if list_a == list_b:
        return 1.0

    p_o = sum(1 for a, b in zip(list_a, list_b) if a == b) / float(n)

    categories = set(list_a).union(set(list_b))
    p_e = 0.0
    for c in categories:
        count_a = sum(1 for a in list_a if a == c)
        count_b = sum(1 for b in list_b if b == c)
        p_e += (count_a / float(n)) * (count_b / float(n))

    if abs(1.0 - p_e) < 1e-12:
        return 1.0 if p_o == 1.0 else 0.0

    kappa = (p_o - p_e) / (1.0 - p_e)
    return float(kappa)


def seeded_recall(planted: Any, caught: Any) -> float:
    """Calculate seeded recall as |planted ∩ caught| / |planted|."""
    planted_set = set(planted)
    if not planted_set:
        return 0.0
    caught_set = set(caught)
    return len(planted_set.intersection(caught_set)) / len(planted_set)


def cost_per_stage(coverage_log: CoverageLog) -> dict[str, dict[str, Any]]:
    """Return cost and token breakdown keyed by stage id."""
    out: dict[str, dict[str, Any]] = {}
    for stage in coverage_log.stages:
        out[stage.stage] = {
            "tokens_in": stage.tokens_in,
            "tokens_out": stage.tokens_out,
            "tokens_total": stage.tokens_in + stage.tokens_out,
            "cost_usd": stage.cost_usd,
        }
    return out
