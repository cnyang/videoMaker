# 兒童音樂 Shorts 自動生成管線｜Phase 1 實作規格（v2，2026-10-02 查證修訂）

> **歷史文件，部分已過時。** 現況以 [`README.md`](README.md)、[`AGENTS.md`](AGENTS.md)、[`docs/operations.md`](docs/operations.md) 為準。
> 與現況不同處：路線 A（Wan 14B／RIFE／Lightning）已取消，只用 TI2V-5B（沒有 `--route`）；成品不再存 NAS（§5.9）；
> 之後新增了 long／xl 片型、照片模式、夜間排程、週五合併、YouTube 上傳與 TUI。
> 仍有效：§2 硬性限制與授權、§7 GX10 注意事項、§11 工作規則。

給 Claude Code（Mac 10.1.1.241）使用。請先讀完整份文件，再開始實作。遇到「待確認」項目，先實際檢查環境，查不到再問使用者。

## 1. 目標

輸入一個主題（例如「小熊刷牙」），自動產出一支 **15–20 秒、直式 9:16、純音樂、無旁白** 的兒童 YouTube Shorts，存到 NAS。

Phase 1 **只做到存 NAS**，不接 YouTube。目的是跑出一支成品，量出各階段耗時與畫質。

## 2. 硬性限制

- **所有模型必須可商用**，且成品會在 YouTube 對全球公開。頻道為個人名義。
- 優先選擇沒有營收門檻、也沒有地區限制的授權（Apache 2.0 / MIT）。
- 內容為兒童向，完全由 AI 生成。
- 不使用旁白、TTS、歌詞。音樂為純樂器。
- 不要把任何金鑰、token 寫進程式碼或 git。

### 2.1 授權白名單（2026-10-02 查證）

| 用途 | 模型 | 授權 |
|---|---|---|
| 音樂 | ACE-Step 1.5（含 XL） | MIT |
| 影片 | Wan 2.2 I2V-A14B、TI2V-5B | Apache 2.0 |
| 影片加速 | lightx2v/Wan2.2-Lightning（4-step LoRA） | Apache 2.0 |
| 關鍵幀 | FLUX.1 [schnell] | Apache 2.0 |
| 關鍵幀 | SDXL base 1.0 | CreativeML OpenRAIL++-M |
| 補幀 | Comfy-Org/frame_interpolation（RIFE v4.25/4.26、FILM） | 標示為 MIT 與 Apache-2.0 |

### 2.2 禁用清單

| 模型 | 原因 |
|---|---|
| **FLUX.1 [dev]** | 非商用授權 |
| **MiniMax H3**（含其 LoRA、衍生模型） | 授權禁止在美國、歐盟、英國、南韓使用或展示其輸出，YouTube 全球公開即違反 |
| **任何 RIFE TensorRT 節點**（ComfyUI-Rife-Tensorrt 及其分支） | CC BY-NC-SA，非商用 |
| LTX-2.x、Stable Audio 3 | 有營收門檻的社群授權；目前不需要，不使用 |
| 白名單以外的任何模型、LoRA | 先停下來問使用者 |

## 3. 環境

| 機器 | IP | 角色 |
|---|---|---|
| Mac | 10.1.1.241 | 開發、Claude Code；SSH 到 GX10 的設定已在 `~/.ssh/config` |
| GX10 | 10.200.200.200 | ComfyUI（docker）、生成運算；ARM64 + 128GB 統一記憶體 |
| NAS | 10.100.100.100 | 成品儲存，約 4TB 可用 |

**待確認（請實際檢查）：**
1. ComfyUI 的容器名稱、版本、對外 port（預期 8188）、是否以 `--listen 0.0.0.0` 啟動。
2. ComfyUI 目前已有哪些模型與自訂節點；template 庫中是否有 ACE-Step 1.5 與 Wan 2.2 的官方 workflow。
3. GX10 能否連到 NAS（兩者網段不同）。能就由 GX10 直接 rsync 或掛載；不能就由 Mac 中繼拉取。
4. NAS 的掛載點或 rsync 目標路徑。
5. GX10 上有哪個 LLM 服務（OpenAI 相容端點）可用來產企劃。

## 4. 執行位置

長任務**不要依賴 Mac 的 SSH 連線**，Mac 螢幕保護程式啟動時連線會中斷。

- 編排器放在 GX10 上，用 `systemd-run`、`at` 或 tmux 執行。
- Mac 只負責開發與觸發。
- 每個階段必須**可續跑**：輸出檔已存在就跳過。

## 5. 流程

```
主題
 └─ [1] LLM 產企劃 plan.json
     └─ [2] ACE-Step 生成音樂（比成片多 2 秒）
         └─ [3] 偵測實際拍點，決定每鏡切點
             └─ [4] 每鏡生成關鍵幀（FLUX.1 schnell 或 SDXL）
                 └─ [5] 每鏡圖生影片（Wan 2.2）
                     └─ [6] 補幀（僅 14B 路線需要）
                         └─ [7] ffmpeg 合成 final.mp4
                             └─ [8] QC 檢查 → pending_review
                                 └─ [9] 存 NAS
```

