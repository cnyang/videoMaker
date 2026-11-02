# videoMaker 操作手冊

給「用這套管線產片、審核、上傳、維運」的人。專案總覽見 [README](../README.md)；改程式的人（含 AI 代理）先讀 [AGENTS.md](../AGENTS.md)。

1. [機器與路徑](#機器與路徑)
2. [每天的流程](#每天的流程)
3. [TUI（互動式介面）](#tui互動式介面)
4. [指令參考](#指令參考)
5. [片型與照片模式](#片型與照片模式)
6. [夜間排程](#夜間排程)（含[對白類內容](#對白類內容兒童英文對話動物短劇有對白的動畫)與每日合併）
7. [YouTube 上傳與 token](#youtube-上傳與-token)
8. [環境變數](#環境變數)
9. [作業資料夾結構](#作業資料夾結構)
10. [企劃內容規則](#企劃內容規則)
11. [模型與下載](#模型與下載)
12. [部署與開發](#部署與開發)
13. [疑難排解](#疑難排解)
14. [已知限制](#已知限制)

---

## 機器與路徑

| 機器 | 位址 | 角色 |
|---|---|---|
| Mac | 本機 | 開發、YouTube 授權（要瀏覽器）、每日 token 檢查（LaunchAgent） |
| GX10 | `10.200.200.200`（ssh 別名 `gx10`） | ComfyUI（docker）、Ollama（LLM）、所有生成與夜間排程、實際上傳 YouTube |
| NAS | `10.100.100.100` | 模型檔案（管線已不再把成品存到 NAS） |

| 項目 | 路徑 |
|---|---|
| Mac 專案 | `/Users/cnyang/code/temp/videoMaker`（用 `uv run python -m videomaker …`） |
| GX10 專案 | `/home/ehomeservice/code/videoMaker`（用 `.venv/bin/python -m videomaker …`） |
| GX10 作業資料夾 | `/home/ehomeservice/code/videoMaker/work/<日期>_<slug>/` |
| 夜間歷史 | `work/history.jsonl`（GX10） |
| 夜間日誌 | `~/code/videoMaker/log/vmk-nightly.log`（GX10） |
| ComfyUI 模型目錄 | `/home/ehomeservice/code/comfyui-docker/models/`（GX10） |
| YouTube 憑證（Mac 與 GX10 各一份） | `~/.config/videomaker/client_secret.json`（只在 Mac）、`~/.config/videomaker/token.json` |
| Mac 的 token 檢查日誌 | `~/Library/Logs/videomaker-token.log` |

**金鑰絕對不放進專案或 git。** `.gitignore` 已排除 `client_secret*.json`、`token*.json`、`*.pem`；部署 GX10 的 `rsync` 也不會帶它們。

---

## 每天的流程

先在 Mac 的 `~/.zshrc` 加一次這個函式（已加好）：

```bash
videoMaker() {
  if [ $# -eq 0 ]; then
    (cd /Users/cnyang/code/temp/videoMaker && .venv/bin/python -m videomaker tui)   # 不帶參數 = 開 TUI
  else
    ssh gx10 "cd ~/code/videoMaker && .venv/bin/python -m videomaker $*"           # 帶參數 = 在 GX10 執行（如 videoMaker list）
  fi
}
```

> `videoMaker` 是 Mac `~/.zshrc` 的 shell 函式，不在專案內；非互動 shell 或 AI 代理環境沒有它，請用上面展開後的完整指令
> （本文件其餘地方的 `videoMaker …` 皆指 GX10 上的 `cd ~/code/videoMaker && .venv/bin/python -m videomaker …`）。

**`videoMaker` 開啟互動式 TUI**，所有審核、下載、上傳、刪除都在裡面按鍵完成，見 [TUI](#tui互動式介面)。

| 時間 | 動作 |
|---|---|
| 週一 19:30 | 夜間排程（**兒童音樂**）自動啟動（提早到辦公室冷氣還開著的時段，避免 GPU 在夜間無空調時長時間滿載）：LLM 想 3 個新主題，依序產生 1 支短片（約 20 秒）+ 1 支長片（約 2:48）+ 1 支 5 分鐘版，狀態停在「待審核」。實測約需 4 小時（約 23:30 到 00:20 結束），07:00 是硬性截止。結束後把當晚的 short 與 long 合併成一支，xl 不合併（見下） |
| 週二 19:30 | 夜間排程（**小知識**）：LLM 想 2 個英文維基百科條目，依序產生 1 支短片 + 1 支長片（沒有 xl），畫面疊英文字幕、說明欄附維基來源，狀態同樣停在「待審核」。見[小知識影片](#小知識影片)。結束後把當晚的 short 與 long 合併成一支 |
| 週三 19:30 | 夜間排程（**兒童英文對話**）：LLM 想 2 個學習主題，產生 2 支約 20～40 秒的短片（角色對話、英文字幕）。見[對白類內容](#對白類內容兒童英文對話動物短劇有對白的動畫)。結束後把這 2 支合併 |
| 週四 19:30 | 夜間排程（**動物短劇**）：固定主角 Meatball 與 HappySunny（兩隻寫實哈士奇）演 20～45 秒的搞笑日常短劇，每晚產 2 支，結束後把這 2 支合併 |
| 週五 19:30 | 夜間排程（**有對白的動畫**）：30～60 秒的故事短片（起承轉合、有對白），每晚產 2 支，結束後把這 2 支合併 |
| 每晚生成結束後 | **合併**（只用 ffmpeg `-c copy`，不用 GPU）：只合併同一種內容、同解析度的成片。**兒童音樂與小知識：把當晚的 short 與 long 依序串成一支（short 在前、long 在後），xl 不加入合併；對白類：把當晚的 2 支 short 串成一支。**來源是近 6 天內（同一週，不用滑動視窗：上週的來源不會再進這週的合輯）、成片還在 GX10、狀態待審核或已核准的作業（你按「不通過」或 QC 失敗的不會被串進去，所以少了其中一支就只剩 1 支）；不到 2 支就略過。合併身分由「內容種類＋片型組合＋來源作業名稱」決定：來源沒變就不重做，多了新來源才產生新的合併（`<日期>_weekly-short-long`、`<日期>_weekly-trivia-short-long`、`<日期>_weekly-<對白類>-short`，同日再合併加 `-2`）。合併失敗只記進 `history.jsonl`，不會讓當晚被標成 failed。合併結果同樣停在「待審核」；若來源都已人工核准，TUI 顯示「待審核（來源皆已核准）」，按 `u` 可直接上傳（不必再看片），否則要另外人工核准 |
| 早上（週二到週六） | 1. 輸入 `videoMaker` 開 TUI，每支一列，看狀態與兩處（GX10／Mac）是否都有<br>2. 若有「QC 失敗」「未完成」，到 `ssh gx10 'tail -40 ~/code/videoMaker/log/vmk-nightly.log'` 看原因<br>3. 選一列按 `d` 下載到 Mac，按 `o` 開啟看片<br>4. 好的按 `a` 核准，不好的按 `x` 不通過（也可以直接按 `u`：未核准時會問你要不要「先核准再上傳」）<br>5. 按 `U` 一次上傳所有已核准的（全部私人）<br>6. 到 YouTube Studio 逐支確認是「私人」且有「為兒童打造」，再手動改成公開或排程<br>7. 看完、傳完的，按 `g` 刪 GX10 的存檔（Mac 已下載時按 `y` 確認；Mac 沒有、那是唯一一份時要輸入 `DELETE`） |
| 09:00 | Mac 的 LaunchAgent 自動檢查 GX10 的 YouTube token；過期會跳視窗，見 [YouTube 上傳與 token](#youtube-上傳與-token) |

重點：

- **一定要你人工核准才會上傳**，沒核准的永遠不會上傳。
- 上傳一律是私人：專案沒通過 Google API 稽核，API 上傳的影片會被 YouTube 強制為私人，所以公開這一步必須手動。
- 成片只存在 GX10 與你下載到 Mac 的那份；**已不再備份到 NAS**，刪 GX10 存檔前請先按 `d` 下載。舊的 NAS 存檔（`/volume1/gx10-backup/ai-video/`）不會被動到，但 TUI 不再列出。

---

## TUI（互動式介面）

在 Mac 上輸入 `videoMaker`（或 `uv run python -m videomaker tui`）。TUI 只在 Mac 上跑，用 ssh 跟 GX10 溝通；耗時動作（下載、上傳、刪除）都在背景執行，不會卡住畫面。每 2 分鐘自動重新整理，也可按 `r`。

表格每列一支作業，欄位：狀態、主題、片型、長度、大小、YouTube 影片 ID，以及 **GX10／Mac 兩欄**（`●` = 該處有，`·` = 沒有）。兩處只要任何一處有就會列出，所以「GX10 已刪、只剩在 Mac」的作業也看得到。

「視覺 QC」欄顯示未見疑慮、有疑慮、檢查失敗、檢查中或未檢查，以及目前最高疑慮分數。按 Enter 看各項最高分數、原始鏡頭檔名與約略時間；詳情可捲動。舊作業不會自動補查，也不會清除既有人工審核。

| 按鍵 | 動作 | 說明 |
|---|---|---|
| ↑ ↓ | 選擇 | |
| Enter | 詳情 | 標題、片型、長度、位置、Mac 路徑、YouTube 連結 |
| 空白 | 勾選／取消勾選 | 勾選後游標自動下移，可連按；第一欄「選」固定顯示勾選框（`⬜` 未勾、`✅` 已勾）；Esc 清除全部勾選。目前只給批次刪除用 |
| `d` | 下載到 Mac | 下載 `final.mp4`、`thumb.jpg` 與企劃／QC JSON 到 `~/Downloads/videoMaker/<作業>/`；GX10 已沒有就無法下載 |
| `o` | 開啟 | GX10 仍有成片時，先以 checksum 同步最新檔，再用播放器開啟；只剩 Mac 時直接開啟本機檔 |
| `a` | 核准 | 先按 `o` 看片，核准會比對觀看版本的 hash；只能核准有 QC 結果的作業，QC 失敗會再確認一次 |
| `x` | 不通過 | |
| `u` | 上傳這一支（私人） | 已核准直接上傳；合輯的來源都已核准（狀態顯示「待審核（來源皆已核准）」）也直接上傳，GX10 端會重驗來源並記 `approved_basis: sources`；未核准會問你要不要「先核准再上傳」；已上傳過的不會重複上傳 |
| `U` | 上傳全部已核准 | 先確認支數，全部私人上傳 |
| `f` | 上傳 FB Reels 草稿 | 必須先按 `o` 看片、按 `a` 核准；上傳到設定的粉絲專頁，不發布。FB 欄顯示「待完成」或「草稿」，可與 YouTube 分別上傳 |
| `g` | 刪除 GX10 存檔 | 刪掉 `work/<作業>/` 整個資料夾；有勾選時改為批次刪除所有勾選的 |
| `m` | 刪除 Mac 下載檔 | 刪掉 `~/Downloads/videoMaker/<作業>/` 整個資料夾（含成片、縮圖與 JSON）；有勾選時改為批次刪除所有勾選的 |
| `T` | token | 檢查 GX10 的 YouTube token，過期就跳視窗、重新授權並自動複製 |
| `r` | 重新整理 | |
| `q` | 離開 | |

**刪除的安全機制**

- 一般刪除：跳出確認視窗，按 `y` 確認、`n` 或 Esc 取消。
- **唯一一份**（刪 GX10 時 Mac 沒有下載，或刪 Mac 時 GX10 已沒有成片）：視窗會用紅字警告，**必須手動輸入 `DELETE` 再按 Enter 才會刪**，單按 `y` 無效。
- 還在執行中的作業（沒有 `status.json` 且 30 分鐘內有檔案更新，`jobs.JOB_ACTIVE_SECONDS`）不能刪除、也不能審核。門檻取 30 分鐘是因為單支 Wan 片段要 15–20 分鐘才寫出檔案。
- **批次刪除**（有勾選時按 `g` 或 `m`）：只跳一次確認，列出要刪的作業。進行中、該處沒有存檔的會略過並標示支數；只要其中一支是唯一一份就整批必須輸入 `DELETE`。單支失敗不會中斷其餘，最後彙整回報；失敗的保留勾選方便重試，刪成功的取消勾選。有勾選時 `g`／`m` 一律作用在勾選的列，不是游標所在列。
- 作業名稱必須符合 `<日期>_<slug>`（只含小寫英數與連字號），任何含 `/`、`..`、空白、`;`、`$` 等字元的名稱都會被拒絕，**不會送出任何遠端指令**。
- `g` 只刪 GX10 存檔，`m` 只刪 Mac 下載檔，都不影響已上傳的 YouTube 影片。刪除不經垃圾桶；Mac 的作業資料夾若是符號連結會拒絕刪除。

**幾件事**

- 上傳、核准在 GX10 執行（token 與成片都在那裡），所以 GX10 上沒有這支作業時 `a`、`x`、`u`、`d`、`g` 都不能用，仍可看列表、開啟或用 `m` 刪除已下載的 Mac 本機檔。
- 核准 hash 記在 `status.json.approved_sha256`；上傳前再次比對成片，內容變更或舊核准沒有 hash 都要重新人工核准。既有狀態不會批次改寫；已上傳的仍回傳原影片 ID。每作業的 `.upload.lock` 保護單支與批次上傳，避免同時觸發而建立兩支影片，請勿手動刪鎖檔。CLI `approve <作業> --sha256=<觀看版本的 hash>` 可明確比對版本；省略時視為核准 GX10 上當下的成片。`approve <合輯> --from-sources` 只在來源全部已核准、合輯為待審核時才核准（來源已被刪、QC 失敗或被否決的合輯一律拒絕）。
- 想看原始畫面的文字輸出，仍可用 `videoMaker list`（帶參數就是在 GX10 執行該子指令）。

---

## 指令參考

所有指令的入口是 `python -m videomaker <子指令>`。下面用 `videoMaker` 代表在 GX10 上執行；在 Mac 上跑則是 `uv run python -m videomaker …`。

### `make` — 產生一支影片

```
videoMaker make "主題" [--kind kids|trivia|kids_english_dialogue|animal_sitcom|dialogue_animation] [--format short|long|xl] [--stage <階段>] [--images DIR [DIR]] [--dry-run] [--resume]
```

| 選項 | 說明 |
|---|---|
| `主題` | 例如 `小熊刷牙`。同一天同主題再跑一次，會另開資料夾（結尾加 `-2`） |
| `--format short\|long\|xl` | `short` ≈ 20 秒（5 鏡）；`long` ≈ 2:48（14 個獨特鏡頭重複使用，共 42 個時段）；`xl` ≈ 5:00（20 個獨特鏡頭重複使用，共 75 個時段，**超過 3 分鐘，不再是 Shorts**）。預設 `short` |
| `--stage` | 「跑到這個階段為止」，已完成的階段會跳過。可選：`plan`、`music`、`keyframes`、`clips`、`mux`、`qc`（預設到 `qc`） |
| `--images DIR [DIR]` | 照片模式：1 個資料夾 = 用你的照片當關鍵幀；2 個資料夾 = 兩隻狗同框。見[片型與照片模式](#片型與照片模式) |
| `--kind` | 內容種類，預設 `kids`（兒童音樂）。對白類三種的 `主題` 填繁體中文短語、只支援 `--format short`，需要先啟用 H3，見[對白類內容](#對白類內容兒童英文對話動物短劇有對白的動畫) |
| `--dry-run` | 只產 `plan.json`，不呼叫 ComfyUI（對白類不需要啟用 H3 就能 dry-run） |
| `--resume` | 續跑「同主題、同 `--kind`、同 `--format`」建立時間最新的作業；小知識的既有正式標題可離線續跑，別名才需要查維基。每階段輸出已存在就跳過；重合成先產生暫存影片與縮圖，兩者都成功才發布，失敗可重試 |

在 GX10 上手動跑時要帶環境變數（自動排程的 cron 已內建）：

```bash
export VMK_COMFY_URL=http://127.0.0.1:8188 VMK_LLM_URL=http://127.0.0.1:11434/v1
.venv/bin/python -m videomaker make 小熊刷牙
```

長任務請放進 tmux 或用 cron，不要依賴 Mac 的 ssh 連線（螢幕保護程式啟動時連線會斷）。

**實測耗時（Wan 5B，20 步）**：短片約 30 到 34 分鐘（企劃約 20 到 180 秒（LLM 驗證退回重寫時會變長）、音樂約 11 到 72 秒、關鍵幀約 42 秒、影片約 29 分鐘[每鏡約 346 秒]、合成約 4 秒）；長片約 84 分鐘；兩隻狗的合成約 3 到 4 分鐘一張。

### `nightly` — 夜間排程本體

```
videoMaker nightly [--kind auto|kids|trivia|kids_english_dialogue|animal_sitcom|dialogue_animation] [--plan short,long,xl] [--deadline 07:00] [--dry-run]
```

`--kind auto`：依星期決定今晚做哪一種（週一兒童音樂、週二小知識、週三兒童英文對話、週四動物短劇、週五有對白的動畫，週末不跑）；crontab 就是用它。`--kind kids`（預設，維持舊行為）：LLM 想不重複的繁體主題 → 依序產生 short、long、xl。`--kind trivia`：LLM 想英文維基百科條目 → 依序產生 short、long。對白類每晚 2 支 short（各自的主題）。生成結束後（非 `--dry-run`）會接著合併同一種內容。`--dry-run` 只想主題與預估，不產片、不合併。詳見[夜間排程](#夜間排程)、[小知識影片](#小知識影片)與[對白類內容](#對白類內容兒童英文對話動物短劇有對白的動畫)。

### `weekly` — 手動合併

```
videoMaker weekly
```

夜間排程結束後會自動合併當晚那一種內容；這個指令是手動版，一次處理所有內容種類：每種內容各串成一支：把近 6 天內（同一週）成片還在 GX10、狀態待審核或已核准的作業串起來（兒童音樂與小知識是 short＋long、xl 不加入；對白類是 short；`-c copy`，最多最新 4 支、同解析度）；不同內容種類不會混在一起。`history.jsonl` 沒有 `kind` 欄位的舊紀錄視為兒童音樂。來源沒變就不重做；詳見[夜間排程](#夜間排程)。

### `characters` — 固定角色庫

```
videoMaker characters list                 # 列出角色與缺少的參考圖
videoMaker characters build [角色 ID …] [--force] [--salt N]   # 一次性產生缺少的參考圖（佔用 GPU，不要在夜間時段跑；--salt 換一組 seed）
```

見[對白類內容](#對白類內容兒童英文對話動物短劇有對白的動畫)。

### `list` — 列出所有作業的狀態

```
videoMaker list
```

每支一行：`資料夾 | 片型 | 狀態 | 長度 | 主題`。狀態：

| 狀態 | 意思 |
|---|---|
| 待審核 | QC 通過，等你看片 |
| 已核准（待上傳） | 你核准了，還沒上傳 |
| 已上傳 `<影片 ID>` | 已私人上傳 |
| 不通過 | 你標記為 reject |
| QC 失敗 | 自動檢查有項目沒過（看 `status.json` 的 `checks`） |
| 未完成 | 階段中途失敗或還在跑 |

### `approve` / `reject` — 人工審核

```
videoMaker approve <資料夾名>      # 之後才能上傳
videoMaker reject  <資料夾名>
```

資料夾名是 `work/` 底下的名字（如 `2026-10-05_feng-ling-qing-xiang`），也可以給完整路徑。結果寫進該作業的 `status.json`。

### `upload` / `upload-approved` — 上傳 YouTube（私人）

```
videoMaker upload <資料夾名>        # 只上傳這一支（必須已核准）
videoMaker upload-approved          # 上傳所有「已核准、尚未上傳」的
```

- 上傳內容：`privacyStatus = private`、`selfDeclaredMadeForKids = true`（兒童內容）、分類 Music；動物短劇與有對白的動畫明確送 `selfDeclaredMadeForKids = false`（不是兒童內容；不送的話 YouTube 會套用頻道預設「為兒童打造」）；標題、說明、標籤取自 `plan.json` 的 `youtube`；**照片模式**的影片會加上 `containsSyntheticMedia = true`（真實照片經 AI 動畫化）。
- 冪等：已上傳過的不會重複上傳（`meta.json` 會記 `youtube_id`、`youtube_uploaded_at`、`youtube_privacy`）。
- `upload-approved` 一支失敗不會擋其他支；失敗的下次重跑即可。
- 上傳的影片網址：`https://studio.youtube.com/video/<影片 ID>/edit`。

### `facebook-auth` / `upload-facebook` — Facebook Reels（粉絲專頁草稿）

這個入口只建立 **DRAFT 草稿**，不會公開或排程發布。先人工核准成片，完成後再到 Meta Business Suite 檢查與手動發布；YouTube 的 `upload`／`upload-approved` 仍只處理 YouTube。

**首次設定（在 GX10 操作，不要將 token 貼在聊天或命令列）**

1. 在 [Meta 開發者後台](https://developers.facebook.com/apps/) 建立 App，設定 Facebook Login／粉絲專頁存取。依 [Pages API getting started](https://developers.facebook.com/docs/pages-api/getting-started/) 取得目標專頁的 **Page access token**；不是 User token 或 App token。授權帳號需有該專頁建立內容的權限，常用權限為 `pages_show_list`、`pages_read_engagement`、`pages_manage_posts`。是否需要 App Review／Advanced Access 取決於 App 模式與管理對象，請以 Meta 後台顯示為準。
2. SSH 登入 GX10，在專案目錄執行下列互動設定。輸入 Page ID，再貼上 token（不回顯）。程式只將憑證存在專案外的 `~/.config/videomaker/facebook.json`，權限 `600`；不要自行放進 `work/`、repo 或 `.env`。

```bash
cd ~/code/videoMaker
.venv/bin/python -m videomaker facebook-auth
```

Token 過期或權限被撤回時，用相同指令更新；此版沒有 OAuth 網頁登入與自動換發 token，也不讀取或修改 YouTube 憑證。若在 Mac 設定，只能供 Mac CLI 上傳使用；TUI 的上傳在 GX10 執行，必須在 GX10 設定。

**日常上傳**

```text
TUI: o (觀看最新版) -> a (人工核准) -> f (FB 草稿) -> Meta (檢查與手動發布)
```

```bash
videoMaker approve <資料夾名>
videoMaker upload-facebook <資料夾名>
```

- CLI 的 stdout 只有影片 ID；進度與錯誤不含 token。標題與說明沿用 `plan.json.youtube`；這個專案產出的影片一律送 `is_ai_generated=true`。
- 目前依 [Meta 官方範例](https://github.com/fbsamples/reels_publishing_apis/tree/main/fb_reels_publishing_api_sample) 與 [官方 API collection](https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api) **保守接受 4–60 秒、9:16、至少 540×960、至少 23fps**，以實際成片的 ffprobe 結果判斷；長片或超長合輯會拒絕，不自動裁切、不重新生成。Meta 網站能接受的長度可能與 API 不同，此版未放寬限制。
- 沿用 `status.json.approved_sha256` 與 `.upload.lock`。比對 hash、ffprobe 與傳送影片使用同一個已開啟檔案，防止上傳途中路徑被換成其他版本。鎖只護住讀寫 `meta.json` 與送出請求；等 Meta 處理（最久約 2.5 分鐘）時不持鎖，結果寫回前會重讀 `meta.json`，所以這段時間可以核准、不通過或上傳 YouTube，也不會互相覆寫。
- **FB 不接受「合輯來源全核准」的例外**：`status.json` 的 `approved_basis` 是 `sources` 時 `upload-facebook` 一律拒絕，要在 TUI 按 `o` 看片、再按 `a` 人工核准（重新核准會清掉 `approved_basis`）。
- `meta.json.facebook_reel` 分開記錄 `page_id`、`video_id`、`sha256`、`upload_url`、`phase`。`phase` 依序為 `starting → created → uploaded → finishing → processing → draft`；只有 finish 的 `DRAFT` 請求成功、Meta 回報 `video_status=ready`，**且 `publishing_phase.publish_status` 明確是 `draft` 或 `not_started`** 才標為草稿（欄位缺漏或其他值都不算，寧可停在 `processing`）。這代表 API 已完成處理，仍需到 Meta 確認草稿與發布狀態；TUI 不追蹤你之後在 Meta 的手動發布。
- 終態：處理失敗記 `failed`（TUI 顯示「失敗」）、Meta 回報已發布／已排程記 `published`（TUI 顯示「已發布」）。這兩種重跑會直接拒絕、不連線，要先到 Meta 處理，再依下一條的步驟移除 `facebook_reel`。已是 `draft` 的重跑只比對成片 hash 就回傳影片 ID，不需要憑證或企劃。
- 傳送或處理中斷，重跑 **同一作業** 會沿用 ID、依伺服器回報位置續傳或只查狀態（停在 `finishing` 時會先問 Meta 收到 finish 沒有，收到就不重送）；處理超過約 2.5 分鐘就保留進度並提示重試，不會冒充完成。遠端影片處理失敗、被刪除或 session 過期時，要先到 Meta 處理，不自動另建一支。
- 建立 session 時若 Meta 明確拒絕（HTTP 4xx，例如 token 過期、專頁 ID 或權限錯誤）或連線階段就逾時，程式會自動撤回 `starting` 標記，修好後直接重跑即可。只有結果不明（送出後逾時、5xx、DNS 失敗等）才會停在 `starting` 並拒絕再建立，避免發生不明的重複草稿。這時請先到 Meta 查明有無物件；確認沒有要保留的草稿後，備份 `meta.json`，在沒有上傳執行中的情況下移除該檔的 `facebook_reel` 欄位，再重跑（`failed`／`published` 也用這個步驟）。Meta 回傳的上傳網址無效時，`video_id` 已先記進 `meta.json`（`created`），可據此到 Meta 查詢。切換專頁或成片 hash 變更也會拒絕沿用既有 Reel，需先人工確認。
- API 固定使用 `v25.0`，可用 `VMK_FB_API_VERSION` 指定 App 支援的版本。此版沒有 FB 批次上傳、自動發布或排程發布。

### `auth` / `token-status` / `renew-token` — YouTube 授權

```
uv run python -m videomaker auth          # Mac：開瀏覽器授權，產生 ~/.config/videomaker/token.json
videoMaker token-status                            # 檢查 GX10 的 token：ok / expired / missing（exit code 0 或 2）
uv run python -m videomaker renew-token    # Mac：檢查 GX10 的 token，過期就跳視窗 → 授權 → 自動複製
```

詳見 [YouTube 上傳與 token](#youtube-上傳與-token)。

### `tui` / `jobs-json` — 互動式介面

```
uv run python -m videomaker tui      # 在 Mac 上跑（等於輸入 videoMaker）
videoMaker jobs-json                          # 在 GX10 上盤點作業，輸出 JSON（TUI 內部使用）
```

### `check` — 模型是否都在

```
videoMaker check
```

向 ComfyUI 的 `/object_info` 比對所有 workflow 用到的模型檔名，缺的列出（`缺：<檔名>（<workflow>）`），齊全則輸出 `模型齊全`。

### `bench` — 各階段耗時

```
videoMaker bench
```

統計 `work/` 底下「跑完 QC」的作業，輸出各階段平均耗時的表格。

---

## 片型與照片模式

### 短片與長片

| | `short` | `long` |
|---|---|---|
| 目標長度 | 約 20 秒（15 到 20.5 秒） | 約 2:48（150 到 179 秒，必須在 YouTube Shorts 的 3:00 上限內） |
| 獨特鏡頭 | 5 個（每鏡 2 小節 = 4 秒 @120 BPM） | 14 個，輪流重複填滿 42 個時段 |
| 重複方式 | 無 | 第一輪照順序，之後每輪換順序、相鄰時段不同鏡頭；重複使用同一鏡頭時換起點（第 0、24、12 幀） |
| 預估耗時 | 約 37 分鐘 | 約 104 分鐘（實測 84 分鐘） |

另有 `xl`（約 5 分鐘，300 秒）：20 個獨特鏡頭、75 個時段，每個鏡頭用 3 到 4 次。實測（兩隻狗照片模式）：20 張同框合成 60 分鐘、20 個 Wan 影片 113 分鐘，**全程約 3 小時 10 分鐘**，成片約 354MB。

BPM 固定 120（程式允許 100 到 140）。鏡頭切點落在偵測到的拍點上，誤差小於 1 幀。

### 照片模式：用你自己的狗狗照片

1. 把照片放到 GX10，每個資料夾一隻狗；可選擇附同名 `.txt`，寫一行**英文**描述：

```
photos/meatball/  meatball1.jpeg  meatball1.txt  ...      （txt 例：a black and white siberian husky with a gray and white face）
photos/sunny/     sunny1.JPG      sunny1.txt      ...      （txt 例：a fluffy reddish-brown and white husky）
```

2. 一隻狗（1 個資料夾）：照片直接放大裁切成 704×1280 當每鏡關鍵幀（橫式照片會被裁切成 9:16，狗可能被切掉，程式會警告）。

```
videoMaker make "小狗跳舞" --images photos/dog
```

3. 兩隻狗同框（2 個資料夾）：每鏡各取兩邊的一張，用 Qwen-Image-Edit-2509 合成同框，每鏡換場景（草地、客廳地毯、公園、沙灘、花園）：

```
videoMaker make "肉丸和Sunny跳舞" --images photos/meatball photos/sunny
```

注意：

- 兩隻狗模式的 `.txt` **只寫外觀**（品種、毛色），不要寫姿勢或場景。
- 描述必須是英文（中文會被拒絕），沒有 `.txt` 就用 `a dog`。
- 照片比鏡頭少就輪流重複；比鏡頭多只取前面幾張。
- **已知問題：Wan 會憑空畫出「人手拿藍色棒狀玩具」**。實測 5 分鐘版 20 個影片約 14 個出現藍色棒狀玩具、約 7 個還有人手（五種律動動作都有）；原因推測是「兩隻狗並排正面坐著、頭在動」被聯想成「主人拿玩具逗狗拍照」。照片模式已自動加上抑制提示詞（負向：人、人手、手臂、棍子、玩具、逗狗棒、球、藍色玩具、藍色棍子；正向：`only the dogs are in the scene`），三個實驗鏡頭的實測：大手與大棍子幾乎消失，但邊緣仍會偶爾闖入細棍子；把動作改成「安靜坐著呼吸、耳朵輕動」則 2 個鏡頭 8 個取樣幀全部乾淨，代價是狗幾乎不「跳舞」。樣本很小（3 個鏡頭）。
- 動作固定是「點頭、左右搖擺、上下彈跳、歪頭」之類小而慢的律動，Wan 才不會把真實照片扭曲；**動作不保證踩在拍點上**，只有鏡頭切換落在拍點。
- 合成會要求「不要出現任何文字、數字或名牌」；兩隻狗的長相是「大致像」，不保證和照片一模一樣。
- 照片模式不套用企劃內容規則（因為描述來自你），原始照片會複製到作業的 `source/`，檔名與 sha256 記在 `meta.json`（來源證據）。

---

## 小知識影片

給兒童看的小知識短片（週二夜間產生）。**每則知識都必須有來源**，做法是「先抓來源、再寫內容」，不信任 LLM 自己記得的事實或網址：

```
LLM 想英文維基百科條目名稱 → wiki.fetch 抓條目（不存在、消歧義頁、太短的直接略過）
  → LLM 只能依抓到的條目寫 facts：每則 = 一句英文字幕 text + 條目原文的片段 quote
  → 程式驗證：quote 逐字出現在條目裡（≥ 30 字元）、字幕裡的數字也出現在 quote 裡、字幕裡 4 字母以上的實詞（扣掉虛詞、條目標題的字，容許詞形變化）也都出現在 quote 裡、字幕純英文且 ≤ 110 字元、不重複；不過就把錯誤（含被擋的詞）丟回 LLM 重寫（最多 3 次）
  → 來源由程式寫入 plan.sources 與 YouTube 說明欄（固定版本的永久連結 + CC BY-SA 4.0），不採用 LLM 填的網址
```

- **畫面與配樂**：和兒童音樂同一條管線（ACE-Step、FLUX、Wan，同樣的企劃內容規則，角色是條目主題的卡通版）。畫面上的 AI 仍然不准畫字。
- **英文字幕**：`mux` 時用 ffmpeg `drawtext` 疊上去（程式畫的，不會亂碼），字型預設 GX10 內建的 DejaVu Sans Bold（`VMK_FONT` 可換）。每則 fact 平分時段（`plan.FACTS_PER`：short 2 則約 10 秒一則、long 6 則約 28 秒一則），字幕文字在作業的 `captions/NN.txt`。GX10 的 ffmpeg 有 drawtext，Mac 的沒有，所以本機跑不了含字幕的 `mux`。
- **中文名稱**：取英文條目的中文跨語言連結，再向中文維基要台灣正體（zh-tw）標題（`长颈鹿` → `長頸鹿`），標題與說明欄必須使用它；查不到就不強制。同一晚選主題與產作業用同一份抓取結果（`wiki.fetch` 有快取），來源版本一致。
- **YouTube 欄位**：標題與說明維持中英雙語；說明欄最後固定附 `Source: Wikipedia, "<條目>" (<永久連結>), CC BY-SA 4.0.`。上傳照舊一律私人、人工核准、標「為兒童打造」。
- **片型**：只有 short、long（沒有 xl，也不支援照片模式）。
- **手動產一支**：`videoMaker make "Sea otter" --kind trivia [--format long]`（`topic` 必須是英文維基百科條目名稱，大小寫、單複數等重新導向都可以；`--resume` 先找同內容種類、同片型的既有作業，找不到才解析正式標題）；先用 `--dry-run` 看企劃。`nightly --kind trivia --plan` 帶了不支援的片型（xl）會直接報錯，不會浪費一次重試。
- **`meta.json` 與 `plan.json`**：`meta.wikipedia` 記條目標題、永久連結、授權；`plan.kind = "trivia"`、`plan.facts`（字幕與 quote）、`plan.sources`。
- **限制（人工看片不可省）**：quote 驗證保證「引用的原文存在」，詞彙比對擋下「字幕寫了 quote 沒說的事」（實測抓到過 `eat sea urchins`）；但它是啟發式，抓不到把範圍寫成單一數字、省略但書這類改寫，同義詞改寫也會被擋（所以 LLM 必須沿用 quote 的用詞）。中文標題與說明欄只檢查有沒有用維基的中文名稱，內容沒有驗證。字幕與條目仍要人工對照。維基條目會被編輯，永久連結固定在抓取當時的版本。

## 對白類內容（兒童英文對話、動物短劇、有對白的動畫）

三種內容共用同一條管線：**LLM 寫結構化腳本（JSON）→ 程式驗證 → 關鍵幀（只有 `h3_fl2va`）→ H3 一鏡一鏡產生帶對白音訊的短片 → ffmpeg 串接＋字幕＋ loudness → QC → 待審核**。

| 內容 | `--kind` | 星期 | 長度／鏡數 | 預設 renderer | 目的 |
|---|---|---|---|---|---|
| 兒童英文對話 | `kids_english_dialogue` | 三 | 20～40 秒／3～5 鏡 | `h3_fl2va` | 英文教育（1 個明確學習點、國小程度句型） |
| 動物短劇 | `animal_sitcom` | 四 | 20～45 秒／3～6 鏡 | `h3_ref2va` | 兩隻哈士奇（`husky_01` Meatball、`husky_02` HappySunny）的搞笑日常，寫實畫風，角色一致性優先 |
| 有對白的動畫 | `dialogue_animation` | 五 | 30～60 秒／3～6 鏡 | `h3_fl2va` | 故事與角色互動（hook → setup → conflict → reaction → punchline） |

兒童音樂（`kids`）與小知識（`trivia`）的流程完全不變，仍用 Wan，不使用 H3。

**⚠ H3 授權限制。** MiniMax H3 於 2026-10-22 由你放行，但授權排除美國、歐盟、英國、南韓（`LICENSES.md`）——影片公開到 YouTube 後這些地區也看得到，是否公開由你判斷（程式一律先私人上傳）。H3 預設啟用；`VMK_H3_ENABLED=0` 可一鍵關閉。**權重還沒下載完（ComfyUI 下拉清單看不到）時**，週三到週五的夜間排程只會記一筆 `skipped`（`reason: renderer … 的模型尚未安裝：…`），不想主題、不碰 GPU。`videomaker check` 會列出缺的 H3 模型。啟用步驟見[啟用 H3 的前置條件](#啟用-h3-的前置條件)。

### 結構化腳本

LLM 輸出 JSON，`content.py` 用 pydantic 驗證，失敗把錯誤丟回給 LLM 重寫，最多 3 次（沿用 `make_plan` 的模式），仍失敗整支作業失敗。通過的腳本存在 `plan.json` 的 `script`：

| 內容 | 頂層欄位 | 每鏡欄位 |
|---|---|---|
| 兒童英文對話 | `title`、`topic`、`learning_goal`、`vocabulary`、`characters`、`duration` | `shot_id`、`duration`、`dialogue`、`subtitle`、`visual_prompt` |
| 動物短劇 | `episode_title`、`characters`、`setting`、`hook`、`reference_assets`、`duration` | `shot_id`、`duration`、`action`、`emotion`、`camera`、`dialogue`、`reference_assets` |
| 有對白的動畫 | `title`、`synopsis`、`characters`、`setting`、`duration` | `shot_id`、`duration`、`characters`、`action`、`emotion`、`camera`、`dialogue`、`visual_prompt`、`beats`、`reference_assets` |

程式強制的規則（不靠 LLM 自律）：角色 ID 必須存在於角色庫；說話者必須是該鏡的角色；單鏡 5～15 秒（H3 的訓練範圍）、總長在上表範圍內，`duration` 由程式加總覆寫；語速上限每秒 3 個詞；對白純英文、不含禁用字詞；視覺描述不得出現文字類物件（字幕由程式畫）。兒童英文對話另外要求：一句最多 10 個詞、單字最多 10 個字母、`subtitle` 與 `dialogue` 逐字一致（忽略標點與大小寫）、`vocabulary` 3～8 個且出現在對白裡、至少兩個角色輪流說話、最後一句是問觀眾的問句（含 `you`、以 `?` 結尾）。動物短劇要求 `characters` 剛好是 `husky_01` 與 `husky_02`（`content.LEADS`）、每鏡有 `action`／`emotion`／`camera`。有對白的動畫要求至少兩個角色、五個故事段落依序都出現、每鏡都有對白與動作（不能只是站著說話）。YouTube 欄位仍須中英雙語。

對白一律英文（字幕用 DejaVu 字型，沒有中文字形；H3 的中文配音能力也未驗證），主題標題與 YouTube 文案是繁體中文。

### 角色庫

`characters/<id>/character.json` ＋ 參考圖（`front.png`、`side.png`、`happy.png`、`sad.png`…，視角由 `references` 宣告）。目前有 `husky_01`（Meatball，黑白異色瞳哈士奇）、`husky_02`（HappySunny，棕白哈士奇）——這兩隻是動物短劇的固定主角，參考圖是從使用者提供的角色設定圖裁出來的寫實照片（`front`／`side`／`happy`），只出現在動物短劇的角色清單；另有卡通角色 `dog_01`（Mochi）、`dog_02`（Bruno）、`bear_01`（Benny）、`rabbit_01`（Rosie），給兒童英文對話與有對白的動畫用。欄位：`id`、`name`、`species`、`breed`、`appearance`（純英文、每鏡原樣附在提示詞）、`personality`、`accessories`、`voice`（H3 的聲音描述）、`references`。

- **只能引用存在的角色 ID**；不存在或寫成別的格式就是驗證失敗，由 LLM 改寫，不會偷偷生成新角色。ID 只接受 `<英文>_<兩位數字>`（擋路徑穿越）。
- 參考圖是**持久資產**：`videomaker characters build` 只產生缺少的圖，已存在就跳過、`--force` 才覆蓋（`--salt N` 換一組 seed，0 = 預設）；產法：`front` 用 FLUX.1 schnell 文字生圖，其他視角用 Qwen-Image-Edit-2509 從 `front` 編輯而來，各視角才是同一隻角色（seed 由「角色＋視角＋salt」決定）；夜間排程**絕不**生成角色。產生後請人工檢查，不滿意就手動換圖（檔名要與 `references` 一致）。
- `characters/` 會隨 rsync 部署到 GX10；參考圖在 GX10 上產生，要回到 Mac 版控請自行 rsync 回來。
- 新增角色：建資料夾與 `character.json`，執行 `characters build <id>`。

### FL2VA 與 Ref2VA 怎麼選

`renderer` 在**建立作業時**寫進 `meta.json`（續跑不會中途換），預設由內容種類決定，`VMK_RENDERER_<KIND>`（如 `VMK_RENDERER_ANIMAL_SITCOM=h3_fl2va`）可覆寫。

| | `h3_fl2va`（首幀轉影片＋音訊） | `h3_ref2va`（參考圖轉影片＋音訊） |
|---|---|---|
| 角色一致性來源 | 固定外觀文字＋FLUX 關鍵幀 | 角色庫的參考圖（`<Picture N>`，每鏡 1–3 張，腳本沒寫就每個在場角色取 `front`） |
| 額外階段 | 要先產關鍵幀（約 12 秒/張） | 不需要，但參考圖必須已產生 |
| 適合 | 場景構圖重要、角色簡單（兒童英文對話、動畫） | 同一角色要在多集長期保持一致（動物短劇） |
| 缺資源時 | — | 開跑前（佔用 GPU 之前）中止，提示 `characters build` |

評估建議：先用 `h3_ref2va` 跑動物短劇；若 H3 在 Ref2VA 的品質不如 FL2VA（參考圖密集時對提示詞很敏感），把動物短劇改成 `h3_fl2va`——靠固定關鍵幀一樣可以維持角色。兩者的 workflow 在 `workflows/h3_fl2va.json`、`h3_ref2va.json`，結構取自 ComfyUI 官方範本，**尚未在 GX10 實跑驗證**。

### 夜間保護機制（套用到新片型）

- **deadline**：`nightly.estimate` 對 H3 以「企劃最長時長＋取整餘裕 × 每秒生成成本」估算；每秒成本取 `history.jsonl` 最近 10 支同 renderer 實際值的中位數（欄位 `gen_sec_per_video_sec`），沒有資料用保守預設 `VMK_H3_SEC_PER_SEC`（90）。「現在＋預估」超過 deadline 就記 `skipped`。
- **GPU 溫度**：與其他內容相同，每支（含重試）開跑前 `wait_cool`。
- **記憶體**：`dialogue.clips` 開始前 `Comfy.prepare(extra_gb=VMK_H3_EXTRA_GB)`＝先 `/free` 卸載 ComfyUI 模型、再檢查「剩餘記憶體 ≥ `VMK_MIN_FREE_GB` ＋ `VMK_H3_EXTRA_GB`」，不足就中止（基準門檻不降低）；企劃的 LLM 寫完腳本就以 `keep_alive: 0` 卸載；階段結束再 `/free`。`VMK_H3_EXTRA_GB` 預設 25 是**尚未實測的保守值**，首次實跑後依峰值校準。
- **失敗**：與其他內容相同——等 10 分鐘 `--resume` 一次，仍失敗記 `failed` 並繼續，不中斷整晚。H3 逐鏡輸出（`clips/NN.mp4`）已存在就跳過，所以續跑只重做沒完成的鏡頭。
- **flock**：沿用 cron 的 `flock`。

### QC

沿用解析度 1080×1920、音軌、響度 -14 ±1.5 LUFS、黑畫面與 clef-flash 視覺抽樣（`Job.windows()` 改讀 `timeline.json`，每個鏡頭整段檢查）；長度範圍改跟著企劃總長（企劃範圍下限 -2 秒、上限 +5 秒，因為 H3 幀數會往上取整到 17k+5 格）。對白類另外三項（純 metadata／訊號，不跑 ASR）：`no_long_silence`（-50dB 以下的無聲區間不得超過 3 秒；完全落在「沒有對白的鏡頭」內的不算，如反應鏡頭）、`subtitles_within_duration`（字幕時間軸在片長內且不重疊）、`subtitles_match_dialogue`（`subtitles.json` 的文字與腳本對白逐句一致）。字幕時間是**依詞數分配的估計值**（沒有 ASR 對齊），文字就是腳本對白；人工看片要確認字幕與實際配音對得上。QC 沒過一律 `failed`，通過也只是 `pending_review`，審核與上傳（永遠私人）完全沿用原流程。

### 手動操作

```bash
# dry-run：只產 plan.json（呼叫 LLM，不需要啟用 H3、不用 GPU）
videoMaker make "點餐時說想吃什麼" --kind kids_english_dialogue --dry-run
videoMaker make "狗狗下班前一分鐘" --kind animal_sitcom --dry-run
videoMaker make "找不到的氣球" --kind dialogue_animation --dry-run
# 夜間排程 dry-run（週三／四／五各自的 --kind；權重沒裝好或 H3 關閉時只會看到 skipped）
videoMaker nightly --kind kids_english_dialogue --dry-run
videoMaker nightly --kind auto --dry-run            # 依今天星期
# 只跑單一集（H3 權重已裝好；非夜間時段）
videoMaker make "狗狗下班前一分鐘" --kind animal_sitcom
# 只重做單一鏡頭 N（例：第 2 鏡）：刪該鏡輸出與成片相關檔，再續跑
cd work/<作業> && rm -f clips/02.mp4 final.mp4 thumb.jpg status.json && cd ../..
videoMaker make "狗狗下班前一分鐘" --kind animal_sitcom --resume
# 續跑失敗的 H3 作業（已完成的鏡頭會跳過）
videoMaker make "<原主題>" --kind <原 kind> --resume
# 只跑到某個階段
videoMaker make "<主題>" --kind animal_sitcom --stage clips
```

重做 fl2va 某鏡的關鍵幀要一併刪 `keyframes/02.png`。

### 啟用 H3 的前置條件

1. ✅ **使用者已放行 MiniMax H3（2026-10-22）**；`LICENSES.md` 已記錄授權全文與地區限制，`models.txt` 已列權重。公開影片前請自行評估地區限制。
2. 在 GX10 下載權重（官方 `Comfy-Org/MiniMax-H3`）：`minimax_h3_fl2va_pruned_int8_convrot.safetensors`、`minimax_h3_ref2va_pruned_int8_convrot.safetensors`（各約 19.5GB，diffusion_models）、`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`（約 14.6GB，text_encoders）、`minimax_h3_video_vae_int8_convrot.safetensors`、`minimax_h3_audio_vae_fp32.safetensors`（vae）。**NAS 的 H3 檔案（`/volume1/comfyUI-model`）2026-10-22 起可用**，GX10 執行 `scripts/nas_pull_h3.sh`（限速、驗 sha256 對 HF；NAS 沒有 int8_convrot 的 video VAE，那個用 `scripts/download.sh minimax_h3_video_vae_int8` 從 HF 下載）。純 HF 下載：`ssh gx10 'cd ~/code/videoMaker && nohup scripts/download.sh MiniMax-H3 >> log/h3-download.log 2>&1 &'`（限速、可續傳、驗 sha256，約 55GB）。
3. 下載完成後 `videomaker check` 確認模型都看得到（H3 預設啟用，不需設環境變數；`VMK_H3_ENABLED=0` 才是關閉）。
4. 在非夜間時段手動實跑一支（`make … --kind animal_sitcom`），量記憶體峰值與每鏡耗時，校準 `VMK_H3_EXTRA_GB`、`VMK_H3_SEC_PER_SEC`，並確認 workflow（取樣器、步數、解析度 `VMK_H3_SIZE`）與提示詞寫法的實際效果。
5. ✅ `characters build` 已產生參考圖（2026-10-22），人工檢查通過後才用於正式排程。
6. 檢查 YouTube 設定：動物短劇與有對白的動畫已明確送 `selfDeclaredMadeForKids = false`；`containsSyntheticMedia` 只在照片模式為 True，H3 輸出是 AI 合成的影音，公開前請在 Studio 自行確認；分類目前一律是 Music，對白類公開前請改。

---

## 夜間排程

GX10 的 crontab（**新排程已於 2026-10-22 安裝**；舊的 `1,3`／`2,4`／週五 `weekly` 三行備份在 GX10 的 `~/crontab.backup-20261022`；實際以 `ssh gx10 crontab -l` 為準）：

```
# videoMaker 夜間排程：週一兒童音樂、週二小知識、週三兒童英文對話、週四動物短劇、週五有對白的動畫；19:30 開跑、07:00 前結束，生成完接著合併同一種內容
30 19 * * 1-5 cd /home/ehomeservice/code/videoMaker && flock -n /tmp/vmk-nightly.lock env VMK_COMFY_URL=http://127.0.0.1:8188 VMK_LLM_URL=http://127.0.0.1:11434/v1 .venv/bin/python -m videomaker nightly --kind auto >> /home/ehomeservice/code/videoMaker/log/vmk-nightly.log 2>&1
```

H3 預設啟用，不用在 `env` 加東西；要暫時關掉 H3 就在 `env` 後面加上 `VMK_H3_ENABLED=0`。舊排程（回滾用）：

```
30 19 * * 1,3 … nightly --kind kids …
30 19 * * 2,4 … nightly --kind trivia …
30 19 * * 5   … weekly …
```

行為：

- **週一到週五**每晚 19:30 啟動，`--kind auto` 依星期選今晚的內容種類（`nightly.SCHEDULE`），週末不跑；`--deadline 07:00` 前必須結束。兒童音樂預設 short、long、xl 各 1 支；小知識只有 short、long；對白類每晚 2 支 short（`nightly.DIALOGUE_PER_NIGHT`）。都由短到長依序執行；時間不夠時被略過的是最耗時的那支。
- 主題由 LLM 想：繁體中文短語，不得與 `history.jsonl` 和現有作業的主題重複，避開牙齒、嘴巴、飲食、水、洗澡、細軟物體這類容易畫壞的題材（僅兒童音樂；對白類的角色會說話，不套用）。
- 每支開跑前預估耗時，「現在 + 預估」超過 deadline 就略過（記成 `skipped`），避免拖到白天占用 GPU。
- 每支（含重試）開跑前先用 `nvidia-smi` 量 GPU 溫度，高於 `VMK_COOL_TEMP`（預設 45°C）就每 30 秒再量一次，降下來才開跑；等到「再不開跑就趕不上 deadline」仍沒降下來，該支記成 `skipped`。讀不到溫度（沒有 nvidia-smi）時不等。
- 單支失敗：等 10 分鐘用 `--resume` 重試一次；仍失敗記成 `failed`，繼續下一支，不會整晚中斷。
- **生成結束後合併同一種內容**（`nightly.merge_after`）：不用 GPU、用 ffmpeg `-c copy`；失敗只記錄，不影響當晚結果。合併規則見上方「每天的流程」。來源視窗：所有種類都只看近 6 天（同一週，不用滑動視窗）。兒童音樂與小知識合併當晚的 short 與 long（xl 不加入）；對白類每晚產 2 支 short，合併這一對。**合併發生在你審核當晚新片之前**：合輯裡可能包含還沒看過的新片，之後你若把其中一支按不通過，合輯不會自動更新——合輯本身要另外人工核准，看片時請連同來源一起確認。
- 每支的主題、片型、內容種類（`kind`／`content_type`）、`renderer`、開始／結束／耗時、`retry_count`、狀態、輸出路徑、QC 結果、GPU 生成秒數、使用的模型、錯誤都記在 `work/history.jsonl`；H3 另記 `h3_mode`、`shot_count`、`seconds_generated`、`generation_seconds`、`gen_sec_per_video_sec`（之後估算耗時用）。合併結果也記一筆（`type: "merge"`，含 `merge_id`、來源、`status` = merged／skipped／failed 與原因）；它們帶 `topic`／`format`／`kind`，舊的讀取程式不會壞，且 `weekly.sources` 會跳過它們。舊紀錄的欄位完全不變。
- **不會自動上傳 YouTube**（要你核准），也不再存 NAS。
- `flock` 保證同一時間只有一個夜間任務；手動執行 `make` 不受它保護，**19:30 之後不要同時手動跑 GPU 工作**（會搶記憶體，夜間任務會因 `剩餘記憶體 … < 40GB` 失敗）。

檢查 history：`ssh gx10 'tail -20 ~/code/videoMaker/work/history.jsonl | python3 -m json.tool --no-ensure-ascii'`；只看合併：`grep '"type": "merge"' work/history.jsonl`；看 H3 每秒成本：`grep gen_sec_per_video_sec work/history.jsonl`。

停用：`crontab -e` 刪掉那一行，或用上面的舊排程三行還原（舊單行備份在 `~/crontab.backup-20261016`，舊三行備份在 `~/crontab.backup-20261022`）。

---

## YouTube 上傳與 token

### 一次性設定（已完成）

專屬 Google 帳號 → 建頻道、手機驗證、觀眾預設「為兒童打造」→ 同帳號建 Google Cloud 專案、啟用 YouTube Data API v3 → OAuth 同意畫面（External、範圍只勾 `youtube.upload`）→ 建 OAuth 用戶端（Desktop app）→ 下載 `client_secret.json` 放到 Mac 的 `~/.config/videomaker/`（權限 600）→ `uv run python -m videomaker auth` 在瀏覽器授權 → 把 `token.json` 複製到 GX10 的同路徑（目錄 700、檔案 600）。

### 為什麼會過期、怎麼處理

OAuth 同意畫面只要還是 **Testing** 狀態，refresh token 7 天就失效，上傳會報 `invalid_grant`。Mac 的 LaunchAgent 每天 09:00 與每次登入時自動檢查：

1. 向 GX10 查 token：實際用 refresh token 向 Google 換一次（過期或被撤銷才會失敗）。
2. 正常 → 靜默結束。
3. 過期 → 跳出對話框「…要現在重新授權嗎？」，按「稍後再說」就結束（隔天再問）。
4. 按「重新授權」→ 開瀏覽器授權（請選專屬帳號；若 Mac 本機 token 本來就有效則跳過授權）→ 自動複製到 GX10 → 重新驗證 → 通知結果。
5. 連不上 GX10 → 只發通知。

手動觸發：`uv run python -m videomaker renew-token`。

LaunchAgent 檔案：`~/Library/LaunchAgents/com.videomaker.tokencheck.plist`（備份在 `scripts/com.videomaker.tokencheck.plist`）。

```bash
# 查狀態與日誌
launchctl print gui/$(id -u)/com.videomaker.tokencheck | grep -E "state|runs|last exit code"
cat ~/Library/Logs/videomaker-token.log
# 移除
launchctl bootout gui/$(id -u)/com.videomaker.tokencheck && rm ~/Library/LaunchAgents/com.videomaker.tokencheck.plist
```

要根除每週重新授權，只能把 OAuth 同意畫面改成 In production（Google Cloud Console）。

### 限制

- 專案沒通過 Google API 稽核，`videos.insert` 上傳的影片一律被強制為私人；要自動公開必須另外通過稽核，目前不做。
- 目前 token 的範圍只有 `youtube.upload`，**無法修改或刪除已上傳影片**（`videos.update` 需要 `youtube`、`youtube.force-ssl` 或 `youtubepartner`）。要改已上傳影片的標題或說明，請在 Studio 手動改。
- 上傳額度：Video Uploads 每天 100 次、每次算 1，一天 3 支遠低於上限。
- token 超過 6 個月沒使用或改密碼也會失效。

---

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `VMK_COMFY_URL` | `http://10.200.200.200:8188` | ComfyUI；在 GX10 上用 `http://127.0.0.1:8188` |
| `VMK_LLM_URL` | `http://10.200.200.200:11434/v1` | Ollama 的 OpenAI 相容端點；在 GX10 上用 `http://127.0.0.1:11434/v1` |
| `VMK_LLM_MODEL` | 空值（依內容種類） | 手動覆寫分鏡、提示詞與 YouTube 文案的企劃模型；不影響主題模型。通常保持未設定，以免增加模型切換 |
| `VMK_KIDS_TOPIC_MODEL` | `qwen3.8:27b` | 兒童音樂的主題與分鏡企劃共用模型，手動 `make` 也適用 |
| `VMK_TRIVIA_TOPIC_MODEL` | `gemma4:31b` | 小知識的主題與分鏡企劃共用模型，手動 `make --kind trivia` 也適用；維基百科來源驗證照常執行 |
| `VMK_WORK` | `<專案>/work` | 作業資料夾根目錄 |
| `VMK_MIN_FREE_GB` | `40` | 統一記憶體剩餘低於此值就中止該階段。兒童音樂的 FLUX.3 關鍵幀階段多要 `pipeline.FLUX3_EXTRA_GB`（8GB，即預設 48GB）：2026-10-20 實測相對基線的記憶體峰值 FLUX.3＋upsampler 37.4GB、Wan 29.6GB，8GB 是兩者差距，讓兩個階段都保有約 10GB 餘裕 |
| `VMK_COOL_TEMP` | `45` | 夜間排程每支開跑前，GPU 要降到此溫度（°C）以下才開始；閒置實測約 36-37°C |
| `VMK_FONT` | `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` | 小知識英文字幕用的 TTF；檔案不存在時 `mux` 會直接報錯 |
| `VMK_STEPS` | `20` | Wan 取樣步數。實測 40 步與 20 步細節看不出差別，卻慢 1.85 倍，所以維持 20 |
| `VMK_CHARACTERS` | `<專案>/characters` | 固定角色庫（對白類） |
| `VMK_H3_ENABLED` | `1` | 設 `0` 關閉 H3 renderer（`h3_fl2va`／`h3_ref2va`）；MiniMax H3 於 2026-10-22 放行，授權限制見 `LICENSES.md` |
| `VMK_H3_EXTRA_GB` | `25` | H3 階段相對 `VMK_MIN_FREE_GB` 再多要的剩餘記憶體（GB）；未實測的保守值 |
| `VMK_H3_SIZE` | `576x1024` | H3 生成解析度（9:16、32 的倍數），`mux` 再放大裁切到 1080×1920 |
| `VMK_H3_STEPS` | `20` | H3 取樣步數 |
| `VMK_H3_SEC_PER_SEC` | `90` | 沒有 history 時，H3 每產出 1 秒成片的保守生成秒數（有資料後改用最近 10 支的中位數） |
| `VMK_RENDERER_<KIND>` | 空值（用預設） | 覆寫某對白類內容的 renderer，如 `VMK_RENDERER_ANIMAL_SITCOM=h3_fl2va` |
| `VMK_YT_DIR` | `~/.config/videomaker` | `client_secret.json` 與 `token.json` 的資料夾 |
| `VMK_FB_DIR` | `~/.config/videomaker` | FB 的 `facebook.json` 憑證目錄，必須在專案外；TUI 使用 GX10 上的設定 |
| `VMK_FB_API_VERSION` | `v25.0` | Facebook Graph API 版本 |
| `VMK_GX10` | `gx10` | `renew-token` 與 TUI 連 GX10 用的 ssh 別名 |
| `VMK_LOCAL_DIR` | `~/Downloads/videoMaker` | TUI 下載到 Mac 的位置 |

---

## 作業資料夾結構

```
work/2026-10-05_feng-ling-qing-xiang/
  plan.json         企劃（style、character、prop、shots、layout、雙語 youtube 欄位、seed）
  meta.json         證據：模型檔名、seed、完整提示詞、設定／實測 BPM、切點、各階段耗時、git hash、YouTube ID
  status.json       審核狀態：pending_review → approved／rejected（QC 沒過是 failed），含 QC 各項結果
  visual_qc.json    clef-flash 逐張抽樣結果、最高疑慮位置、模型 digest、問題、素材指紋與續跑進度
  beats.json        拍點偵測結果與切點
  music.wav         音樂（music_raw.flac 是 ComfyUI 原始輸出）
  keyframes/NN.png  關鍵幀
  clips/NN.mp4      Wan 生成的影片（對白類：H3 生成、帶對白音訊）
  source/           （照片模式）你的原始照片副本
  timeline.json     （對白類）每個鏡頭在成片的起點與長度；視覺 QC 用它決定檢查區段
  subtitles.json    （對白類）每句字幕的起訖時間與文字（依詞數分配的估計時間）
  final.mp4         成片 1080×1920、24fps、AAC 48kHz
  thumb.jpg         縮圖
  run.log           執行日誌
```

QC 自動檢查（任一失敗標 `failed`）：解析度 1080×1920、片型長度、有音軌、響度 -14 ±1.5 LUFS、沒有 0.5 秒以上的黑畫面，加上視覺抽樣。**人工審核仍是必經關卡。**

視覺檢查使用 GX10 Ollama 0.40.4 以上的 `clef-flash:9b`（`ollama pull clef-flash:9b`），端點沿用 `VMK_LLM_URL` 的 Ollama 主機。每秒抽 2 張、縮至寬 540 像素，檢查兒童內容疑慮與明顯變形；照片模式另查多餘人手／道具。只查獨特鏡頭中 `mux` 實際放進成片的區段（`pipeline.slot_windows`，與合成共用同一份計算），沒進成片的畫面不查；缺少原始鏡頭時改查整支成片。結果同時記錄原始鏡頭內的約略秒數（`time`）與成片時間（`final_time`，鏡頭重複出現時取第一次）。QC 的基本檢查（解析度、長度、音軌、響度、黑畫面）任一未通過時，直接標 `failed` 並略過視覺檢查，不佔用 GPU。

任一疑慮分數達 20% 就標為有疑慮（2026-10-16 由 50% 降下來：實測身體融合、肢體變成色塊的明顯缺陷畫面只得 38%，50% 會放行）；這是初篩門檻，不是影片有問題的實際機率。2026-10-16 的初步校準（80 張人工標記，其中 3 張有問題）：只有身體融合那張明顯缺陷拿到 38%；另外兩張較細微的（1 張排第 6、1 張排第 60）分數是 12% 與 6%，門檻要降到 5～7% 才抓得到，但會標出 6～9 成的畫面，不可行。所以維持 20%，視覺 QC 只能擋明顯壞掉的畫面，細微缺陷仍靠人工看片；低分隨機樣本（40 張）裡有 1 張有問題，代表低分畫面也有缺陷，偵測器看不到。門檻只影響判定、不影響分數：調整後，TUI 與週五合併對舊作業依已存的分數重新判定（`visual_qc.restate`），但不改 `status.json`、不重設人工審核。Ollama 離線、模型缺少、記憶體不足或回傳無效分數都視為檢查失敗，不能當成通過。檢查前先釋放 ComfyUI 模型並遵守 `VMK_MIN_FREE_GB`，結束後卸載 clef-flash。

逐張結果會原子寫入 `visual_qc.json`；中斷後 `--resume` 沿用已查畫面，已查完的來源不再重新抽幀；素材、取用區段、模型 digest、問題或抽樣設定（取樣率、縮放、品質、提示詞）改變就重查。疑慮門檻不在其中：調整門檻只依已存的分數重算狀態，不重跑 GPU。Ollama 離線等錯誤發生在取得本次指紋之前時，既有結果原樣保留，只標記錯誤。檢查行程被強制中斷時，`visual_qc.json` 會停在 `running`；超過 10 分鐘（`jobs.ACTIVE_SECONDS`）沒有進度，TUI 與盤點改顯示「檢查失敗」。`meta.json` 的 `models.visual_qc` 只在確實有畫面送進模型時才寫入，並附 digest（`clef-flash:9b@<digest>`）。視覺檢查錯誤會讓夜間任務照原規則重試；其他已完成的階段與人工審核不會因此重做。既有作業若要補查，先備份並移走該作業的 `status.json`，再用原主題 `make "主題" --resume --kind <原內容種類> --format <原片型>`；這會重新等待人工核准，請只對要重新審核的作業操作。

**校準門檻**：先抽出要人工標記的畫面，再用標記結果決定門檻。

**舊校準資料需重做**：上面的 80 張初步數據使用舊 `fps` 抽幀時間，標記頁面與模型實際評分的畫面可能相差約 0.21 秒，不能用來證明現行門檻的準確度。新抽樣記錄實際選中幀的時間，`sampling=selected-pts-v1` 納入快取指紋；校準工具會略過舊抽樣報告，請在指定作業重新做 QC 後匯出並人工重標。這次更新不會自動重查舊作業或改門檻。

```bash
# GX10：抽分數最高的 40 張 + 其餘隨機 40 張，截圖並產生盲標頁面（不顯示分數與作業名稱、順序打亂）
ssh gx10 'cd ~/code/videoMaker && .venv/bin/python -m videomaker visual-labels ~/code/videoMaker/labels --top 40 --random 40'
# Mac：下載並用瀏覽器開啟，按 1 沒問題／2 有問題／3 不確定，最後按「匯出 labels.csv」
rsync -a gx10:code/videoMaker/labels/ ~/Downloads/videoMaker-labels/ && open ~/Downloads/videoMaker-labels/index.html
```

`manifest.json`（編號 → 作業、來源、時間、分數）留在資料夾裡，標記完把 `labels.csv` 放進同一個資料夾，就能對照出各門檻的漏判與誤報。標記存在瀏覽器的 localStorage，關掉再開會接著標。

合輯不跑 GPU，而是彙整各來源的視覺結果；任一來源未檢查、失敗或有疑慮，合輯就不會標為 QC 通過。下載到 Mac 會一併帶回 `visual_qc.json`，刪掉 GX10 存檔後仍可在詳情查看。

合輯的疑慮位置會加上前面來源影片的長度；舊來源缺少長度時只顯示來源內時間，避免指向錯誤的合輯位置。

---

## 企劃內容規則

企劃（`plan.json`）由 LLM 產生並用程式驗證，不合格會把原因丟回 LLM 重寫，最多 3 次。被退回時日誌會出現 `企劃第 N 次驗證失敗，請 LLM 重寫：…`。

| 規則 | 原因 |
|---|---|
| style、character、每鏡提示詞必須純英文 | 中文進到 FLUX 會被畫成亂碼文字 |
| character 至少 6 個英文字（物種、顏色、固定特徵） | 太短就無法跨鏡維持角色一致 |
| 道具（`prop`）必須寫顏色，且各鏡提到該道具時顏色不能不同；程式把「holding `<prop>`」固定附在每鏡前面 | 避免各鏡牙刷一下藍一下粉 |
| 禁用高風險字眼：teeth、tooth、mouth、lips、tongue、grin、floss、string、ribbon、rope、hose、cable、water、liquid、pour、splash、bubbles、foam、soap、toothpaste、bend、curve、stretch、twist 等（`toothbrush` 本身可以） | 牙齒與嘴巴會畫成怪牙；細長會彎的物體在影片裡會變形；液體與泡沫會糊成一團 |
| 禁用文字類字眼：text、letters、sign、label、poster、writing、logo、caption 等 | 畫面不要出現 AI 生成的文字（小知識的英文字幕是 `mux` 時用 ffmpeg 疊上去的，不受影響） |
| 禁用現有角色、品牌與「in the style of」 | 版權；清單只是第一道防線，另有人工審核 |
| YouTube 標題 `繁體中文｜English title`（≤100 字元）；說明為中文兩句＋英文兩句＋3 到 5 個英文 hashtag；標籤至少 3 個英文 | 英文增加觸及率；超過 100 字元英文會被截斷 |
| YouTube 欄位與主題不得含簡體字 | 偵測用常見簡體專有字清單，不是完整簡繁轉換，冷門字可能漏網 |
| 長片 `layout` 的鏡頭 id 必須存在 | — |

---

## 模型與下載

白名單與授權依據見 `LICENSES.md`；只用以下模型：

| 用途 | 模型 | 檔名 |
|---|---|---|
| 音樂 | ACE-Step 1.5 | `ace_step_1.5_turbo_aio.safetensors` |
| 關鍵幀（小知識） | FLUX.1 schnell | `flux1-schnell-fp8.safetensors` |
| 關鍵幀（兒童音樂，白名單外，你於 2026-10-20 明確放行；FLUX Kommunity License，年營收須低於 US$5M） | FLUX.3 Image Klein + prompt upsampler | `flux3-image-klein-bf16.safetensors`、`qwen3vl_4b_bf16.safetensors`、`qwen3.5_9b_flux3_prompt_upsampler_bf16.safetensors`、`flux2-vae.safetensors`（約 38GB；upsampler 一定要用，擴寫後的 caption 記在 `meta.json` 的 `shots[].upsampled`） |
| 影片 | Wan 2.2 TI2V-5B | `wan2.2_ti2v_5B_fp16.safetensors`、`umt5_xxl_fp8_e4m3fn_scaled.safetensors`、`wan2.2_vae.safetensors` |
| 對白類影片＋音訊（白名單外，你於 2026-10-22 明確放行；授權排除美國、歐盟、英國、南韓） | MiniMax H3 | `minimax_h3_fl2va_pruned_int8_convrot.safetensors`、`minimax_h3_ref2va_pruned_int8_convrot.safetensors`、`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`、`minimax_h3_video_vae_int8_convrot.safetensors`、`minimax_h3_audio_vae_fp32.safetensors` |
| 兩隻狗合成（白名單外，你於 2026-10-05 明確放行） | Qwen-Image-Edit-2509 | `qwen_image_edit_2509_fp8_e4m3fn.safetensors`、`qwen_2.5_vl_7b_fp8_scaled.safetensors`（7B，不可換 3B／72B）、`qwen_image_vae.safetensors` |

**對白類的 H3 renderer（`h3_fl2va`／`h3_ref2va`）已於 2026-10-22 放行**：授權排除美國、歐盟、英國、南韓，細節在 `LICENSES.md`（見[啟用 H3 的前置條件](#啟用-h3-的前置條件)）。

**禁用**：FLUX.1 dev、MiniMax H3 的 Turbo LoRA／ControlNet／embedding（H3 主模型已放行）、任何 RIFE TensorRT 節點、LTX-2.x、Stable Audio 3、白名單外的模型與 LoRA（NAS 的 `/volume1/comfyUI-model` 內的 LTX-2.3、FLUX.2 檔案不得用於本管線；H3 檔案可用，需通過 sha256 驗證）。路線 A（Wan 14B、Lightning、RIFE）已取消並移除。

限速下載（不用 `comfy-get-model.sh`，因為它底層的 `hf download` 多連線、無法限速，會佔滿同區網頻寬）：

```bash
scripts/download.sh <pattern> [限速，預設 2M]    # 例：scripts/download.sh flux1-schnell 2M；不限速用 0
```

- `pattern` 比對 `models.txt` 的行；每行格式是「HF 路徑 ComfyUI 資料夾」，資料夾一律明確指定。
- 先寫 `.part`（ComfyUI 看不到半成品）、可續傳、下載完比對 HF 的 sha256 才改成正式檔名，不符改名成 `.bad`。
- 在 GX10 上用 tmux 或 `at` 跑：`tmux new -d -s vmk-dl "cd ~/code/videoMaker && scripts/download.sh <pattern> 2M 2>&1 | tee -a ~/code/videoMaker/log/vmk-dl.log"`。
- 下載後用 `videoMaker check` 確認 ComfyUI 看得到。實測線路約 0.8MB/s（限速值不是瓶頸）。

---

## 部署與開發

```bash
# Mac：開發與測試（專案固定使用 Python 3.12）
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest -q

# 部署到 GX10（不帶 venv、作業資料、照片）
rsync -a --exclude .venv --exclude work --exclude __pycache__ --exclude .pytest_cache \
      --exclude .ruff_cache --exclude .git --exclude photo --exclude photos \
      ./ gx10:/home/ehomeservice/code/videoMaker/

# GX10：驗證（GX10 用 pip 的 venv，不是 uv）
ssh gx10 'cd ~/code/videoMaker && VMK_COMFY_URL=http://127.0.0.1:8188 .venv/bin/python -m pytest -q -p no:cacheprovider'
```

- 套件新增依賴後，GX10 的 venv 要手動 `pip install`（例如 `google-api-python-client`、`google-auth-oauthlib` 已裝）。
- 設定的 workflow 是 ComfyUI API 格式，放在 `workflows/*.json`；要替換的節點欄位記在 `workflow_map.yaml`。
- GX10 沒有 git repo（rsync 不帶 `.git`），所以 `meta.json` 的 `git` 欄位在 GX10 上是 `no-commit`。
- **ComfyUI 版本釘在 v0.43.0**（2026-10-27 由 v0.41.3 升級，為了 MOSS-Image 原生支援；v0.41.3 是 2026-10-20 由 v0.33.0 升級，為了 FLUX.3 的 `CLIPLoader type=flux3`）：`~/code/comfyui-docker/Dockerfile` 的 `ARG COMFY_REF`，要升級就改它再 `docker compose build && docker compose up -d`（先確認佇列空、不在夜間時段）。升級後 `CLIPLoader` 仍有 `flux3`，既有 `workflows/*.json` 的節點都在；兒童音樂 short 已實跑通過（2026-10-27 `little-bear-brushing-song`，clips 1680.9s，與升級前相當，QC 全過），小知識（schnell）與兩狗合成（Qwen-Image-Edit）**尚未實跑驗證**。回滾：把 `comfyui-docker-comfyui:pre-v043-20261027`（v0.41.3）標回 `latest` 後 `docker compose up -d`；更早的 v0.33.0 是 `pre-flux3-20261020`。`custom_nodes/ComfyUI-LTXVideo` 在新版 import 失敗，LTX 本來就在禁用清單、沒有 workflow 用到，不用修。
- `test_workflows_match_live_object_info` 會展開動態 combo（`TextGenerate` 的 `sampling_mode.*` 子欄位）與動態長出的輸入（H3 的 `ref_images.ref_image_N`）。
- **回滾（新排程與對白類）**：改回舊 crontab 三行（見[夜間排程](#夜間排程)）即可恢復舊行為；程式本身向下相容——舊 `plan.json`、舊 `history.jsonl` 都能讀，`nightly --kind kids|trivia` 與 `weekly` 指令照舊可用，`git revert` 該次提交也能完整退回。已產生的對白類作業與合併作業不受影響（可從 TUI 刪除）。

---

## 疑難排解

| 現象 | 原因與處理 |
|---|---|
| `剩餘記憶體 NGB < 40GB，中止`（兒童音樂關鍵幀階段是 48GB） | 統一記憶體被別的工作占用（例如另一個 ComfyUI 任務）。每個 GPU 階段開始前會先卸載模型並最多等 30 秒。夜間任務會等 10 分鐘重試一次；手動跑則等別的工作結束再跑。**不要降低 `VMK_MIN_FREE_GB` 硬跑** |
| `連不上 ComfyUI` | `ssh gx10 'docker ps \| grep comfyui'`；容器沒起來就啟動它 |
| `… 必須先人工核准（videomaker approve）才能上傳` | 還沒 `videoMaker approve <資料夾>` |
| 上傳報 `invalid_grant` | token 過期，跑 `uv run python -m videomaker renew-token` |
| `音樂 3 次都不合格` | 實測 BPM 與設定差超過 5%，或音樂在最後一個切點前就結束（ACE-Step 會提早演完、後面是靜音，程式已多要 10 秒並驗證）。換 seed 重跑 |
| `plan 重試 3 次仍失敗` | 看日誌的 `企劃第 N 次驗證失敗` 找出被擋的規則；多半是 LLM 反覆寫出高風險字眼或沒有雙語 |
| 夜間排程沒跑 | `ssh gx10 'crontab -l \| grep videomaker; tail -40 ~/code/videoMaker/log/vmk-nightly.log'`；若有人手動跑 `make`，`flock` 不會擋，但會搶記憶體 |
| 夜間某支 `failed` | 看 `work/history.jsonl` 的 `error`；修好後 `videoMaker make "<同一個主題>" --resume --kind <原內容種類> --format <原片型>` 可接續（已完成的階段不會重做，結果需自己 `videoMaker approve` 與上傳） |
| 想重做某個階段 | 刪掉該階段的輸出檔再 `--resume`（例如刪 `final.mp4` 與 `thumb.jpg` 重新合成；重新合成會刪掉 `status.json`，QC 會重跑） |
| Mac 的 token 日誌沒有輸出 | `launchctl print gui/$(id -u)/com.videomaker.tokencheck`；沒有就重新安裝 plist |

---

## 已知限制

- 畫質上限是 Wan 5B 的 704×1280，再放大到 1080×1920。多步取樣沒有幫助；要突破需要 AI 放大模型（不在白名單，需你放行）。
- 影片裡的物體仍可能變形（牙刷、四肢）；clef-flash 每秒 2 張的抽樣可能漏掉短暫異常，也沒有量測閃爍或動作連貫性，必須人工看片。
- 兩隻狗同框的長相只是「大致像」。
- 所有影片都是 120 BPM，變化只靠音樂風格與調性。
- 過期 token 的「視窗 → 授權 → 自動複製」整條流程沒有端到端實跑過（各環節分開驗證過）。
- 已上傳影片的資料無法用目前的 token 修改。
- 音樂的純樂器、無人聲只能靠人工聽。
