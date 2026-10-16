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
| 企劃 LLM | gpt-oss-20b（GX10 Ollama：`gpt-oss-20b-128k:latest`） | Apache 2.0（未於本次核對；自訂 tag，載入前請確認其底層模型） | 尚未核對 |

## 白名單未列的上游子元件（已核對，待你放行）

| 元件／檔案 | 用途 | 授權 | 核對依據 |
|---|---|---|---|
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors`（Comfy-Org/Wan_2.1_ComfyUI_repackaged） | Wan 2.2 文字編碼器 | Apache 2.0 | HF `google/umt5-xxl` 與重新打包頁皆 apache-2.0 |
| FLUX fp8 checkpoint 內含 T5-XXL | 關鍵幀文字編碼器 | Apache 2.0 | HF `google/t5-v1_1-xxl` apache-2.0 |
| FLUX fp8 checkpoint 內含 CLIP-L | 關鍵幀文字編碼器 | MIT | `openai/clip-vit-large-patch14` 卡片與 GitHub `openai/CLIP/LICENSE` 為 MIT |

## 禁用

FLUX.1 [dev]、MiniMax H3（含 LoRA 與衍生）、任何 RIFE TensorRT 節點（CC BY-NC-SA）、LTX-2.x、Stable Audio 3、白名單以外的任何模型／LoRA。
NAS `/volume1/comfyUI-model` 內的 MiniMax H3、LTX-2.3、FLUX.2 檔案**不得**用於本管線。

## 白名單外、經使用者明確放行（2026-10-05）

| 用途 | 模型／檔案 | 授權 | 核對依據 |
|---|---|---|---|
| 照片模式：兩隻狗同框的關鍵幀合成 | Qwen-Image-Edit-2509 → `qwen_image_edit_2509_fp8_e4m3fn.safetensors` | Apache 2.0 | HF `Qwen/Qwen-Image-Edit-2509` 與 `Comfy-Org/Qwen-Image-Edit_ComfyUI` 皆標 apache-2.0 |
| 同上 | 文字編碼器 `qwen_2.5_vl_7b_fp8_scaled.safetensors`（Qwen2.5-VL **7B**） | Apache 2.0 | HF `Qwen/Qwen2.5-VL-7B-Instruct` apache-2.0（注意：Qwen2.5-VL 的 3B／72B 授權不同，不可換用） |
| 同上 | `qwen_image_vae.safetensors` | Apache 2.0 | HF `Qwen/Qwen-Image` apache-2.0 |

來源證據：GX10 上這三個檔案的 sha256 與 HF `Comfy-Org` 官方檔案**完全相同**（diffusion `318568f6…`、text encoder `cb5636d8…`、vae `a70580f0…`）。
**不使用** `Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors`：其上游 `lightx2v/Qwen-Image-Edit-2509-Lightning` 未標示授權。

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
