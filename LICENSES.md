# LICENSES — 模型授權清單

初版取自 `kids-shorts-pipeline-spec.md` §2.1，**2026-10-02 已逐一核對授權頁**（HuggingFace 模型卡／LICENSE 與上游 GitHub）。
每次換模型、LoRA 或節點就更新，並同步到 NAS 的 `ai-video/LICENSES.md`。下載前再看一次授權頁，與本表不符時以授權頁為準並回報。
逐支影片實際使用的檔名見該支的 `meta.json`。

## 白名單模型（已核對）

| 用途 | 模型／檔案 | 授權 | 核對依據 |
|---|---|---|---|
| 音樂 | ACE-Step 1.5 → `ace_step_1.5_turbo_aio.safetensors`（Comfy-Org 重新打包） | MIT（上游）；重新打包頁標示 Apache-2.0，兩者皆寬鬆 | HF `ACE-Step/Ace-Step1.5` 卡片 `license: mit`、GitHub `ace-step/ACE-Step-1.5/LICENSE` 為 MIT；卡片明載可商用；`Comfy-Org/ace_step_1.5_ComfyUI_files` 標 apache-2.0 |
| 影片 | Wan 2.2 TI2V-5B → `wan2.2_ti2v_5B_fp16.safetensors` | Apache 2.0 | HF `Wan-AI/Wan2.2-TI2V-5B` apache-2.0；GitHub `Wan-Video/Wan2.2/LICENSE.txt` 為 Apache 2.0；重新打包 `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` apache-2.0 |
| 關鍵幀 | FLUX.1 [schnell] → `flux1-schnell-fp8.safetensors`（Comfy-Org 重新打包） | Apache 2.0 | GitHub `black-forest-labs/flux` README 與 `model_licenses/LICENSE-FLUX1-schnell` 標 apache-2.0（含 autoencoder 權重）；上游 HF 為 gated（需登入同意），重新打包版 `Comfy-Org/flux1-schnell` 標 apache-2.0 且不 gated |
| 舊企劃 LLM（已非預設） | gpt-oss-20b（GX10 Ollama：`gpt-oss-20b-128k:latest`） | Apache 2.0（未於本次核對；自訂 tag，載入前請確認其底層模型） | 尚未核對 |

## 主題與分鏡企劃（2026-10-16，經使用者明確放行）

