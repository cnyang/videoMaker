# videoMaker — 兒童音樂 Shorts 自動生成管線

輸入一個主題，自動產出直式 9:16、純音樂、無旁白的兒童音樂短片（短片約 20 秒、長片約 2 分 48 秒、xl 約 5 分鐘），存在 GX10，經人工核准後以「私人」上傳到 YouTube，最後由你在 YouTube Studio 手動公開。

也支援人工核准後上傳 **Facebook 粉絲專頁 Reels 草稿**（TUI 按 `f`，或 CLI `upload-facebook`），再到 Meta 手動發布；首次設定與短片限制見[操作手冊](docs/operations.md#facebook-auth--upload-facebook--facebook-reels粉絲專頁草稿)。

```
主題
 └─ [plan] LLM 企劃 plan.json（雙語 YouTube 標題／說明／標籤）
     └─ [music] ACE-Step 1.5 生成音樂 → librosa 偵測拍點，決定每鏡切點
         └─ [keyframes] FLUX.1 schnell 關鍵幀（或用你的照片，兩隻狗用 Qwen-Image-Edit 合成同框）
             └─ [clips] Wan 2.2 TI2V-5B 圖生影片（704×1280、24fps、約 5 秒）
                 └─ [mux] ffmpeg 合成 final.mp4（1080×1920）
                     └─ [qc] 自動檢查 → pending_review
                                  ↓
          你看片 → approve → upload（私人）→ 你在 Studio 手動公開
```

## 文件地圖

| 你想做什麼 | 看哪份 |
|---|---|
| 改程式／讓 AI 代理（Codex、Copilot、Claude Code）接手 | [`AGENTS.md`](AGENTS.md)：硬規則、架構、設計決定與陷阱、修改指南 |
| 產片、審核、上傳、維運、排錯 | [`docs/operations.md`](docs/operations.md)：每天流程、TUI、指令參考、夜間排程、YouTube token、疑難排解 |
| 查模型授權、確認能不能換模型 | [`LICENSES.md`](LICENSES.md) |
| 了解當初的設計動機（歷史文件，部分已過時） | [`kids-shorts-pipeline-spec.md`](kids-shorts-pipeline-spec.md) |

## 兩台機器

| 機器 | 角色 |
|---|---|
| Mac（本機） | 開發、TUI（`videoMaker`）、YouTube 授權（要瀏覽器）、每日 token 檢查（LaunchAgent） |
| GX10（`ssh gx10`） | ComfyUI（docker）、Ollama（LLM）、所有 GPU 生成、夜間排程、實際上傳 YouTube |

成片只存在 GX10 與你下載到 Mac 的那份，**沒有 NAS 備份**。路徑、環境變數與 cron 細節見 [`docs/operations.md`](docs/operations.md#機器與路徑)。

## 快速開始（開發）

專案固定 Python 3.12，用 [uv](https://docs.astral.sh/uv/) 管理。

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest -q   # 目前：ruff 乾淨、226 passed
uv run python -m videomaker --help                                         # 所有子指令
uv run python -m videomaker make "小熊刷牙" --dry-run                       # 只產 plan.json（需連得到 LLM，預設是 GX10 的 Ollama）
uv run python -m videomaker tui                                            # 互動式介面（Mac；需能 ssh gx10）
```

真正產片（`make`、`nightly`）要 GPU，在 GX10 上跑，見 [操作手冊](docs/operations.md#指令參考)；部署到 GX10 的 `rsync` 指令見 [部署與開發](docs/operations.md#部署與開發)。

## 專案結構

```
videomaker/        Python 套件（pipeline、plan、content、dialogue、renderers、characters、comfy、audio、youtube、nightly、weekly、jobs、tui）
workflows/         ComfyUI API 格式 workflow（flux、ace_step、wan22、qwen_compose）
workflow_map.yaml  要替換的 workflow 節點欄位
tests/             pytest（不需要 GPU／ComfyUI）
scripts/           download.sh（限速下載模型）、token 檢查 LaunchAgent plist
models.txt         白名單模型下載清單
work/              作業資料夾（不進 git；GX10 才是正本）
photo/             照片模式用的本機照片（不進 git）
```

## 重要原則

- **只用 [`LICENSES.md`](LICENSES.md) 白名單內的模型**；要用別的先查授權並問過使用者。
- **金鑰（`client_secret.json`、`token.json`）絕不進專案或 git。**
- **必須人工核准才能上傳；YouTube 一律私人、Facebook 一律草稿**；QC 用 clef-flash 抽查兒童內容與畫面變形，TUI 顯示結果；抽樣仍可能漏判，必須人工看片。
