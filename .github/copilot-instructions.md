# Copilot 指引 — videoMaker

完整專案知識在 [`AGENTS.md`](../AGENTS.md)（硬規則、架構、設計決定與陷阱、修改指南），**動手前先讀它**。操作手冊在 [`docs/operations.md`](../docs/operations.md)。

專案：主題 → LLM 企劃 → ACE-Step 音樂 → FLUX 關鍵幀 → Wan 圖生影片 → ffmpeg → QC → 人工核准 → 私人上傳 YouTube。Python 3.12、uv、pydantic、Textual（只在 Mac）、ComfyUI。

## 一定要遵守

- 只用 `LICENSES.md` 白名單內的模型；換模型前先問。
- 金鑰／token 不進 git、不印出。上傳一律 `private` 且必須人工核准。
- 作業名稱一律過 `jobs.valid()`；不要放寬，也不要把使用者輸入直接拼進 ssh／shell 指令。
- 各階段「輸出已存在就跳過」不可破壞（可續跑）。
- `jobs-json`、`token-status`、`upload` 的 stdout 是機器介面，不要多印東西；訊息走 `logging`。
- 不要為了驗證去跑真實管線（要 GPU、數十分鐘到數小時）；用 pytest 與 `make --dry-run`。

## 指令

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest -q   # 基準：ruff 乾淨、226 passed
```

## 風格

- 沿用現有程式風格；關鍵邏輯註解用繁體中文（台灣用語）。工具鏈只有 ruff（line-length 110）+ pytest。
- 測試失敗修實作，不改測試（除非測試本身有錯）。新行為先寫測試。
- commit 前先讓使用者確認；格式 `<type>: <description>`，不加 `Co-Authored-By` 等 attribution。