| 用途 | 模型／檔案 | 授權 | 核對依據 |
|---|---|---|---|
| 兒童音樂主題與分鏡企劃（含提示詞、YouTube 文案） | Ollama `qwen3.8:27b`，本機 ID `22130167c4c2`、Q4_K_M | Apache 2.0 | [Qwen 上游模型卡](https://huggingface.co/Qwen/Qwen3.8-27B)、[Ollama 模型卡](https://ollama.com/library/qwen3.8:27b) 與 GX10 `/api/show` 的 license 均為 Apache 2.0 |
| 小知識主題與分鏡企劃（含字幕、提示詞、YouTube 文案；來源仍由維基百科驗證） | Ollama `gemma4:31b`，本機 ID `6316f0629137`、Q4_K_M | Apache 2.0 | [Google 上游模型卡](https://huggingface.co/google/gemma-4-31B-it)、[Ollama 模型卡](https://ollama.com/library/gemma4:31b) 與 GX10 `/api/show` 的 license 均為 Apache 2.0 |

主題與分鏡企劃預設共用各內容種類的模型，減少載入／卸載；`VMK_LLM_MODEL` 未設定時依內容種類選擇，明確設定才覆寫企劃模型。每支影片的 `meta.json.llm` 記錄實際企劃模型。

## 白名單未列的上游子元件（已核對，待你放行）

| 元件／檔案 | 用途 | 授權 | 核對依據 |
|---|---|---|---|
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors`（Comfy-Org/Wan_2.1_ComfyUI_repackaged） | Wan 2.2 文字編碼器 | Apache 2.0 | HF `google/umt5-xxl` 與重新打包頁皆 apache-2.0 |
| FLUX fp8 checkpoint 內含 T5-XXL | 關鍵幀文字編碼器 | Apache 2.0 | HF `google/t5-v1_1-xxl` apache-2.0 |
| FLUX fp8 checkpoint 內含 CLIP-L | 關鍵幀文字編碼器 | MIT | `openai/clip-vit-large-patch14` 卡片與 GitHub `openai/CLIP/LICENSE` 為 MIT |

## 禁用

FLUX.1 [dev]、MiniMax H3 的 Turbo LoRA／ControlNet／風格 embedding（未核對；H3 主模型見下方放行紀錄）、任何 RIFE TensorRT 節點（CC BY-NC-SA）、LTX-2.x、Stable Audio 3、白名單以外的任何模型／LoRA。
NAS `/volume1/comfyUI-model` 內的 LTX-2.3、FLUX.2 檔案**不得**用於本管線；MiniMax H3 的檔案 2026-10-22 起可從 NAS 拉，但必須通過下方的 sha256 驗證。

## 白名單外、經使用者明確放行（2026-10-22）：MiniMax H3

使用者於 2026-10-22 明確放行，用於對白類內容（兒童英文對話、動物短劇；有對白的動畫已於 2026-11-02 退出排程，仍可手動執行；`renderers.py` 的 `h3_fl2va`／`h3_ref2va`）。兒童音樂與小知識仍用 Wan，不使用 H3。

| 用途 | 模型／檔案 | 授權 | 核對依據 |
|---|---|---|---|
| 對白類影片＋音訊 | MiniMax H3 → `minimax_h3_fl2va_pruned_int8_convrot.safetensors`、`minimax_h3_ref2va_pruned_int8_convrot.safetensors`（Comfy-Org 重新打包 `Comfy-Org/MiniMax-H3`） | MiniMax H3 Community License Agreement（License date 2026-08-02；HF 標籤 `minimax-h3-community-license-agreement`） | HF `MiniMaxAI/MiniMax-H3` 的 `LICENSE` 全文；重新打包頁 `Comfy-Org/MiniMax-H3` 標示同一授權 |
| 同上 | 文字編碼器 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`、VAE `minimax_h3_video_vae_int8_convrot.safetensors`、`minimax_h3_audio_vae_fp32.safetensors` | 同上（同一個 repo、同一份授權） | 同上 |

**授權條件（放行不解除這些限制）：**

- **地區**：授權範圍是「全世界，但排除歐盟、英國、南韓與美國」（§I.3、§I.5）；在這些地區使用不在授權內（§V.4）。這個管線的生成在台灣的 GX10 進行；但**影片公開到 YouTube 後，這些地區的觀眾也看得到**，是否構成「在排除地區使用或展示」要由你判斷，上傳一律私人、公開前由你決定（程式不會公開）。
- **商業**：商業產品或服務年營收超過 US$20M 要另行取得 MiniMax 書面授權（§IV.1）；商業產品介面要顯著標示「MiniMax H3」（§IV.2）。
- **散布**：轉散布模型或衍生檔要附授權全文、修改檔要標示修改，並附 NOTICE「MiniMax H3 is licensed under the MiniMax H3 Community License Agreement, Copyright © 2026 MiniMax. All Rights Reserved.」（§III）。本專案只在 GX10 本機使用權重，不散布。
- **未納入**：Turbo LoRA（`minimax_h3_*_turbo_*step*`）、ControlNet（`model_patches/`）與風格 embedding 未納入，用之前需另外核對。
- 權重來源：NAS `/volume1/comfyUI-model` 的 H3 檔案（使用者 2026-10-22 放行，`scripts/nas_pull_h3.sh`）或 HF `Comfy-Org/MiniMax-H3`（`scripts/download.sh`）。**一律要與 HF 的 LFS sha256 相同才採用**（腳本自動驗，不符改名 `.bad`）。NAS 只有 fp16 的 video VAE，沒有 workflow 用的 `int8_convrot`，所以這個檔從 HF 下載。
- 一鍵關閉：`VMK_H3_ENABLED=0`（cron 的 `env`）；關閉後對白類夜間排程只記 `skipped`。

## 白名單外、經使用者明確放行（2026-10-05）

| 用途 | 模型／檔案 | 授權 | 核對依據 |
|---|---|---|---|
| 照片模式：兩隻狗同框的關鍵幀合成；對白類角色參考圖的多視角編輯（2026-10-22，同一模型、同一授權） | Qwen-Image-Edit-2509 → `qwen_image_edit_2509_fp8_e4m3fn.safetensors` | Apache 2.0 | HF `Qwen/Qwen-Image-Edit-2509` 與 `Comfy-Org/Qwen-Image-Edit_ComfyUI` 皆標 apache-2.0 |
| 同上 | 文字編碼器 `qwen_2.5_vl_7b_fp8_scaled.safetensors`（Qwen2.5-VL **7B**） | Apache 2.0 | HF `Qwen/Qwen2.5-VL-7B-Instruct` apache-2.0（注意：Qwen2.5-VL 的 3B／72B 授權不同，不可換用） |
| 同上 | `qwen_image_vae.safetensors` | Apache 2.0 | HF `Qwen/Qwen-Image` apache-2.0 |

來源證據：GX10 上這三個檔案的 sha256 與 HF `Comfy-Org` 官方檔案**完全相同**（diffusion `318568f6…`、text encoder `cb5636d8…`、vae `a70580f0…`）。
**不使用** `Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors`：其上游 `lightx2v/Qwen-Image-Edit-2509-Lightning` 未標示授權。

## 白名單外、經使用者明確放行（2026-10-20）

| 用途 | 模型／檔案 | 授權 | 核對依據 |
|---|---|---|---|
| 兒童音樂的關鍵幀（小知識仍用 FLUX.1 schnell） | FLUX.3 Image Klein → `flux3-image-klein-bf16.safetensors`（`black-forest-labs/flux-3-image-klein-comfy`） | **FLUX Kommunity License v1.0**（不是 Apache） | [LICENSE.md](https://huggingface.co/black-forest-labs/flux-3-image-klein/blob/main/LICENSE.md)；重新打包頁標同一授權。非商用人人可用；商用限「合格使用者」＝本人含關係企業的年營收低於 US$5M（使用者 2026-10-20 確認：頻道要營利、年營收一定低於 US$5M）。輸出可商用且不算衍生作品；義務：發布前過濾或審查輸出（本管線有人工核准）、法規要求時標示 AI 生成。禁止：用輸出訓練功能相近的模型、侵害肖像權或數位替身權、監控與軍事用途等。**營收達 US$5M 時必須停用或另取授權** |
| 同上 | 文字編碼器 `qwen3vl_4b_bf16.safetensors`（Qwen3-VL-4B-Instruct） | Apache 2.0 | HF `flux-3-image-klein` 的 `text_encoder/LICENSE`、重新打包頁皆標 Apache-2.0 |
| 同上（官方要求搭配，不用則結果明顯較弱） | prompt upsampler `qwen3.5_9b_flux3_prompt_upsampler_bf16.safetensors`（Qwen3.5-9B 微調） | Apache 2.0 | HF `black-forest-labs/flux-3-image-klein-pup` 標 apache-2.0，基底 Qwen3.5-9B 亦為 Apache 2.0 |
| 同上 | `flux2-vae.safetensors` | 重新打包頁未單獨標示，視同隨 repo 的 Kommunity License 處理 | 檔名雖叫 flux2-vae，取自 `flux-3-image-klein-comfy/vae/`；**下方禁用的 NAS FLUX.2 檔案仍不得使用** |

來源證據：GX10 上四個檔案的大小與 HF 檔案**完全相同**（11097404512、8875719408、18819722392、336211292 位元組），下載腳本當時已比對 HF 的 sha256（不符會改名 `.bad`，目前沒有殘留）；HF API 對此 repo 遮蔽 LFS hash，無法事後重新比對。GX10 實測 sha256：diffusion `c3e9813c…`、text encoder `f0b24fc5…`、vae `868fe7b3…`、upsampler `f5062a1f…`。
逐支影片實際使用的檔名與 upsampler 擴寫後的 caption 見該支 `meta.json` 的 `models.keyframes` 與 `shots[].upsampled`。

## 視覺 QC（2026-10-15，經使用者明確放行並接入管線）

| 用途 | 模型／檔案 | 授權 | 核對依據 |
|---|---|---|---|
| 兒童內容與畫面變形的自動抽樣 QC | Cloudflare/Clef-Flash → Ollama `clef-flash:9b` | Apache 2.0 | [上游模型卡](https://huggingface.co/Cloudflare/clef-flash) 與 [Ollama 模型卡](https://ollama.com/library/clef-flash) 皆標 Apache-2.0；上游基底為 Qwen3.5-9B；逐作業 digest 記於 `visual_qc.json` |

## 小知識的資料與字型（2026-10-16）

非模型，但同樣要有授權依據：

| 用途 | 資料／檔案 | 授權 | 核對依據與義務 |
|---|---|---|---|
| 小知識的內容來源 | 英文維基百科條目文字（`videomaker/wiki.py` 經 MediaWiki API 抓取） | CC BY-SA 4.0 | [維基媒體使用條款](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use)：內容採 CC BY-SA 授權，再利用須標示來源並以相同授權分享。影片說明欄固定附條目的永久連結與授權；字幕是 LLM 依條目改寫的一句話，來源與授權記在 `plan.json` 的 `sources` 與 `meta.json` 的 `wikipedia` |
| 小知識的英文字幕 | DejaVu Sans Bold（GX10 內建 `/usr/share/fonts/truetype/dejavu/`） | Bitstream Vera 衍生的自由字型授權 | [DejaVu 授權](https://dejavu-fonts.github.io/License.html)允許使用、嵌入與散布；以 ffmpeg `drawtext` 渲染到影片，不隨專案散布字型檔 |

不新增任何模型：小知識沿用本文件已列的企劃 LLM、ACE-Step、FLUX、Wan。

## 已取消（2026-10-05）

路線 A（Wan 2.2 I2V-A14B、Lightning 4-step LoRA、Comfy-Org frame_interpolation／RIFE v4.25、`wan_2.1_vae`）已取消，未下載、未使用，
程式碼與 workflow 已移除。因此 RIFE 訓練資料集的非商用疑慮也不再適用。以後若重新啟用，需重新核對授權。
