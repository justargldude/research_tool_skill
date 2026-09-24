# INSTALL-PROMPT — dán 1 prompt cài đủ 3 skill

Dán nguyên khối dưới đây vào agent (Antigravity, Claude Code, Cursor, Codex, …):

---

Install the following 3 agent skills, finish their setup, then show me the result.

**1) Scout Research** — research pipeline that finds, clusters, ranks and audits software tools for any topic. You are its LLM: it pauses with JSON requests, you answer them and it resumes.
- If you're in Antigravity, run `npx skills add justargldude/research_tool_skill -a antigravity`. If you're in Claude Code, run `npx skills add justargldude/research_tool_skill -a claude-code`. In any other agent, run `npx skills add justargldude/research_tool_skill --skill research-tool-skill` and select your agent.
- Then finish setup by following the **Install procedure in its SKILL.md** — it is script-free and cross-platform (Windows/macOS/Linux): check and install **Python ≥ 3.10 and git if missing** (winget/choco/brew/apt/dnf/pacman — assume nothing is pre-installed), clone the pipeline to the user's home `.research-tool-skill` dir, set up the only dependency (pydantic) via venv or `pip --user`, run the offline smoke test, and **PRINT the usage guide (`USAGE.md`) in full**. Report what you installed, at which paths.
- Later, when I say **"update research-tool-skill"**, follow the Update section of its SKILL.md (git fetch/pull + smoke test + changelog + reprint usage guide).

**2) Watermarks Remover** — strips AI watermarks and hidden metadata from content you own.
- Install: `npx skills add guillaumemeyer/watermarks-remover --skill remove-ai-marks` (add `-a antigravity` or `-a claude-code` to pin your agent).
- Setup: clone `https://github.com/guillaumemeyer/watermarks-remover` to `~/.watermarks-remover` and start its local service in the background (`cd ~/.watermarks-remover && make serve`, port 8765, Python 3.10+ stdlib only).

**3) Humanizer** — rewrites AI-sounding text into natural human prose.
- Install: `npx skills add blader/humanizer --global` (add `-a antigravity` or `-a claude-code` to pin your agent; drop `--global` for project-only).

Use one installation method consistently. If `npx skills` is unavailable in your environment, fall back to cloning each repo and copying its skill folder into your agent's skills directory (Antigravity global: `~/.agents/skills/`; Antigravity project: `.agent/skills/`; Claude Code: `~/.claude/skills/`). After installing all three, read each SKILL.md, verify each setup actually ran (show me the paths/versions), and finally print the Scout usage guide in full.

---

## Ghi chú

- Repo của skill 1: <https://github.com/justargldude/research_tool_skill> (nhánh `main`).
- Prompt này học theo mẫu cài skill kiểu TypeSafe: một phương thức cài duy nhất (`npx skills add <owner>/<repo> [--skill <name>] [-a <agent>]`), có fallback clone thủ công, và **bắt buộc in hướng dẫn sử dụng** sau setup (yêu cầu riêng của skill 1 — nằm cứng trong `skills/research-tool-skill/SKILL.md`, mục "MANDATORY on first activation").
- Cập nhật pipeline về sau: chỉ cần nói **"update research-tool-skill"** — agent chạy `update.sh` (git pull + smoke test + in lại hướng dẫn). Không auto-update giữa chừng một run đang dở để giữ tính tái lập của kết quả.
- 2 skill còn lại là repo bên thứ ba (`guillaumemeyer/watermarks-remover`, `blader/humanizer`) — cài đúng cách gốc của từng repo (npx skills / clone + service).
