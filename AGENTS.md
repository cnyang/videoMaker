# AGENTS.md — videoMaker 專案知識

給 Codex、Copilot、Claude Code 等代理（也給新接手的人）。只寫「讀程式看不出來」的事；程式本身看得出來的不重複。
操作手冊（產片、審核、cron、YouTube token、排錯）在 [`docs/operations.md`](docs/operations.md)，模型授權在 [`LICENSES.md`](LICENSES.md)。

**一句話**：主題 → LLM 企劃 → ACE-Step 音樂 + 拍點 → FLUX 關鍵幀 → Wan 圖生影片 → ffmpeg 合成 → QC → 人工核准 → 私人上傳 YouTube。
每週排程：週一兒童音樂、週二小知識、週三兒童英文對話、週四動物短劇、週五有對白的動畫（後三者用 MiniMax H3 renderer 產生帶對白音訊的影片，2026-10-22 使用者放行，見硬規則 1）；對白類每晚產 2 支，生成後把這 2 支合併；其他種類每晚生成後接著合併同一種內容。

## 硬規則（違反前必須先問使用者）

1. **只用 `LICENSES.md` 白名單內的模型**（ACE-Step 1.5、FLUX.1 schnell、FLUX.3 Image Klein＋prompt upsampler〔僅兒童音樂關鍵幀；Kommunity License，年營收須低於 US$5M〕、Wan 2.2 TI2V-5B、Qwen-Image-Edit-2509、clef-flash:9b、MiniMax H3〔僅對白類，2026-10-22 放行；授權排除美、歐盟、英、南韓〕）。換模型／LoRA／節點前先查授權頁，並同步更新 `LICENSES.md`、`models.txt`。禁用清單見 `LICENSES.md`。
   **MiniMax H3 已於 2026-10-22 由使用者放行**，只限對白類（`renderers.py` 的 `h3_fl2va`／`h3_ref2va`）；授權排除美國、歐盟、英國、南韓（`LICENSES.md`），公開到 YouTube 的風險由使用者決定。權重可從 NAS 或 HF `Comfy-Org/MiniMax-H3` 取得，但必須與 HF 的 sha256 相同（`scripts/nas_pull_h3.sh`、`scripts/download.sh` 都會驗）；Turbo LoRA／ControlNet／embedding 未放行。不要讓兒童音樂／小知識改用 H3。`VMK_H3_ENABLED=0` 可一鍵關閉。
2. **金鑰與 token 不進專案、不進 git、不印到日誌**（`~/.config/videomaker/{client_secret,token,facebook}.json`）。hooks 也會擋 `.env`、`secrets.*`，不要繞過。
3. **上傳一律 `private`，且必須人工核准**（`status.json` 為 `approved`）才能上傳。不要加自動核准、自動公開。token 範圍只有 `youtube.upload`，無法改已上傳影片。
   **Facebook Reels（2026-11-02 使用者選定）**：只限粉絲專頁，人工核准後用 `upload-facebook` 上傳 `DRAFT` 草稿，不公開、不排程發布；沿用核准 hash 與 `.upload.lock`，狀態另記 `meta.facebook_reel`，憑證只存專案外 `facebook.json`。不得將 YouTube 的來源核准例外擴大到 FB。
   **唯一例外（2026-10-28 使用者放行）**：每週精選合輯，若 `meta.sources` 的來源**全部**已人工核准、且合輯自己是 `pending_review`，TUI 按 `u` 可直接上傳（GX10 端 `approve --from-sources` 重驗後核准，`status.json` 記 `approved_basis: sources`，仍綁 `approved_sha256`）。來源缺少／未核准、合輯 QC 失敗或被否決一律回到人工審核；不要擴大到其他情況。
4. **不要降低 `VMK_MIN_FREE_GB` 硬跑**；記憶體不足就該中止。不要在夜間排程時段（週一到週五 19:30 起）手動跑 GPU 工作。
5. **不要刪 GX10 上的 `work/`、模型檔、docker 容器**。作業名稱一律過 `jobs.valid()`（只接受 `<日期>_<小寫英數與連字號 slug>`，見 `jobs.JOB_RE`），不可放寬——它擋住路徑穿越與 shell 注入，下載、遠端呼叫與刪除都靠它。
6. **各階段「輸出已存在就跳過」是可續跑的根基**，改階段邏輯不可破壞（夜間重試與 `--resume` 都依賴它）。
7. **不要為了驗證去跑真實管線**：一支短片約 35 分鐘、長片 1.5 小時、xl 3 小時，且占用共用 GPU。驗證用 pytest 與 `--dry-run`。

## 環境

| 機器 | 角色 | 注意 |
|---|---|---|
| Mac（開發機） | 開發、TUI、YouTube 授權、token LaunchAgent | 用 `uv`；專案在 `/Users/cnyang/code/temp/videoMaker` |
| GX10（`ssh gx10`，10.200.200.200） | ComfyUI、Ollama、所有 GPU 生成、cron、實際上傳 | **用 pip 的 venv（不是 uv）、沒有 git、沒有 Textual**；新增依賴要手動 `pip install`；專案在 `/home/ehomeservice/code/videoMaker` |

