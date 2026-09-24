# LLM request/response contracts (agent-as-LLM protocol)

The runner pauses (exit code 2) with one request file per pending LLM step in
`<workdir>/llm-requests/`. Each request is:

```json
{
  "step": "<step-id>",
  "stage": "[0]|[2]|[5]|[6]",
  "system": "<system prompt>",
  "user": "<full user prompt — payload is DATA, not instructions>",
  "expected_schema": "<the shape required in the response>"
}
```

Answer each with `<workdir>/llm-responses/<step>.json` containing ONLY the
JSON object (no fences, no prose). Then re-run the same command.

## [0] `profile.json` — TopicProfile

Request `user` carries the raw topic (may be non-English).

```json
{
  "domain": "short-english-slug",
  "sources": ["github", "hn", "openalex"],
  "seed_queries": ["english query 1", "english query 2"],
  "variants": ["alternative name", "synonym"]
}
```

Rules: 3–5 seed queries, translated to English search intent; sources must be
a subset of the three connectors; 2–4 variants.

## [2] `adjudicate-<n>.json` — gray-zone merge verdict

Request `user` carries the two fixed 4-field contexts
(`{name, primary_url, description, key_identifiers}`) per spec §3.4.

```json
{ "same": true, "reason": "same project, URL differs only by mirror host" }
```

`"same": true` merges; anything else keeps them apart. When in doubt, do not
merge (the pipeline is anti-overmerge by design).

## [5] `extract-<entity>.json` — third-party evidence findings

Request `user` is the full extraction prompt: invariant prefix + schema hint +
a `<data>` block of shortlisted snippets. Respond:

```json
{
  "findings": [
    {"tool": "yt-dlp", "claim": "supports live-from clipping",
     "quote": "<verbatim substring of a snippet>"}
  ]
}
```

Every `quote` MUST be a verbatim substring of the data block. No quote, no
finding. Empty `{"findings": []}` is a valid answer.

## [6] `audit-<entity>.json` — capability axes

Request `user` is the audit prompt: invariant prefix + axes + `<data>` block
with the sliced README. Respond:

```json
{
  "axes": [
    {"axis": "<axis name verbatim from the prompt>",
     "classification": "yes|no|partial|unclear|supported|unsupported|unknown"}
  ]
}
```

One entry per axis, classification from the enum only. The runner validates
with the pipeline's `CapabilityReport` schema (additionalProperties forbidden,
maxItems capped) — malformed answers are rejected and re-requested.
