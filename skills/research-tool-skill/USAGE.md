════════════════════════════════════════════════════════════════
  RESEARCH TOOL SKILL — cài đặt xong ✅
════════════════════════════════════════════════════════════════

Pipeline nghiên cứu công cụ phần mềm: phát hiện → gom cụm → chấm điểm
bằng chứng → audit năng lực → báo cáo. Nguồn miễn phí (GitHub, Hacker
News, OpenAlex) — KHÔNG cần API key.  Vị trí cài: ~/.research-tool-skill

── CÁCH DÙNG NHANH NHẤT ────────────────────────────────────────
Chỉ cần NÓI với agent (Antigravity, Claude Code, …):

  "research tools để <mục tiêu của bạn>"

Agent sẽ tự chạy pipeline và đóng vai LLM: khi pipeline dừng với yêu
cầu JSON (exit code 2), agent tự đọc, tự trả lời và tự chạy tiếp.

Chạy trực tiếp (tương đương — dùng đúng python đã cài ở bước setup):

  Windows:     %USERPROFILE%\.research-tool-skill\.venv\Scripts\python.exe %USERPROFILE%\.research-tool-skill\.agent\skills\research-tool-skill\scripts\run_skill_pipeline.py --topic "..." --workdir out\skill-run
  Linux/macOS: ~/.research-tool-skill/.venv/bin/python ~/.research-tool-skill/.agent/skills/research-tool-skill/scripts/run_skill_pipeline.py --topic "..." --workdir out/skill-run

Demo offline — không cần mạng, kiểm tra cài đặt (chọn python như trên):

  <python> ~/.research-tool-skill/run_pipeline.py --out ~/.research-tool-skill/out/demo

── CẬP NHẬT PIPELINE (dễ nhớ) ──────────────────────────────────
Lâu lâu chỉ cần nói:   "update research-tool-skill"

→ agent tự chạy: git fetch/pull thư mục cài đặt, chạy lại smoke test
(offline), báo changelog, in lại hướng dẫn này (quy trình đầy đủ nằm
trong SKILL.md mục Update — không cần script, chạy được trên mọi OS).
Không tự update giữa chừng một run đang dở — kết quả phải tái lập được.

── FLAG THƯỜNG DỤNG (scout = run_skill_pipeline.py) ────────────
  --topic (bắt buộc)   câu hỏi nghiên cứu, tiếng Việt được
  --workdir DIR        thư mục artifact (mặc định out/skill-run)
  --seed-queries N     số seed query Stage [0] dùng (mặc định 3)
  --max-repos N        số repo được REST enrichment (mặc định 4)
  --max-issues N       số issue/repo fetch (mặc định 5)
  --llm agent|gateway  agent = agent trả lời LLM steps (khuyên dùng)
                       gateway = tự gọi API OpenAI-compatible

── CÁC BƯỚC LLM KHI CHẠY THẬT ──────────────────────────────────
exit code 2 → đọc <workdir>/llm-requests/*.json, trả lời đúng contract
(skills/research-tool-skill/references/llm-contracts.md), ghi vào
<workdir>/llm-responses/<cùng-tên>.json, chạy lại cùng lệnh.
Pipeline resume từ checkpoint — không chạy lại stage cũ.

── ARTIFACTS (trong --workdir) ─────────────────────────────────
  report.md          sản phẩm chính — báo cáo xếp hạng + bằng chứng
  entities.json      entity, band, điểm, capability
  findings.json      bằng chứng verbatim (quote + claim)
  eval.json          recall/đánh giá chất lượng run
  coverage_log.json  bộ đếm từng stage (calls, tokens)
  snapshot_plan.json kế hoạch recrawl
  llm-requests/ · llm-responses/   toàn bộ transcript LLM

── HẠN CHẾ ĐÃ BIẾT ─────────────────────────────────────────────
Chấm điểm là heuristic (token-cosine, chưa có embedding model); API
unauth bị giới hạn (run thật dài vài phút do nhả nhịp có chủ đích);
band là gợi ý thứ bậc, không phải điểm tuyệt đối.
════════════════════════════════════════════════════════════════
