"""設定一律走環境變數（不寫死金鑰）。

在 GX10 上跑：export VMK_COMFY_URL=http://127.0.0.1:8188 VMK_LLM_URL=http://127.0.0.1:11434/v1
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMFY_URL = os.getenv("VMK_COMFY_URL", "http://10.200.200.200:8188")
LLM_URL = os.getenv("VMK_LLM_URL", "http://10.200.200.200:11434/v1")  # Ollama 的 OpenAI 相容端點
LLM_MODEL = os.getenv("VMK_LLM_MODEL", "")
KIDS_TOPIC_MODEL = os.getenv("VMK_KIDS_TOPIC_MODEL", "qwen3.8:27b")
TRIVIA_TOPIC_MODEL = os.getenv("VMK_TRIVIA_TOPIC_MODEL", "gemma4:31b")
WORK = Path(os.getenv("VMK_WORK", ROOT / "work"))
MIN_FREE_GB = float(os.getenv("VMK_MIN_FREE_GB", "40"))  # 統一記憶體剩餘低於此值就中止
STEPS = int(os.getenv("VMK_STEPS", "20"))  # Wan 取樣步數：越多細節通常越好、越慢（每鏡約 17 秒/步）
COOL_TEMP = int(
    os.getenv("VMK_COOL_TEMP", "45")
)  # 每支生成前 GPU 要降到此溫度（°C）以下才開跑；閒置實測約 36-37°C
DEFAULT_BPM = 120
CHARACTERS = Path(
    os.getenv("VMK_CHARACTERS", ROOT / "characters")
)  # 固定角色庫：character.json 與參考圖，跨集重用、不每晚重生
FONT = os.getenv(
    "VMK_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)  # 小知識字幕用的字型（GX10 內建 DejaVu）


# ---------- H3 renderer（2026-10-22 使用者放行；授權地區限制見 LICENSES.md。VMK_H3_ENABLED=0 可一鍵關閉）----------
H3_ENABLED = os.getenv("VMK_H3_ENABLED", "1") == "1"
# H3 比 Wan 吃更多統一記憶體：這是「相對 VMK_MIN_FREE_GB 再多要的 GB」，基準門檻不降低。尚未實測，先用保守值，首次實跑後依 meta／日誌校準
H3_EXTRA_GB = float(os.getenv("VMK_H3_EXTRA_GB", "25"))
H3_SIZE = tuple(
    int(x) for x in os.getenv("VMK_H3_SIZE", "576x1024").split("x")
)  # 9:16、32 的倍數；mux 再放大裁切到 1080x1920
H3_STEPS = int(os.getenv("VMK_H3_STEPS", "20"))  # H3 取樣步數（官方範本預設 20）
H3_DEFAULT_SEC_PER_SEC = float(
    os.getenv("VMK_H3_SEC_PER_SEC", "90")
)  # 沒有 history 時，每秒成片的保守生成秒數
H3_SHOT_SEC = (5.0, 15.0)  # H3 訓練範圍約 124–362 幀（24fps）= 5–15 秒；單鏡企劃時長必須落在這裡


def plan_model(kind: str) -> str:
    # 分鏡與主題共用模型以減少切換；保留舊環境變數供手動覆寫企劃模型。
    return LLM_MODEL or (TRIVIA_TOPIC_MODEL if kind == "trivia" else KIDS_TOPIC_MODEL)
