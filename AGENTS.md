# AGENTS.md — videoMaker 專案知識

給 Codex、Copilot、Claude Code 等代理（也給新接手的人）。只寫「讀程式看不出來」的事；程式本身看得出來的不重複。
操作手冊（產片、審核、cron、YouTube token、排錯）在 [`docs/operations.md`](docs/operations.md)，模型授權在 [`LICENSES.md`](LICENSES.md)。

**一句話**：主題 → LLM 企劃 → ACE-Step 音樂 + 拍點 → FLUX 關鍵幀 → Wan 圖生影片 → ffmpeg 合成 → QC → 人工核准 → 私人上傳 YouTube。
每週排程：週一兒童音樂、週二小知識、週三兒童英文對話、週四動物短劇、週五有對白的動畫（後三者用 MiniMax H3 renderer 產生帶對白音訊的影片，2026-10-22 使用者放行，見硬規則 1）；對白類每晚產 2 支，生成後把這 2 支合併；其他種類每晚生成後接著合併同一種內容。

## 硬規則（違反前必須先問使用者）

1. **只用 `LICENSES.md` 白名單內的模型**（ACE-Step 1.5、FLUX.1 schnell、FLUX.3 Image Klein＋prompt upsampler〔僅兒童音樂關鍵幀；Kommunity License，年營收須低於 US$5M〕、Wan 2.2 TI2V-5B、Qwen-Image-Edit-2509、clef-flash:9b、MiniMax H3〔僅對白類，2026-10-22 放行；授權排除美、歐盟、英、南韓〕）。換模型／LoRA／節點前先查授權頁，並同步更新 `LICENSES.md`、`models.txt`。禁用清單見 `LICENSES.md`。
   **MiniMax H3 已於 2026-10-22 由使用者放行**，只限對白類（`renderers.py` 的 `h3_fl2va`／`h3_ref2va`）；授權排除美國、歐盟、英國、南韓（`LICENSES.md`），公開到 YouTube 的風險由使用者決定。權重可從 NAS 或 HF `Comfy-Org/MiniMax-H3` 取得，但必須與 HF 的 sha256 相同（`scripts/nas_pull_h3.sh`、`scripts/download.sh` 都會驗）；Turbo LoRA／ControlNet／embedding 未放行。不要讓兒童音樂／小知識改用 H3。`VMK_H3_ENABLED=0` 可一鍵關閉。
