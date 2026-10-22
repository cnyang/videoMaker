"""Renderer：一鏡 → 一支「帶對白音訊」的短片（clips/NN.mp4）。

- `wan`：既有的 Wan 2.2 TI2V-5B（兒童音樂、小知識），仍由 `pipeline.clips` 直接處理；它沒有音訊，所以不能用在對白類。
- `h3_fl2va`：MiniMax H3 首尾幀轉影片＋音訊。先用 FLUX 產固定關鍵幀（角色外觀靠關鍵幀維持），再由 H3 演出並配音。
- `h3_ref2va`：MiniMax H3 參考圖轉影片＋音訊。直接餵角色庫的參考圖（`<Picture N>`），不需要關鍵幀；角色一致性較強。

H3 的所有細節（workflow、提示詞、幀數換算、啟用開關）都收在這個檔案，pipeline 其他地方不出現 H3 專屬指令。
H3 已於 2026-10-22 由使用者放行（授權限制見 `LICENSES.md`）；`VMK_H3_ENABLED=0` 可一鍵關閉，權重沒裝好時夜間排程只記 `skipped`。
"""

import logging
import time
from dataclasses import dataclass

import requests

from . import characters, config, content
from .comfy import MAP, Comfy, fill, missing_models, models_in

log = logging.getLogger("videomaker")
FPS = 24
REF_SLOTS = content.MAX_REFS  # workflow 內建的參考圖節點數；實際張數少於此時，多的節點與連線在送出前移除
CLIP_TIMEOUT = 3600


class RendererDisabled(RuntimeError):
    pass


def frames(seconds: float) -> int:
    """秒數 → H3 合法幀數（24fps、17k+5 格，往上取整）；與官方範本的 Math Expression 同一條公式。"""
    n = max(5, round(seconds * FPS))
    return n + (5 - n % 17) % 17


