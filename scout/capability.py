"""Stage [6] Capability Audit preprocessing (v6 spec §7).

Zero LLM in this module: the classifier is an injectable callable owned by the caller.
This module handles markdown AST heading slicing, audit prompt assembly with invariant prefix,
structured output validation for CapabilityReport, and 0-LLM competitor_set derivation.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scout.config import CapabilityConfig

CapabilityClassification = Literal[
    "yes", "no", "partial", "unclear", "supported", "unsupported", "unknown"
]


class AxisAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: str
    classification: CapabilityClassification


class CapabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axes: list[AxisAssessment] = Field(default_factory=list, max_length=100)


def slice_readme(markdown_text: str, cfg: CapabilityConfig) -> str:
    """Cắt README: giữ heading trong keep của cfg, bỏ drop, sub-heading dưới section giữ được sống.

    Nếu không có section nào được giữ -> fallback lấy fallback_words từ đầu (default 500).
    """
    keep_set = {h.strip().lower() for h in cfg.keep}
    drop_set = {h.strip().lower() for h in cfg.drop}

    lines = markdown_text.splitlines()
    kept_content_lines: list[str] = []
    all_body_lines: list[str] = []

    current_state = "SKIP"
    current_level = 0

    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            heading_title = re.sub(r"[#\s]+$", "", m.group(2)).strip().lower()

            if heading_title in keep_set:
                current_state = "KEEP"
                current_level = level
            elif heading_title in drop_set:
                current_state = "DROP"
                current_level = level
            else:
                if current_state == "KEEP" and level > current_level:
                    # Sub-heading nằm dưới section được giữ thì sống
                    pass
                else:
                    current_state = "DROP"
                    current_level = level
        else:
            if stripped:
                all_body_lines.append(stripped)
            if current_state == "KEEP" and stripped:
                kept_content_lines.append(stripped)

    if kept_content_lines:
        return "\n".join(kept_content_lines)

    # Fallback: lấy fallback_words từ đầu
    all_words = " ".join(all_body_lines).split()
    cap = cfg.fallback_words if cfg.fallback_words is not None else 500
    return " ".join(all_words[:cap])


def build_audit_prompt(text: str, axes: list[str]) -> str:
    """Tạo prompt audit: prefix bất biến >= 40 ký tự, chứa các axes, văn bản bọc delimiter data."""
    axes_str = ", ".join(axes)
    prefix = (
        "You are an expert capability auditor. Evaluate the project against target capability axes: "
        f"{axes_str}.\n"
        "Output your classification strictly in JSON format according to the CapabilityReport schema.\n\n"
        "=== BEGIN SCRAPED DATA ===\n"
    )
    suffix = "\n=== END SCRAPED DATA ===\nPlease output the valid JSON response."
    return prefix + text + suffix


def parse_capability_output(payload_str: str) -> CapabilityReport:
    """Parse và validate payload JSON sang CapabilityReport.

    Raise ValueError nếu JSON hỏng, không phải object, hoặc vi phạm schema.
    """
    try:
        data = json.loads(payload_str)
    except Exception as exc:
        raise ValueError(f"Malformed JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")

    try:
        return CapabilityReport.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Invalid capability report: {exc}") from exc


STOPWORDS = {
    "a", "an", "the", "and", "or", "vs", "versus", "to", "from", "for", "in",
    "on", "at", "by", "with", "about", "is", "are", "was", "were", "be", "been",
    "solid", "alternative", "alternatives", "migrating", "leaving", "switch",
    "switching", "replace", "replacing", "replacement", "better", "than",
    "comparison", "compare", "compared", "show", "hn", "ask", "why", "we",
    "left", "not", "how", "what", "which", "my", "our", "side", "project",
    "weekly", "thread", "topic", "using", "use", "uses", "good", "bad", "new",
    "over", "into", "out", "of", "it", "its", "up", "shows", "any", "some",
}


def competitor_set(
    entity: Any,
    transport: Any,
    config: Any = None,
) -> dict[str, int]:
    """Tạo tập competitor_set: 0-LLM, bắn HN Algolia 2 template, scope PLAUSIBLE+."""
    entity_name = str(entity if isinstance(entity, str) else entity.get("name", "")).strip()
    scope = str(entity.get("scope", "plausible") if isinstance(entity, dict) else "plausible").strip().lower()

    # Scope gate: scope "below-plausible" -> 0 call
    if "below" in scope:
        return {}
    if not entity_name:  # blank templates would match every title; emit zero calls
        return {}

    # Bắn 2 template HN Algolia
    resp1 = transport(f"{entity_name} vs")
    resp2 = transport(f"alternative to {entity_name}")

    hits: list[dict[str, Any]] = []
    for resp in (resp1, resp2):
        body = resp.json() if hasattr(resp, "json") else resp
        if isinstance(body, dict) and "hits" in body:
            hits.extend(body["hits"])

    name_lower = entity_name.lower()
    co_counts: dict[str, int] = {}

    for h in hits:
        if not isinstance(h, dict):
            continue
        title = str(h.get("title", ""))
        title_lower = title.lower()

        # Title không co-occur với entity -> loại bỏ
        if name_lower not in title_lower:
            continue

        tokens = re.findall(r"[a-zA-Z0-9_\-]+", title)
        seen_in_title: set[str] = set()
        for tok in tokens:
            cleaned = tok.strip("-_").lower()
            if len(cleaned) < 2:
                continue
            if cleaned == name_lower:
                continue
            if cleaned in STOPWORDS:
                continue
            seen_in_title.add(cleaned)

        for comp in seen_in_title:
            co_counts[comp] = co_counts.get(comp, 0) + 1

    # Sắp xếp deterministic: count giảm dần, sau đó theo tên
    return dict(sorted(co_counts.items(), key=lambda item: (-item[1], item[0])))


build_competitor_set = competitor_set
derive_competitors = competitor_set
competitors_for = competitor_set
