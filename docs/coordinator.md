# Coordinator／Worker 協作（herdr + git worktree）

Claude 當 Coordinator，Codex 當 Worker。每個 Worker 在自己的 git worktree 工作，彼此不踩檔案。
只在 herdr 的 pane 內可用（`test "$HERDR_ENV" = 1`）。指令語法以 `herdr --skill` 與各子指令 `--help` 為準。

## 角色分工

- **Coordinator（Claude）**：拆任務、開 worktree、派工、審 diff、決定合併。自己不改程式碼。
- **Worker（Codex）**：只在自己的 worktree 內改碼，跑 `uv run ruff check . && uv run pytest -q`，不 commit、不 push。

## 流程

```bash
# 1. 開 worktree：從回傳 JSON 讀 worktree 路徑與 root pane id，不要猜
herdr worktree create --branch feat/<名稱> --base main --label worker-<名稱> --no-focus

# 2. 在該 worktree 的 root pane 啟動 Codex（pane 必須停在 shell prompt）
herdr agent start worker-<名稱> --kind codex --pane <root_pane_id>

# 3. 派工並等結果（--timeout 單位為毫秒；idle／done／blocked 才會回來）
herdr agent prompt worker-<名稱> "<任務>" --wait --timeout 1800000

# 4. 驗收
herdr agent read worker-<名稱> --source recent-unwrapped --lines 120
git -C <worktree 路徑> diff main...

# 5. 合併與清理（合併前先讓使用者確認）
git merge feat/<名稱>
herdr worktree remove --workspace <workspace_id>
```

## 規則

1. 子任務之間不得改同一批檔案；有重疊就改成序列執行。
2. Worker 狀態為 `blocked`（核准視窗）時，Coordinator 停下來回報使用者，不替 Worker 按核准。
3. `agent prompt --wait` 回 `timeout` 或 `agent_prompt_stalled` 時，先 `agent get`／`agent read` 看狀態，不要盲目重送。
4. Worker 測試失敗最多修 3 次，仍失敗就回報，不要放寬測試。
5. **commit 與合併前必須讓使用者確認內容**（AGENTS.md 工作慣例）；Worker 不 commit，由 Coordinator 在使用者同意後處理。
6. 不要刪 `work/`、模型檔或 docker 容器（硬規則 5）；`herdr worktree remove` 只用在 Coordinator 自己開的 worktree。

## 本專案的注意事項

- 新 worktree 沒有 `.venv`，派工 prompt 要先寫「先跑 `uv sync`」。
- `work/`、`photo/` 在 `.gitignore`，worktree 裡不存在；不要為了測試去複製。
- Worker 只跑 ruff 與 pytest，不跑真實管線或 GPU 階段（硬規則 4、7）。
- 授權、金鑰、上傳相關的硬規則（AGENTS.md「硬規則」）對 Worker 同樣有效，派工 prompt 開頭要要求先讀 `AGENTS.md`。

## 啟動 Coordinator 的 prompt 範本

```
你是 Coordinator，用 herdr 管理 Worker，自己不改程式碼。先讀 docs/coordinator.md 與 AGENTS.md，
再跑 `herdr --skill`。依文件流程：herdr worktree create → herdr agent start --kind codex →
herdr agent prompt --wait → 審 diff。Worker 不得 commit／push；blocked 時停下來問我；
commit 與合併前先給我看內容。
```