@dataclass(frozen=True)
class H3:
    name: str
    workflow: str  # workflow_map.yaml 的名稱
    keyframes: bool  # 要不要先產關鍵幀（fl2va 要，ref2va 不要）

    def prompt(self, script: dict, shot: content.ScriptShot) -> str:
        """H3 提示詞：畫風與場景、這一鏡的動作／情緒／鏡位，最後逐句列出對白（誰、用什麼聲音、說什麼）。
        ref2va 另外用 <Picture N> 把參考圖對應到角色，順序與送進 workflow 的參考圖相同。"""
        chars = {cid: characters.load(cid) for cid in shot.characters}
        parts = [script["style"] + "."]
        if self.name == "h3_ref2va":
            for i, asset in enumerate(shot.reference_assets, 1):
                cid, _, view = asset.partition("/")
                parts.append(f"Use <Picture {i}> ({view} view) as the exact appearance of {chars[cid].name}.")
        else:
            parts.append(", ".join(chars[cid].prompt() for cid in shot.characters) + ".")
        if setting := script.get("setting"):
            parts.append(f"Setting: {setting}.")
        parts += [x + "." for x in (shot.visual_prompt, shot.action) if x]
        if shot.emotion:
            parts.append(f"Emotion: {shot.emotion}.")
        if shot.camera:
            parts.append(f"Camera: {shot.camera}.")
        for line in shot.dialogue:
            c = chars[line.speaker]
            parts.append(f'{c.name} ({c.voice}) says clearly: "{line.text}"')
        parts.append("Natural speech and light sound effects only, no background music, no on-screen text.")
        return " ".join(parts)

    def render(self, job, c: Comfy, shot) -> dict:
        """產生一鏡並存到 clips/NN.mp4；回傳要寫進 meta.json 的欄位（含生成秒數，供耗時估算）。"""
        script = job.plan.script
        sshot = content.ScriptShot.model_validate(next(s for s in script["shots"] if s["shot_id"] == shot.id))
        w, h = config.H3_SIZE
        n = frames(sshot.duration)
        seed = job.plan.seed + shot.id
        prompt = self.prompt(script, sshot)
        values = {
            "prompt": prompt,
            "width": w,
            "height": h,
            "length": n,
            "seed": seed,
            "steps": config.H3_STEPS,
        }
        if self.keyframes:
            values["first_frame"] = c.upload(
                job.p("keyframes", f"{shot.id:02d}.png"), f"{job.dir.name}_{shot.id:02d}.png"
            )
        wf = fill(self.workflow, **values)
        if not self.keyframes:
            wf = self._attach_refs(job, c, wf, sshot)
        t = time.monotonic()
        c.run(wf, job.p("clips", f"{shot.id:02d}.mp4"), timeout=CLIP_TIMEOUT)
        gen = round(time.monotonic() - t, 1)
        job.meta["models"]["clips"] = models_in(wf)
        return {
            "clip_seed": seed,
            "clip_sec": gen,
            "steps": config.H3_STEPS,
            "clip_prompt": prompt,
            "frames": n,
            "seconds_generated": round(n / FPS, 3),
            "renderer": self.name,
        }

    def _attach_refs(self, job, c: Comfy, wf: dict, sshot) -> dict:
        """把這一鏡的參考圖上傳並填進 LoadImage 節點；多的節點連同 ref_images 輸入一起移除。"""
        refs = sshot.reference_assets
        if (
            not 1 <= len(refs) <= REF_SLOTS
        ):  # 腳本驗證已保證；舊 plan.json 或手改的 plan 不能讓 ref2va 在沒有參考圖時照送
            raise RuntimeError(
                f"shot {sshot.shot_id} 的 reference_assets 必須是 1–{REF_SLOTS} 張，目前 {len(refs)} 張"
            )
        paths = [characters.load(a.partition("/")[0]).ref(a.partition("/")[2]) for a in refs]
        spec = MAP[self.workflow]["fields"]
        for i in range(REF_SLOTS):
            node, field = spec[f"ref{i}"][0]
            if i < len(paths):
                wf[node]["inputs"][field] = c.upload(paths[i], f"{job.dir.name}_ref{i}_{paths[i].name}")
            else:
                del wf[node]
                for n in wf.values():
                    n["inputs"].pop(f"ref_images.ref_image_{i}", None)
        return wf

    def missing_refs(self, job) -> list[str]:
        """ref2va 開跑前檢查：企劃用到的參考圖是否都已產生。回傳缺的 '<角色>/<視角>'。"""
        if self.keyframes:
            return []
        need = {a for s in job.plan.script["shots"] for a in s.get("reference_assets", [])}
        return sorted(
            a for a in need if not characters.load(a.partition("/")[0]).ref(a.partition("/")[2]).exists()
        )


RENDERERS = {
    "h3_fl2va": H3("h3_fl2va", "clip_h3_fl2va", keyframes=True),
    "h3_ref2va": H3("h3_ref2va", "clip_h3_ref2va", keyframes=False),
}


def get(name: str) -> H3:
    if name == "wan":
        raise ValueError("wan 沒有音訊，不能用於對白類內容（請改用 h3_fl2va 或 h3_ref2va）")
    if name not in RENDERERS:
        raise ValueError(f"未知的 renderer {name!r}（可用：wan、{'、'.join(RENDERERS)}）")
    return RENDERERS[name]


def enabled(name: str) -> bool:
    """wan 永遠可用；H3 預設啟用，VMK_H3_ENABLED=0 關閉。"""
    return name == "wan" or (name in RENDERERS and config.H3_ENABLED)


def require(name: str) -> None:
    if not enabled(name):
        raise RendererDisabled(f"renderer {name} 未啟用（VMK_H3_ENABLED=0）；H3 已放行，重新啟用請移除該設定")


def missing_weights(name: str) -> list[str]:
    """renderer 的 workflow 用到、但 ComfyUI 下拉清單還看不到的模型檔名（權重還沒下載完）。
    ComfyUI 連不上時回傳空：交給後面的階段用原本的錯誤處理，不在這裡多一種失敗。"""
    if name not in RENDERERS:
        return []
    try:
        info = requests.get(f"{config.COMFY_URL}/object_info", timeout=30).json()
    except (requests.RequestException, ValueError):
        return []
    return sorted({f for wf, f in missing_models(info) if wf == RENDERERS[name].workflow})