### 5.1 企劃 plan.json

由 LLM 產出，用 pydantic 驗證，失敗最多重試 3 次。

```json
{
  "topic": "小熊刷牙",
  "slug": "bear-brush-teeth",
  "style": "flat 2D cartoon, soft pastel colors, rounded shapes, kid-friendly",
  "character": "a round brown bear cub with a red scarf, big friendly eyes",
  "music": {
    "caption": "cheerful kids instrumental, ukulele, glockenspiel, light percussion, playful",
    "bpm": 120,
    "time_signature": "4/4",
    "key": "C major"
  },
  "shots": [
    {"id": 1, "prompt": "the bear cub wakes up in a cozy bedroom, stretching"}
  ],
  "youtube": {"title": "", "description": "", "tags": []}
}
```

規則：
- **BPM 限制在 100–140**（4/4）。這個範圍內「2 小節＝1 鏡」為 3.4–4.8 秒，不會超過 Wan 單段約 5 秒的長度。
- `shots` 數量由程式依 5.3 計算後告訴 LLM，不讓 LLM 自己決定。
- `style` 與 `character` 必須**原樣加到每一鏡的提示詞前面**，維持角色與畫風一致。
- 禁止在 prompt 出現：現有卡通角色、品牌、藝術家名字、「in the style of」、歌曲名稱。
- 畫面避免真人兒童的寫實臉孔，使用擬人動物或風格化卡通角色。
- 畫面避免出現文字（AI 生成文字常出錯）。

### 5.2 音樂

- 模型：**ACE-Step 1.5**（MIT）。**優先使用 ComfyUI 原生的 ACE-Step 1.5 節點與官方 template**。
- 注意：`ace-step/ACE-Step-ComfyUI` 這個節點是呼叫外部 ACE-Step 推論伺服器，需要另外架服務。只在原生節點不能用時才考慮。
- 純樂器設定：
  - lyrics 填 `[Instrumental]`。這是 ACE-Step 1.5 官方推論文件的寫法。
  - 若節點有 `instrumental` 布林參數，一併設為 true。
- BPM、拍號、調性從 plan 帶入；caption 也加入 `instrumental`。
- 時長設為 **成片總長 + 2 秒**，多出的部分合成時裁掉並淡出。
- 同一主題可生成 2–3 個候選存起來，供人工挑選（Phase 1 可先取第一個）。

### 5.3 拍點與鏡頭長度

先以 BPM 計算理論值：

```
bar_sec   = 60 / bpm * 4
shot_sec  = bar_sec * 2                 # 2 小節一鏡，必須 ≤ 5.0 秒
n_shots   = floor(20 / shot_sec)        # 總長不超過 20 秒
total_sec = n_shots * shot_sec          # 必須 ≥ 15 秒，否則改用 bars_per_shot 或調整 n_shots
```

再用實際音訊修正：
- 用 **librosa** 的 `beat_track` 偵測實際 BPM 與拍點。不要用 ffprobe，它無法偵測節拍。
- 實測 BPM 與設定差距 > 5% 就重新生成音樂。
- **以偵測到的拍點作為切點**，不要只用理論網格。生成音樂的第一拍不一定落在 0 秒，音樂的起點要對齊第一個重拍。

### 5.4 關鍵幀

- 模型：FLUX.1 [schnell] 或 SDXL（見白名單）。**FLUX.1 [dev] 禁用。**
- 解析度與 5.5 所選的影片解析度相同（直式）。
- 同一支影片固定同一個 seed 基底，各鏡 seed = 基底 + 鏡頭編號。

### 5.5 圖生影片

兩條路線都要實作並實測，比較速度與畫質後擇一：

| 路線 | 模型 | 直式解析度 | 幀數 | 輸出 fps | 長度 |
|---|---|---|---|---|---|
| A（品質） | Wan 2.2 **I2V-A14B**（fp8 或 GGUF） | 720×1280 或 480×832 | 81 | 16 | 約 5.06 秒 |
| B（速度） | Wan 2.2 **TI2V-5B** | **只能用 704×1280** | 121 | 24 | 約 5.04 秒 |

- 幀數必須是 **4n+1**。
- 5B 官方只支援 704×1280 和 1280×704，不能隨意改寬高。
- 可用 **lightx2v/Wan2.2-Lightning 4-step LoRA**（Apache 2.0）加速；ComfyUI 官方 Wan 2.2 template 已內建這個選項。品質會略降，要和不加速的版本比較。
- 一次只跑一個任務。階段之間呼叫 ComfyUI：

```
POST http://<gx10>:8188/free
{"unload_models": true, "free_memory": true}
```

（部分版本路徑為 `/api/free`，兩個都試。）

### 5.6 補幀

