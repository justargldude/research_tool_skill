# research_tool_skill

An agent skill that researches software tools for any topic: discovery →
clustering → evidence scoring → capability audit → a deterministic markdown
report. Free public sources (GitHub, Hacker News, OpenAlex) — **no API
keys**. Works in Antigravity, Claude Code, and any agent that supports
SKILL.md.

## Install

Paste this into your agent (Antigravity, Claude Code, or any SKILL.md agent):

> Install the research-tool-skill: run `npx skills add justargldude/research_tool_skill --skill research-tool-skill` (Antigravity: add `-a antigravity`, Claude Code: `-a claude-code`, otherwise let me select my agent). Then follow the **Install procedure in its SKILL.md**: check and install Python ≥ 3.10 and git if missing, clone this repo to my home dir, set up the only dependency (pydantic), run the offline smoke test, and print the usage guide in full.

Manual route: follow [`skills/research-tool-skill/SKILL.md`](skills/research-tool-skill/SKILL.md)
directly — the procedure is script-free and cross-platform (Windows/macOS/Linux).

## Using it

- Just tell your agent: *"research tools to <your goal>"* — the agent runs
  the pipeline and plays its LLM (the runner pauses with JSON requests, the
  agent answers them, the run resumes from checkpoint).
- Update later by saying: **"update research-tool-skill"** — the agent pulls
  the newest commit, re-runs the offline smoke test, and reports the changelog.

## Layout

```
skills/research-tool-skill/   # the skill: SKILL.md + USAGE.md + LLM contracts + runner
scout/                        # pipeline package (deterministic machinery)
run_pipeline.py               # offline demo (no network) — also the post-install smoke test
run_research.py               # standalone runner (gateway LLM mode)
```

## Development

```bash
pip install -r requirements.txt
python run_pipeline.py --out out/demo   # offline demo, deterministic
```

Scoring thresholds and bands are spec starting points, not calibrated values;
treat bands as ordinal hints.
