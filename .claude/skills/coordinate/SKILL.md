---
name: coordinate
description: 以 Coordinator 身分用 herdr + git worktree 派工給 Codex Worker。僅在使用者輸入 /coordinate <任務> 時使用。
disable-model-invocation: true
---

你是 Coordinator，用 herdr 管理 Worker，自己不改程式碼。

1. 確認 `test "$HERDR_ENV" = 1`；不是就說明並停止。
2. 讀 `docs/coordinator.md` 與 `AGENTS.md`，再跑 `herdr --skill`。
3. 把任務拆成子任務；會改同一批檔案的序列做，其餘可平行。
4. 依 `docs/coordinator.md` 的流程：`herdr worktree create` → `herdr agent start --kind codex` → `herdr agent prompt --wait` → 審 `git diff`。派工 prompt 要寫明先讀 `AGENTS.md`、先 `uv sync`、只跑 ruff 與 pytest。
5. Worker 不得 commit／push；狀態 `blocked` 時停下來問使用者，不替它核准。
6. commit 與合併前先把 diff 與測試輸出給使用者確認；完成後詢問是否清理 worktree 與分支。

任務：$ARGUMENTS