- **路線 B（5B，24fps）不需要補幀**，直接以 24fps 輸出。
- **路線 A（14B，16fps）**：
  - 以 RIFE 2 倍補到 32fps，再用 ffmpeg `fps=30` 轉成 30fps。
  - 權重使用 **Comfy-Org/frame_interpolation** 重新打包的 RIFE v4.25/4.26（標示為 MIT/Apache-2.0）。先確認目前的 ComfyUI 是否已有能載入這些權重的原生節點；若沒有，用 ComfyUI-Frame-Interpolation，並先確認該節點包的 LICENSE 檔。
  - **禁止使用任何 RIFE TensorRT 版本**（CC BY-NC-SA）。
  - 授權注意：RIFE 的程式碼與權重為 MIT，但其訓練資料集可能是非商用授權，風險較低但不是零。若使用者希望零疑慮，用 ffmpeg `minterpolate` 或直接選路線 B。

### 5.7 合成

```
ffmpeg: 逐鏡依 5.3 的切點裁切 → concat → scale/pad 到 1080x1920
        → 音樂裁到 total_sec，最後 1 秒淡出 → loudnorm 約 -14 LUFS
        → libx264 yuv420p + aac 192k + faststart
```

鏡頭之間用硬切，落在拍點上。

### 5.8 QC 檢查

自動檢查，任一失敗就標記為 `failed`：
- ffprobe：解析度 1080×1920、長度 15–20.5 秒、有音軌。
- 響度在目標範圍內。
- `blackdetect`：不可有長時間黑畫面。
- 選配：抽 8–10 幀，用 GX10 上的視覺模型檢查肢體扭曲、畫面破碎、不適合兒童的內容。

**所有成片都必須經人工審核**。狀態以 `status.json` 表示：`pending_review` → `approved`。

### 5.9 NAS 結構

```
<NAS_ROOT>/ai-video/
  LICENSES.md                       # 以 2.1 白名單為初版，每次換模型就更新
  2026-10-02_bear-brush-teeth/
    plan.json
    music.wav
    keyframes/
    clips/
    final.mp4
    thumb.jpg
    meta.json
    status.json
```

`meta.json` 必須記錄：所有模型檔名與版本（含 LoRA）、seed、完整 prompt、設定 BPM 與實測 BPM、切點時間、各階段耗時、程式碼 git hash、生成時間。這是日後被質疑授權或版權時的證據。

## 6. ComfyUI 整合

- 把每個 workflow 以 **API 格式** 匯出，放在 `workflows/*.json`。
- 用 `workflow_map.yaml` 記錄要替換的節點 ID 與欄位（prompt、seed、解析度、幀數、BPM、時長、輸入圖片）。
- Python client：`POST /prompt` → 輪詢 `/history/{prompt_id}`（或 websocket）→ 用 `/view` 取回輸出。
- 逾時、重試、錯誤訊息都要處理，並寫入日誌。

## 7. GX10 注意事項

- **torchaudio 請用 CPU 版**。NGC 的自訂 CUDA 版本字串會讓 pip 安裝的 torchaudio 在執行時壞掉。安裝 ComfyUI 相依套件時，要過濾 `torch`、`torchvision`、`torchaudio`。ACE-Step 節點會用到 torchaudio。
- 記憶體頻寬約 273 GB/s，是主要瓶頸。
- 生成時會與其他載入的模型爭用統一記憶體，**先檢查剩餘記憶體**，不足時中止並回報，不要硬跑。

## 8. 介面

```
python -m videomaker make "主題" [--stage plan|music|keyframes|clips|mux|qc]
                                  [--route a|b] [--dry-run] [--resume]
python -m videomaker bench        # 各階段耗時基準
```

`--dry-run`：只產 plan.json，不呼叫 ComfyUI。

## 9. Phase 1 驗收

1. 輸入一個主題，能自動產出 1 支 `final.mp4` 並存到 NAS。
2. 規格符合：1080×1920、15–20.5 秒、有音軌、純樂器（無人聲）。
3. 鏡頭切換落在偵測到的拍點上（誤差 < 1 幀）。
4. 中斷後可續跑，不會重做已完成的階段。
5. `meta.json` 與 `LICENSES.md` 內容完整。
6. 產出一份耗時與畫質報告：各階段花費秒數、路線 A 與 B 的比較、有無 Lightning LoRA 的比較。

## 10. 後續階段（Phase 1 完成並人工確認品質後才做）

- **Phase 2：YouTube 上傳**
  - OAuth（scope：`youtube.upload`），token 存放在專案外。
  - 一律以 `private` 上傳。未通過 API 稽核的專案，影片會被強制為私人，因此由使用者在 YouTube Studio 人工審核後再手動公開。
  - 設定 `status.selfDeclaredMadeForKids = true`。
  - `status.containsSyntheticMedia`：用於寫實的竄改或合成內容；純卡通風格通常不需要，寫實風格才設為 true。
  - 標題、說明、標籤取自 plan.json。
- **Phase 3：批次與排程**
  - 主題佇列、每日產量上限、失敗重跑、完成通知。
  - 每支影片要有實質差異（旋律、角色動作、場景），避免被判定為大量套版內容。

## 11. 給 Claude Code 的工作規則

- 只使用 2.1 白名單內的模型。其他模型、LoRA、節點的權重，先查授權並停下來問使用者。
- 下載前再看一次授權頁，若與 2.1 不符，以授權頁為準並回報。
- 不要刪除 GX10 上既有的模型與容器；有疑慮先問。
- 每完成一個階段，回報輸出位置與耗時。