2. **金鑰與 token 不進專案、不進 git、不印到日誌**（`~/.config/videomaker/{client_secret,token}.json`）。hooks 也會擋 `.env`、`secrets.*`，不要繞過。
3. **上傳一律 `private`，且必須人工核准**（`status.json` 為 `approved`）才能上傳。不要加自動核准、自動公開。token 範圍只有 `youtube.upload`，無法改已上傳影片。
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
uv run ruff check . && uv run ruff format --check . && uv run pytest -q   # 基準（2026-10-22）：ruff 乾淨、435 passed（約 58 秒；ComfyUI 連得到時 live 測試不再 skip）
uv run python -m videomaker --help
uv run python -m videomaker make "主題" --dry-run                           # 只產 plan.json
```

部署到 GX10（rsync，不帶 venv／work／照片）與 GX10 端驗證指令見 operations.md#部署與開發。
工具鏈只有 **ruff（lint + format，line-length 110）+ pytest**；沒有 mypy／black／isort 設定，不要順手加。

## 模組地圖

```
videomaker/
  __main__.py   argparse CLI 入口；所有子指令在此分派（--format 的 choices 是寫死的）
  config.py     環境變數 → 常數（ROOT、COMFY_URL、LLM_URL、LLM_MODEL、WORK、MIN_FREE_GB、STEPS、COOL_TEMP、DEFAULT_BPM）
  plan.py       Plan／Shot／Music（pydantic）、企劃內容規則（regex）、FORMATS、shot_math、build_layout、make_plan（呼叫 LLM，驗證失敗最多重寫 3 次）
  pipeline.py   Job、new_job、各階段 music／keyframes／clips／mux／qc、slot_windows（mux 與視覺 QC 共用的取用區段）、照片模式（_use_photos）、run、bench；對白類在 new_job／run／_qc 分派到下面三個模組
  content.py    對白類（kids_english_dialogue／animal_sitcom／dialogue_animation）：結構化腳本 schema 與驗證、LLM 腳本企劃（重寫 3 次）、SPECS（鏡數／總長／預設 renderer）、qc_range、to_plan
  characters.py 固定角色庫（characters/<id>/character.json＋參考圖）：load／check_asset／build；ID 樣式擋路徑穿越
  renderers.py  H3 renderer 封裝（workflow 填值、提示詞、幀數換算、參考圖上傳、啟用閘門 enabled／require）；wan 仍在 pipeline.clips
  dialogue.py   對白類的階段實作（無音樂階段、keyframes 沿用 pipeline、clips 走 renderer、mux 串接＋字幕＋loudnorm）與對白 QC（checks）
  comfy.py      ComfyUI client（/prompt→輪詢 /history→/view）、fill()（依 workflow_map.yaml 填值）、記憶體檢查、models_in()、missing_models()
  audio.py      librosa 拍點偵測、切點、frame_bounds
  wiki.py       英文維基百科抓取（小知識的唯一來源）：fetch() 回傳 Article（標題、永久連結、純文字）
  labeling.py   視覺 QC 校準：抽出待人工標記的畫面（盲標頁面 + manifest）；CLI `visual-labels`
  visual_qc.py  clef-flash 視覺抽樣（2fps，只查成片實際用到的區段）；逐張原子保存、素材／區段／模型 digest 失效檢查、合輯結果彙整
  nightly.py    夜間排程：SCHEDULE（星期 → 內容種類）、tonight（生成後 merge_after）、LLM 想主題、預估耗時 vs deadline（H3 取 history 實際每秒成本）、GPU 降溫等待、失敗重試一次、寫 history.jsonl
  weekly.py     合併：用 ffmpeg -c copy 把近 6 天（同一週，**不是滑動視窗**）同內容、同解析度的成片串成一支（最多最新 4 支）：兒童音樂與小知識＝當晚 short＋long 串成一支（xl 不加入，`NO_MERGE_FORMATS`），對白類＝當晚 2 支 short；`merge_group(kind)` 決定哪些片型一組；merge_id 去重；merge_kind 回傳 history 紀錄
  youtube.py    OAuth、可續傳上傳（冪等）、核准閘門、listing、token 健康檢查與 renew_token
  jobs.py       作業盤點（GX10 端 inventory）＋ Mac 端 ssh／rsync 操作（下載、remote_cli、刪除）
  tui.py        Textual 介面（只在 Mac 跑）；只管畫面，遠端動作與名稱檢查都在 jobs.py
