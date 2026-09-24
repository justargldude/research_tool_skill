# research-tool-skill-for-antigravity

One prompt installs an agent skill that researches software tools for any
topic: discovery → clustering → evidence scoring → capability audit → a
deterministic markdown report. Free public sources (GitHub, Hacker News,
OpenAlex) — **no API keys**. Works in Antigravity, Claude Code, and any
agent that supports SKILL.md.

## Install (one prompt)

Open [`INSTALL-PROMPT.md`](INSTALL-PROMPT.md), paste it into your agent, and
follow it — it installs this skill **plus** two companion skills
([watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover),
[humanizer](https://github.com/blader/humanizer)), finishes setup for your OS
(Windows/macOS/Linux — no installer scripts, the agent adapts), and prints
the usage guide.

Manual route: `npx skills add justargldude/research_tool_skill --skill research-tool-skill`
(pin your agent with `-a antigravity` / `-a claude-code`), then follow
[`skills/research-tool-skill/SKILL.md`](skills/research-tool-skill/SKILL.md).

## Using it

- Just tell your agent: *"research tools to <your goal>"* — the agent runs
  the pipeline and plays its LLM (the runner pauses with JSON requests, the
  agent answers them, the run resumes from checkpoint).
- Update later by saying: **"update research-tool-skill"** — the agent pulls
  the newest commit, re-runs the offline smoke test, and reports the changelog.

## Layout

```
skills/research-tool-skill/   # the published skill (SKILL.md + USAGE.md + LLM contracts)
.agent/skills/…               # same skill, workspace layout for this repository
scout/                        # pipeline package (deterministic machinery)
run_pipeline.py               # offline demo (no network) — also the post-install smoke test
run_research.py               # standalone runner (gateway LLM mode)
schemas/ · examples/ · tests/ # contracts generated from pydantic + QA suite (181 tests)
docs/internal/                # process notes, spec, audit history (archived)
tools/                        # one-off maintenance utilities
```

## Development

```bash
pip install -r requirements.txt
python -m pytest tests/ -q        # 181 tests
python run_pipeline.py --out out/demo   # offline demo, deterministic
```

Scoring thresholds and bands are spec starting points, not calibrated values;
treat bands as ordinal hints. Product decisions and architecture review live
in `docs/internal/first-test/` (Vietnamese).
