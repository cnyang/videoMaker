"""ComfyUI client：POST /prompt → 輪詢 /history → /view 取檔；含記憶體檢查、/free、逾時與重試。"""

import json
import logging
import time
import uuid
from pathlib import Path

import requests
import yaml

from . import config

log = logging.getLogger("videomaker")
MAP = yaml.safe_load((config.ROOT / "workflow_map.yaml").read_text())
# 這些節點的輸入是模型檔名：寫進 meta.json 當授權證據
LOADERS = {
    "UNETLoader": "unet_name",
    "CLIPLoader": "clip_name",
    "VAELoader": "vae_name",
    "CheckpointLoaderSimple": "ckpt_name",
    "LoraLoaderModelOnly": "lora_name",
}


class ComfyError(RuntimeError):
    pass


def missing_models(info: dict) -> list[tuple[str, str]]:
    """workflow 用到、但 ComfyUI 下拉清單看不到的模型：(workflow, 檔名)。
    多半是沒下載或放錯資料夾（ComfyUI 只認各類型資料夾第一層，hf download 會留下 split_files/ 巢狀目錄）。"""
    out = []
    for name, spec in MAP.items():
        for node in json.loads((config.ROOT / spec["file"]).read_text()).values():
            field = LOADERS.get(node["class_type"])
            if not field:
                continue
            kind, *opt = info[node["class_type"]]["input"]["required"][field]
            options = kind if isinstance(kind, list) else (opt[0] if opt else {}).get("options", [])
            if node["inputs"][field] not in options:
                out.append((name, node["inputs"][field]))
    return sorted(set(out))


def fill(name: str, **values) -> dict:
    """依 workflow_map.yaml 把值填進 workflow；未知欄位直接 KeyError。"""
    spec = MAP[name]
    wf = json.loads((config.ROOT / spec["file"]).read_text())
    for key, val in values.items():
        for node, field in spec["fields"][key]:
            wf[node]["inputs"][field] = val
    return wf


def models_in(wf: dict) -> list[str]:
    return sorted({n["inputs"][LOADERS[n["class_type"]]] for n in wf.values() if n["class_type"] in LOADERS})


class Comfy:
    def __init__(self, url: str = config.COMFY_URL):
        self.url = url.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _req(self, method, path, retries=3, timeout=60, **kw):
        for i in range(retries):
            try:
                return requests.request(method, self.url + path, timeout=timeout, **kw)
            except requests.ConnectionError as e:  # 只重試連線錯誤；HTTP 4xx/5xx 由呼叫端判斷
                log.warning("ComfyUI 連線失敗 %s（%d/%d）：%s", path, i + 1, retries, e)
                if i == retries - 1:
                    raise ComfyError(f"連不上 ComfyUI {self.url}") from e
                time.sleep(2**i)

    def ensure_memory(self, wait: float = 0):
        """統一記憶體會與其他模型爭用：不足就中止，不硬跑。wait > 0 時最多等這麼多秒（/free 是非同步的）。"""
        deadline = time.monotonic() + wait
        while True:
            free_gb = self._req("GET", "/system_stats").json()["system"]["ram_free"] / 2**30
            if free_gb >= config.MIN_FREE_GB:
                log.info("剩餘記憶體 %.0fGB", free_gb)
                return
            if time.monotonic() >= deadline:
                raise ComfyError(f"剩餘記憶體 {free_gb:.0f}GB < {config.MIN_FREE_GB:.0f}GB，中止")
            time.sleep(3)

    def free(self):
        """階段之間卸載模型；部分版本路徑是 /api/free，兩個都試。"""
        for path in ("/free", "/api/free"):
            r = self._req("POST", path, json={"unload_models": True, "free_memory": True})
            if r.ok:
                return
        log.warning("/free 與 /api/free 都失敗（%s），略過", r.status_code)

    def prepare(self):
        """每個 GPU 階段開始前：先卸載 ComfyUI 殘留的模型，再檢查記憶體（順序不能反，否則會被自己上一階段擋住）。"""
        self.free()
        self.ensure_memory(wait=30)

    def upload(self, path: Path, name: str) -> str:
        with open(path, "rb") as f:
            r = self._req("POST", "/upload/image", files={"image": (name, f)}, data={"overwrite": "true"})
        r.raise_for_status()
        return r.json()["name"]

    def run(self, wf: dict, dest: Path, timeout: int = 3600) -> Path:
        """送出 workflow、等完成、把第一個輸出檔存到 dest。"""
        r = self._req("POST", "/prompt", json={"prompt": wf, "client_id": self.client_id})
        if r.status_code != 200:
            raise ComfyError(f"/prompt {r.status_code}: {r.text[:2000]}")
        pid = r.json()["prompt_id"]
        log.info("已送出 %s", pid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2)
            h = self._req("GET", f"/history/{pid}").json().get(pid)
            st = (h or {}).get("status", {})
            if st.get("status_str") == "error":
                msgs = [m[1] for m in st["messages"] if m[0] == "execution_error"]
                raise ComfyError(f"執行失敗 {pid}: {msgs or st}")
            if st.get("completed"):
                return self._fetch(h["outputs"], dest)
        self._req("POST", "/interrupt")
        raise ComfyError(f"逾時 {timeout}s，已送 /interrupt：{pid}")

    def _fetch(self, outputs: dict, dest: Path) -> Path:
        for out in outputs.values():
            for key in ("images", "audio", "videos", "gifs"):
                for f in out.get(key, []):
                    r = self._req(
                        "GET",
                        "/view",
                        params={k: f[k] for k in ("filename", "subfolder", "type")},
                        timeout=300,
                    )
                    r.raise_for_status()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.content)
                    return dest
        raise ComfyError(f"沒有輸出檔：{json.dumps(outputs)[:500]}")