workflows/*.json     ComfyUI API 格式 workflow；workflow_map.yaml 記錄要替換的「節點 ID → 輸入名」
tests/               test_pipeline.py（plan／audio／comfy／pipeline／nightly／youtube 的大雜燴，含對白類的排程保護測試）、test_content.py（腳本 schema／角色庫／renderer）、test_dialogue.py（對白類階段與 QC）、test_tui.py、test_weekly.py（含每日合併）、test_labeling.py
characters/          固定角色庫（對白類共用，見 characters/README.md）
```

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

## 設計決定與陷阱

**企劃（plan.py）**
- 主題與分鏡預設共用模型（兒童音樂 `qwen3.8:27b`、小知識 `gemma4:31b`）以減少切換。`config.plan_model(kind)` 是企劃呼叫與 `meta.json.llm` 共用的選擇入口；`VMK_LLM_MODEL` 只有明確設定才覆寫企劃模型。
- 內容規則（純英文、禁文字類字眼、禁高風險字眼、道具顏色固定、禁現有角色／品牌、簡體字偵測、雙語 YouTube 欄位）是**為了降低 FLUX／Wan 畫壞的機率**，規則表與原因在 operations.md#企劃內容規則。改規則要三處同步：regex、`_SYSTEM` 提示詞、測試。
- **`style` 為空 = 照片模式**：`Plan._content_rules` 在 `style` 為空時整組跳過（提示詞來自使用者的照片描述）；`full_prompt()` 也靠它略過空欄位。
- BPM 由程式決定（預設 120，允許 100–140），LLM 只是附和；鏡頭數也由 `shot_math` 算好告訴 LLM。
- `Plan.seed` 由程式產生；每鏡 seed = 基底 + 鏡頭 id（關鍵幀黑邊重生再加 `1000 * attempt`）。

**音樂與拍點（pipeline.music、audio.py）**
- **ACE-Step 會提早演完**（要 22 秒只演約 16 秒，後面是靜音），所以多要 10 秒、重試每次再加 4 秒，並在 `audio.analyze` 驗證「有聲音的結尾 ≥ 最後一個切點」。別把「檔案夠長」當作「音樂夠長」。
- 切點是**偵測到的拍點**，不是理論網格；重拍用「前 4 拍的 RMS 能量最大者」的啟發式（程式內有 `ponytail:` 註解，錯一拍時切點仍在拍上）。實測 BPM 與設定差 >5% 就重生。
- `frame_bounds` 用累積四捨五入，每個切點誤差 ≤ 0.5 幀。

**關鍵幀（pipeline.keyframes）**
- 兒童音樂走 `workflow_map.yaml` 的 `keyframe_flux3`（FLUX.3 Image Klein，提示詞先經 upsampler 擴寫），小知識維持 `keyframe`（schnell）：2026-10-20 A/B（6 鏡）FLUX.3 構圖與場景明顯較好，但水獺會畫得像海豹，物種外觀對小知識風險較高，所以沒換。依 `plan.kind` 分流。
- **upsampler 不可拿掉**：官方說不用結果明顯較弱，實測無 upsampler 還會出現白色上下邊（現在有 `_has_white_bars` 擋，但別因此拿掉 upsampler）。擴寫後的 caption 從 history 的 `pup_text`（`PreviewAny`）取回，存進 `meta.json` 的 `shots[].upsampled`（授權證據）。
- ComfyUI 釘在 v0.41.3（FLUX.3 要 `CLIPLoader type=flux3`），升級與回滾見 operations.md#部署與開發。
- 2026-10-20 在 GX10 實測（獨立工作目錄，作業 `little-bear-bouncing-ball` 的 plan）：14 鏡關鍵幀 466 秒（≈33 秒/張，`nightly.KEYFRAME_SEC` 取 35）、0 次黑白邊重生、14/14 有 `upsampled`；用這些關鍵幀跑 2 鏡 Wan 各約 334 秒（與 schnell 版的 346 秒相當），抽幀對照 4.8 秒內角色與背景穩定，沒有比 schnell 版差（樣本只有 2 鏡，人工看片仍不可省）。
- 記憶體峰值（相對基線 52.8GB）：FLUX.3＋upsampler 37.4GB、Wan 29.6GB，所以關鍵幀階段多要 `pipeline.FLUX3_EXTRA_GB`（8GB）；`VMK_MIN_FREE_GB` 的基準沒有降低。
- 黑邊與白邊都會換 seed 重生：`_has_white_bars` 用 `negate`＋cropdetect，只認「橫跨整張寬、上下都有」的白帶，白色背景的圖不會誤判（19 張實測圖只標出該標的 1 張）。

**影片（Wan 2.2 TI2V-5B）**
- 解析度只能 **704×1280**，幀數必須是 **4n+1**（`LENGTH = 121`，24fps ≈ 5.04 秒），不補幀；`mux` 時才放大裁切到 1080×1920。
- 每個 GPU 階段開始前 `Comfy.prepare()` = **先 `/free` 再檢查記憶體**（順序不可反，否則被自己上一階段擋住）；階段結束再 `free()`。
- 長片／xl 重複使用鏡頭：判斷用 **`n_unique < n_slots`**，不要寫死片型名稱（`new_job` 有註解）；重複同一鏡頭時換起點幀（`REUSE_OFFSETS`）。
- 照片模式加的負向提示詞 `PHOTO_NEG` **含簡體字是刻意的**（Wan 官方負向提示詞就是簡體中文，workflow 裡的預設 `neg` 也是），不要被簡繁檢查「修正」——`SIMPLIFIED` 只套用在 YouTube 欄位與夜間主題。正向用 `only the dogs are in the scene` 而不寫否定句（Wan 對 "no hands" 反而容易畫出手）。
- 關鍵幀有黑邊或白邊（`_has_black_bars`、`_has_white_bars`）會換 seed 重生最多 3 次，3 次後**不會報錯**而是沿用最後一張。

**小知識（trivia，週二）**
- `Plan.kind`（`kids` 預設／`trivia`）、`facts`（字幕＋`quote`）、`sources`。**來源驗證在 `plan.check_facts`**：quote 必須逐字存在於抓到的條目、字幕數字必須出現在 quote、字幕的實詞必須出現在自己的 quote（`unsupported_words`，啟發式、會擋同義詞；調整虛詞表 `_STOP` 前先看 operations.md 的限制）。只在 `make_plan(article=...)` 套用，舊 plan.json 與合輯 plan 載入不受影響（`weekly` 的合輯 plan 是 `facts=[]`、`sources` 彙整）。
- 中文名稱來自 `wiki._zh_tw`（中文維基的 zh-tw 變體標題），`plan.check_zh_name` 強制標題與說明欄使用；`SIMPLIFIED` 手寫字表抓不完簡體，別拿它判斷維基標題。`wiki.fetch` 有 `lru_cache`（測試要 `cache_clear()`）。
- 來源由程式寫進 `plan.sources` 與 YouTube 說明欄，**不要改成採用 LLM 填的網址**；`plan.topic` 由程式覆寫成條目標題（`--resume` 靠 topic 找作業）。
- 英文字幕在 `pipeline.caption_filters`（ffmpeg `drawtext`，GX10 有、Mac 沒有）；字型走 `VMK_FONT`。`plan.FACTS_PER` 同時決定「支援哪些片型」（short、long，沒有 xl）與每支幾則，`plan.KINDS` 是每日合併與夜間排程共用的「內容種類 → 片型」表。
- 兒童音樂的呼叫路徑刻意沒變：`new_job` 只在小知識時才把 `article`／`n_facts` 傳給 `make_plan`，`nightly` 只在小知識時才把 `kind` 傳給 `pipeline.run`。`history.jsonl` 的 `kind` 缺省視為 `kids`（舊紀錄）。
- 上傳仍然一律私人、人工核准；`selfDeclaredMadeForKids: True` 只對兒童內容（`youtube.NOT_FOR_KIDS` 的動物短劇與有對白的動畫不宣告，公開前在 Studio 自選受眾）。

**對白類（content.py／dialogue.py／renderers.py／characters.py）**
- `Plan.script` 存通過驗證的結構化腳本；`Plan.music` 因此改成選填（`_music_required` 只對 kids／trivia 強制），舊 `plan.json` 完全相容。`Plan._content_rules`（禁 mouth／water 等字眼、道具顏色…）**只套用在 kids／trivia**：對白類角色要開口說話，這組規則為「無旁白音樂短片」訂的，不能套。
- 對白類的 `Plan.character` 是空字串、`Plan.style` 是共用畫風；**每鏡 `shot.prompt` 已含該鏡出現的角色描述**（多角色時不能每鏡都畫全員），`full_prompt` 才不會重複。
- 腳本由程式驗證（`content._Script._check` 與各子類 `hook_ok`）：角色 ID、單鏡 5–15 秒（H3 訓練範圍）、語速 ≤ 3 詞／秒、字幕＝對白、五段故事結構…；`duration` 由程式加總覆寫。改規則要三處同步：schema 驗證、`_COMMON／_PER_KIND` 提示詞、`tests/test_content.py`。真實 LLM 偶爾把 `reference_assets` 寫成 `dog_01_front`／`dog_01:front`，錯誤訊息要講清楚格式（要有斜線）。
- `renderer` 在 `new_job` 時寫進 `meta.json`，續跑不換；預設與環境變數覆寫在 `content.renderer_for`。`wan` 沒有音訊，`renderers.get("wan")` 會拒絕（對白類 QC 要求音軌）。
- 對白類沒有拍點，`Job.windows()` 靠 `timeline.json`（`dialogue.mux` 寫）決定視覺 QC 區段；QC 長度範圍用 `content.qc_range`（企劃範圍 −2／+5 秒，H3 幀數會往上取整到 17k+5 格）。`pipeline._qc` 用 `job.meta["kind"]` 判斷（兒童音樂的 meta 沒有 kind，預設 kids）；既有測試用的假 job 沒有 `plan`，不要改成讀 `job.plan.kind`。
- 字幕時間是依詞數分配的估計值（沒有 ASR）。QC 只驗 metadata：無聲區間、字幕時間軸在片長內、字幕＝腳本對白。
- 完全無聲的音訊 loudnorm 會量到 `-inf` 而失敗，`dialogue.mux` 改成 `anull` 讓 QC 明確判不通過（不是 mux 崩潰後重試）。
- `workflows/h3_*.json` 取自 ComfyUI 官方範本 `video_minimax_h3_i2v／r2v`（已通過 `/object_info` 結構檢查），**尚未實跑驗證**；實跑後的取樣器、步數、解析度、提示詞寫法與記憶體峰值（`VMK_H3_EXTRA_GB`）都要依實測校正。`workflow_map.yaml` 的 `gate: h3` 讓 `comfy.missing_models` 在 `VMK_H3_ENABLED=0` 時略過它們；權重還沒裝好時，`nightly` 靠 `renderers.missing_weights` 記 `skipped`（不想主題、不佔 GPU）。
- 角色參考圖是持久資產：`characters.build` 只補缺的圖，夜間排程絕不呼叫它。
- **動物短劇固定主角是 `content.LEADS`（`husky_01` Meatball、`husky_02` HappySunny，寫實哈士奇）**：`hook_ok` 要求 `characters` 剛好是這兩隻；`content.catalog(kind)` 只對動物短劇列出它們、其他對白類不列（寫實角色配卡通畫風會違和）；動物短劇的畫風規則在 `content.STYLES`（寫實），其他種類維持卡通。兩隻的參考圖是從使用者的角色設定圖手動裁出來的，**不要對它們跑 `characters build --force`**（`KEEP`／`VIEW_PROMPTS` 都寫死 flat 2D cartoon，會把寫實角色重畫成卡通）；要補視角請手動裁圖。個性與聲音描述是代為擬定的，使用者可隨時改 `character.json`。

**視覺 QC（visual_qc.py）**
- 抽幀用 `select` 與 `showinfo` 記錄真正的來源 PTS；不要再用 `number / FPS` 當原始時間。`SAMPLING` 進 fingerprint，標記工具只接受相同抽樣版本，舊報告要重查才能校準。
- 查的是 `mux` 實際放進成片的區段，不是整支原始鏡頭：區段由 `pipeline.slot_windows` 算（`mux` 同一份），改取用邏輯時兩邊會一起動，不要在 `visual_qc` 另寫一份。結果的 `time` 是素材內秒數、`final_time` 是成片時間。
- fingerprint 含素材 hash、取用區段、問題、模型 digest、抽樣設定；**不含 `THRESHOLD`**（只影響判定）。調門檻只依已存分數重算狀態，不重跑 GPU；`visual_qc.restate` 是唯一的重算入口（`check` 快取命中、`combine`、`jobs.visual_summary` 都用它，所以舊作業依新門檻顯示、但不改 `status.json`）。
- `check()` 取得本次 fingerprint 之前失敗（Ollama 離線等），要保留磁碟上既有結果、只標 `error`；`except` 是 `Exception`（刻意，否則檔案會永遠停在 `running`）。
- `combine()`（合輯）fail-closed：只有全部來源明確 `passed` 且門檻、抽樣率與現行相同才算通過。
- 基本 QC（解析度、長度、音軌、響度、黑畫面）失敗時不跑視覺檢查；`meta.json` 的 `models.visual_qc` 只在確實有畫面送進模型時才寫，附 digest。

**CLI 與遠端介面**
- 核准綁 `status.json.approved_sha256`，TUI 的 `o` 先以 checksum 同步，記住開啟版本後才可核准；CLI 可傳 `approve --sha256`。上傳必須重驗 hash，舊核准無 hash 要人工重新核准，不可自動補值。`.upload.lock` 是跨行程鎖，不能刪掉重建。
- **部分子指令的 stdout 是機器介面**：`jobs-json`（TUI 用 `json.loads`）、`token-status`（`remote_status` 取最後一行 `ok／expired／missing`）、`upload`（印影片 ID）。這些指令不要多印東西；人看的訊息走 `logging`（stderr）。
- `tui` 是**延遲載入**（`__main__` 內 `from .tui import run`），因為 GX10 沒裝 Textual，夜間 cron 不能因此壞掉。其他頂層 import 也不要引入 GX10 沒有的套件。
- TUI 在 GX10 上執行子指令（核准、不通過、上傳）一律經 `jobs.remote_cli`，它有**參數白名單**（`approve／reject／upload／upload-approved`、`-` 開頭選項、合法作業名）。新增 TUI 會遠端呼叫的子指令要加進去。
- 合併產生的作業名是 `<日期>_weekly-<片型>`（兒童音樂）、`<日期>_weekly-trivia-<片型>`（小知識）或 `<日期>_weekly-<kind 把底線換連字號>-<片型>`（對白類），同樣符合 `jobs.valid()`（kind 的底線必須換成連字號，`jobs.JOB_RE` 不收底線）；`plan.json` 沿用第一支的 Plan 只換主題與 YouTube 文案，所以走原本的上傳流程。串接用 `-c copy`，靠各支都由同一條 `mux` 產生（編碼參數一致）。

**夜間排程（nightly.py）**
- 預估耗時 = 獨特鏡頭數 × `SEC_PER_STEP`(17.3) × `STEPS` + 其他階段，×1.15；「現在 + 預估」超過 deadline 就略過該支，不硬跑。每支（含重試）開跑前 `wait_cool`。
- `flock` 只保護夜間任務；手動 `make` 不受保護，會搶統一記憶體。
- `nightly.tonight(kind="auto")` 是 cron 入口：依星期決定種類 → `nightly()` 生成 → `merge_after`（`weekly.merge_kind`，不用 GPU，例外只記進 history，**不得讓整晚變 failed**）。`nightly()` 本身不合併、回傳值只含生成結果（既有測試依賴這點）。H3 沒啟用時 `nightly()` 直接記 `skipped`，不想主題。
- **合併身分** `merge_id` = sha256(kind｜fmt｜排序後的來源作業名)；去重看兩處：work/ 裡已有同身分（含舊版只有 `meta.sources` 的合併）、history 記過 `status: merged`（合輯上傳後 GX10 存檔被刪也不重做）。`failed` 的合併可重試。所有種類都只合併同一週的成片（`weekly.WINDOW_DAYS` = 6，**不用滑動視窗**，上週的來源不會再進這週的合輯）。`merge_id` 的片型欄是組合字串（兒童音樂與小知識 `short+long`），作業名用連字號（`weekly-short-long`、`weekly-trivia-short-long`）；`weekly.merge` 仍可傳單一片型字串。
- history 新增的欄位全是加法；merge 紀錄必須帶 `topic`／`format`／`kind`（`_past_topics`、`weekly.sources` 直接取值），並以 `type: "merge"` 區分，`weekly.sources` 會跳過它。
- `pipeline.run` 的呼叫簽名不能再加參數：夜間測試用 `fake_run(topic, fmt, resume, kind)` 限定參數。

## 修改指南

| 想做 | 改哪裡 |
|---|---|
| 新片型 | `plan.FORMATS`；`__main__` 的 `--format` choices（寫死）；`nightly.NIGHT`／`estimate`；`plan.KINDS`（每日合併與夜間排程共用）；補測試 |
| 新內容種類（像小知識） | `plan.Plan.kind` 的 Literal；`plan.KINDS`；`nightly.PLANS` 與主題建議；`__main__` 的 `--kind` choices（寫死）；`jobs.KIND_ZH`；`weekly._plan`／`weekly.COMPILATION` 的合輯文案；`nightly.SCHEDULE`；補測試。對白類另加 `plan.DIALOGUE_KINDS`、`content.SPECS`＋schema 類別 |
| 換／加 ComfyUI workflow | 以 **API 格式**匯出到 `workflows/`；`workflow_map.yaml` 加欄位對應；`LICENSES.md`、`models.txt`；`videoMaker check` 驗證；`test_workflow_node_refs_exist`／`test_workflows_match_live_object_info`（ComfyUI 連不上會 skip）會檢查對應 |
| 新子指令 | `__main__.build_parser` + `main`；若 TUI 要遠端呼叫，加進 `jobs.remote_cli` 白名單 |
| 新環境變數 | `config.py` + operations.md#環境變數 |
| 新企劃規則 | `plan.py` regex／驗證 + `_SYSTEM` + 測試（規則表更新到 operations.md） |

## 測試

- 全是不需要 GPU／ComfyUI 的單元測試（以 fake／monkeypatch 取代 ComfyUI、LLM、YouTube、ssh）；唯一的連線測試 `test_workflows_match_live_object_info` 連不上會 skip。TUI 用 Textual pilot。
- 新測試放進對應的既有檔案；`test_pipeline.py` 目前涵蓋範圍很廣。
- **測試失敗時修實作，不要改測試**（除非測試本身有錯）。偏好 TDD；覆蓋率目標 80%。同一個失敗最多修 3 次，仍失敗就停下來問使用者。

## 工作慣例（使用者偏好）

- 程式碼的關鍵邏輯註解與文件用**繁體中文（台灣用語）**，沿用現有風格；技術名詞與識別字維持英文。
- 改完程式要跑 ruff + pytest，宣告完成前**貼出實際輸出**；跳過任何步驟要明說。
- **commit 前先讓使用者確認內容**。格式 `<type>: <description>`（feat／fix／refactor／docs／test／chore／perf／ci）；不加 `Co-Authored-By` 等 attribution 行。
- 專案已有初始 commit（2026-10-16，35 個檔案一次收進）。`photo/`（私人照片）與 `.DS_Store` 已在 `.gitignore`，不要 `git add -f`。

## 已知問題與待辦

- 下列 80 張校準數據屬於舊抽幀規則，人工標記與模型畫面可能相差約 0.21 秒，須以新抽樣版本重查、重新人工標記後才能下結論；本次保留 20% 門檻，未自動重查或重設既有核准。
- 視覺 QC 是 clef-flash 每秒 2 張的初篩，門檻 20%（原 50%，實測 38% 的融合畫面會被放行）。2026-10-16 用 80 張人工標記（3 張有問題）初步驗證：20% 在 4 支作業上只標出 1 支（唯一明顯壞掉的），再降到 10% 會標出全部作業（4.2% 的畫面）卻只多抓 1 張；偵測器對細微缺陷幾乎無感（AUC 0.74、n 太小），所以門檻維持 20%（標記流程見 operations.md 的「校準門檻」），短暫異常、閃爍與動作不連續仍可能漏判；人工看片不可省略。舊作業不會自動補查或重設人工核准；模型失敗的 QC 可用 `--resume` 續跑。
- **對白類尚未端到端實跑**：H3 已放行，權重 2026-10-22 起在 GX10 背景下載；`workflows/h3_*.json` 只通過 `/object_info` 結構檢查，沒有 GPU 驗證。第一次實跑要在非夜間時段手動做（實測記憶體峰值與每鏡耗時、校準 `VMK_H3_EXTRA_GB`／`VMK_H3_SEC_PER_SEC`），步驟見 operations.md#啟用-h3-的前置條件。腳本企劃（LLM＋驗證）已用真實 LLM 對三種內容做過 `--dry-run`。
- 兒童英文對話沿用 `selfDeclaredMadeForKids: True`；動物短劇與有對白的動畫不宣告（不是兒童內容），公開前要在 Studio 自選受眾。YouTube 分類目前一律是 Music（10），對白類公開前也要在 Studio 改。
- GX10 的 crontab 已於 2026-10-22 換成 `nightly --kind auto` 單行（舊的 `1,3`／`2,4`／週五 `weekly` 三行備份在 GX10 的 `~/crontab.backup-20261022`，回滾見 operations.md#夜間排程）。H3 權重下載完成前，週三到週五只會記 `skipped`。
- Wan 在照片模式會憑空畫出「人手拿藍色棒狀玩具」，目前只有提示詞抑制，細節與實測見 operations.md#片型與照片模式。
- 過期 token 的「跳視窗 → 授權 → 自動複製」整條流程沒有端到端實跑過（各環節分開驗證過）。
- `LICENSES.md` 的企劃 LLM（`gpt-oss-20b-128k` 自訂 tag）授權尚未核對底層模型。
- `models.txt` 檔頭註解仍提到 `comfy-get-model.sh`，實際下載用 `scripts/download.sh`。
- `kids-shorts-pipeline-spec.md` 是 Phase 1 的歷史規格：路線 A（14B／RIFE／Lightning）、NAS 存放、`--route` 旗標都已不存在；現況以本檔與 operations.md 為準，其中 §2 硬性限制、§7、§11 的規則仍有效。