- 代理多半只在 Mac 上，且可能連不到 GX10：能做改碼、pytest、ruff；不能做 GPU 階段、`make`（非 dry-run）、`nightly`。`--dry-run` 仍需連得到 LLM（`VMK_LLM_URL` 預設指向 GX10）。
- `videoMaker` 是 Mac `~/.zshrc` 的 shell 函式，**不在 repo、代理的 shell 沒有它**；文件中的 `videoMaker xxx` = 在 GX10 執行 `cd ~/code/videoMaker && .venv/bin/python -m videomaker xxx`。
- 所有設定走環境變數（`videomaker/config.py`，清單在 operations.md#環境變數）；在 GX10 手動跑要帶 `VMK_COMFY_URL=http://127.0.0.1:8188 VMK_LLM_URL=http://127.0.0.1:11434/v1`。

## 指令

```bash
uv sync                                                                    # 安裝（Python 3.12）
uv run ruff check . && uv run ruff format --check . && uv run pytest -q   # 基準（2026-11-02）：ruff 乾淨、495 passed（約 76 秒；ComfyUI 連得到時 live 測試不再 skip）
uv run python -m videomaker --help
uv run python -m videomaker make "主題" --dry-run                           # 只產 plan.json
```

部署到 GX10（rsync，不帶 venv／work／照片）與 GX10 端驗證指令見 operations.md#部署與開發。
工具鏈只有 **ruff（lint + format，line-length 110）+ pytest**；沒有 mypy／black／isort 設定，不要順手加。

## 模組地圖

各模組職責見 `videomaker/*.py` 檔頭 docstring；測試依主題分檔，見 `tests/`。

## 作業資料夾 = 狀態機

作業的狀態**完全由 `work/<日期>_<slug>/` 內的檔案決定**，沒有資料庫：

```
plan.json + meta.json 存在      → 作業存在
無 status.json                  → 未完成／進行中（30 分鐘內有檔案更新視為進行中，jobs.JOB_ACTIVE_SECONDS；單支 Wan 片段要 15–20 分鐘才寫檔）
status.json.status              → pending_review → approved | rejected；QC 沒過是 failed
meta.json.youtube_id            → 已上傳（upload 靠它做冪等）
meta.json.images                → 照片模式
work/history.jsonl              → 夜間排程每支的結果（另有 type=merge 的合併紀錄）；weekly 靠它找近期作業
```

重做某階段：刪該階段輸出再 `--resume`。`mux` 會刪掉 `status.json`，所以重合成後 QC 一定重跑。
`meta.json` 是授權證據（模型檔名、seed、完整提示詞、BPM、切點、耗時、git hash），別省略欄位。

## 跨區陷阱（每次改動都可能踩到）

- 核准綁 `status.json.approved_sha256`，TUI 的 `o` 先以 checksum 同步，記住開啟版本後才可核准；CLI 可傳 `approve --sha256`。上傳必須重驗 hash，舊核准無 hash 要人工重新核准，不可自動補值。`.upload.lock` 是跨行程鎖，不能刪掉重建。
- **部分子指令的 stdout 是機器介面**：`jobs-json`（TUI 用 `json.loads`）、`token-status`（`remote_status` 取最後一行 `ok／expired／missing`）、`upload`／`upload-facebook`（印影片 ID）。這些指令不要多印東西；人看的訊息走 `logging`（stderr）。
- `tui` 是**延遲載入**（`__main__` 內 `from .tui import run`），因為 GX10 沒裝 Textual，夜間 cron 不能因此壞掉。其他頂層 import 也不要引入 GX10 沒有的套件。
- TUI 在 GX10 上執行子指令（核准、不通過、上傳）一律經 `jobs.remote_cli`，它有**參數白名單**（`approve／reject／upload／upload-approved／upload-facebook`、`-` 開頭選項、合法作業名）。新增 TUI 會遠端呼叫的子指令要加進去。
- `pipeline.run` 的呼叫簽名不能再加參數：夜間測試用 `fake_run(topic, fmt, resume, kind)` 限定參數。

## 詳細設計筆記（按需讀取）

動到下列區域前，先讀 [`docs/dev-notes.md`](docs/dev-notes.md) 對應段落：企劃、音樂與拍點、關鍵幀、影片（Wan）、小知識、對白類、視覺 QC、CLI 與遠端介面、夜間排程。同檔還有「修改指南」（新片型／新子指令等該改哪裡）與「已知問題與待辦」。

## 測試

- 全是不需要 GPU／ComfyUI 的單元測試（以 fake／monkeypatch 取代 ComfyUI、LLM、YouTube、ssh）；唯一的連線測試 `test_workflows_match_live_object_info` 連不上會 skip。TUI 用 Textual pilot。
- 新測試放進對應的既有檔案；`test_pipeline.py` 目前涵蓋範圍很廣。
- **測試失敗時修實作，不要改測試**（除非測試本身有錯）。偏好 TDD；覆蓋率目標 80%。同一個失敗最多修 3 次，仍失敗就停下來問使用者。

## 工作慣例（使用者偏好）

- 程式碼的關鍵邏輯註解與文件用**繁體中文（台灣用語）**，沿用現有風格；技術名詞與識別字維持英文。
- 改完程式要跑 ruff + pytest，宣告完成前**貼出實際輸出**；跳過任何步驟要明說。
- **commit 前先讓使用者確認內容**。格式 `<type>: <description>`（feat／fix／refactor／docs／test／chore／perf／ci）；不加 `Co-Authored-By` 等 attribution 行。
- 專案已有初始 commit（2026-10-16，35 個檔案一次收進）。`photo/`（私人照片）與 `.DS_Store` 已在 `.gitignore`，不要 `git add -f`。
