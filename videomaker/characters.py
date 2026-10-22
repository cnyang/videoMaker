"""固定角色庫：`characters/<id>/character.json` ＋ 參考圖，三種對白類內容共用、跨集重用。

角色資料與參考圖是持久資產，夜間排程**絕不**重新生成：企劃只能引用這裡已有的角色 ID，
不存在就驗證失敗（讓 LLM 改寫），不會偷偷產生外觀不同的新角色。參考圖由人工執行
`videomaker characters build` 一次產生，之後檢查、挑選、必要時手動換圖。
產圖方式：基準視角（front）用 FLUX.1 schnell 文字生圖；其他視角用 Qwen-Image-Edit-2509 從 front 改出來，
各視角才會是同一隻角色（各自文字生圖時，同一段描述會畫出不同的狗）。
"""

import hashlib
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from . import config
from .comfy import Comfy, fill, models_in

log = logging.getLogger("videomaker")
# ID 由 LLM 填寫並會組成檔案路徑，所以只接受這個樣式（擋路徑穿越）；與 jobs.JOB_RE 同一個精神
ID_RE = re.compile(r"^[a-z]+_\d{2}$")
FILE_RE = re.compile(r"^[a-z0-9_]+\.png$")
VIEW_RE = re.compile(r"^[a-z]+$")
VIEW_SIZE = (704, 1280)  # 與 Wan／FLUX 關鍵幀同一組直式尺寸
# 各視角的提示詞後綴；只有 character.json 的 references 宣告的視角才會產生
VIEW_PROMPTS = {
    "front": "front view, full body, standing, friendly neutral expression, plain light background, centered",
    "side": "side view, full body, standing, plain light background, centered",
    "happy": "front view, full body, big joyful expression, arms raised, plain light background, centered",
    "sad": "front view, full body, droopy sad expression, shoulders down, plain light background, centered",
}
# 其他視角由 front 圖「編輯」而來：只描述要改的部分，外觀一律照原圖
VIEW_EDITS = {
    "side": "Show the same character from a side view, full body, standing, with nothing added to the legs or paws.",
    "happy": "Make the same character jump with a big joyful open-mouth smile and both arms raised.",
    "sad": "Make the same character look sad: downcast eyes, a small frown, shoulders slightly down. Keep the ears exactly as they are.",
}
KEEP = (
    "Keep exactly the same character: same species, face, colors, markings, outfit and accessories, "
    "same flat 2D cartoon style, plain light background, full body, centered."
)
BASE_VIEW = "front"


class UnknownCharacter(ValueError):
    """企劃引用了不存在的角色或參考圖；是 ValueError，讓企劃重試迴圈把錯誤丟回給 LLM。"""


class Character(BaseModel):
    id: str
    name: str
    species: str
    breed: str = ""
    appearance: str  # 純英文、固定不變：每鏡原樣附在提示詞前，是外觀一致的最後防線
    personality: str
    accessories: str  # 服裝與配件（如領帶、背包）
    voice: str  # 聲音與說話風格，寫進 H3 提示詞（如 "a cheerful young male voice"）
    references: dict[str, str] = Field(min_length=1)  # 視角 → 檔名（如 front: front.png），至少一個

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not ID_RE.fullmatch(v):
            raise ValueError(f"角色 ID {v!r} 不合法（只接受 <英文>_<兩位數字>，如 dog_01）")
        return v

    @field_validator("appearance", "voice")
    @classmethod
    def _ascii(cls, v):
        if not v.isascii():  # 中文進到 FLUX／H3 會被畫成亂碼文字或唸錯
            raise ValueError("appearance 與 voice 必須是純英文")
        return v

    @field_validator("references")
    @classmethod
    def _refs(cls, v):
        for view, name in v.items():
            if not VIEW_RE.fullmatch(view) or not FILE_RE.fullmatch(name):
                raise ValueError(
                    f"參考圖 {view}: {name!r} 不合法（視角為小寫英文、檔名為 <小寫英數底線>.png）"
                )
        return v

    @property
    def dir(self) -> Path:
        return config.CHARACTERS / self.id

    def ref(self, view: str) -> Path:
        if view not in self.references:
            raise UnknownCharacter(f"{self.id} 沒有 {view!r} 這個參考圖（可用：{sorted(self.references)}）")
        return self.dir / self.references[view]

    def base_view(self) -> str:
        """文字生圖的基準視角（front，沒有就取第一個）；其他視角都從它編輯而來。"""
        return BASE_VIEW if BASE_VIEW in self.references else next(iter(self.references))

    def missing_refs(self) -> list[str]:
        return sorted(v for v in self.references if not self.ref(v).exists())

    def prompt(self) -> str:
        """每鏡提示詞裡描述這個角色的句子：名字 ＋ 固定外觀 ＋ 配件。"""
        return f"{self.name}, {self.appearance}" + (f", {self.accessories}" if self.accessories else "")


def ids() -> list[str]:
    root = config.CHARACTERS
    return sorted(p.name for p in root.iterdir() if (p / "character.json").exists()) if root.exists() else []


def load(cid: str) -> Character:
    if not isinstance(cid, str) or not ID_RE.fullmatch(cid):  # 先驗樣式再碰檔案系統
        raise UnknownCharacter(f"角色 ID {cid!r} 不合法（可用：{ids()}）")
    path = config.CHARACTERS / cid / "character.json"
    if not path.exists():
        raise UnknownCharacter(f"角色 {cid!r} 不存在（可用：{ids()}）；不可自創角色")
    char = Character.model_validate_json(path.read_text(encoding="utf-8"))
    if char.id != cid:
        raise UnknownCharacter(f"{path} 的 id 是 {char.id!r}，與資料夾名 {cid!r} 不同")
    return char


def check_asset(asset: str, allowed: set[str]) -> None:
    """reference_assets 的寫法是 '<角色 ID>/<視角>'（如 dog_01/front）：角色必須在本集角色內，視角必須是它宣告過的。"""
    cid, _, view = asset.partition("/")
    if cid not in allowed:
        raise UnknownCharacter(
            f"reference_assets {asset!r} 的角色 {cid!r} 不在本集角色 {sorted(allowed)} 內"
            "（格式必須是 '<角色 ID>/<視角>'，用斜線，如 dog_01/front）"
        )
    load(cid).ref(view)


def _seed(char: Character, view: str, salt: int) -> int:
    key = f"{char.id}/{view}" + (f"/{salt}" if salt else "")  # salt=0 與舊版相同；換 salt 才會換圖
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 2**31


def _edit_workflow(prompt: str, seed: int, image: str) -> dict:
    """compose workflow 只餵一張圖：移除第二張圖的節點與連線。"""
    wf = fill("compose", prompt=prompt, seed=seed, image1=image, width=VIEW_SIZE[0], height=VIEW_SIZE[1])
    del wf["img2"]
    wf["pos"]["inputs"].pop("image2")
    wf["neg"]["inputs"].pop("image2")
    return wf


def build(
    only: list[str] | None = None, force: bool = False, comfy: Comfy | None = None, salt: int = 0
) -> list[Path]:
    """一次性產生缺少的參考圖（已存在就跳過，force 才覆蓋）。這是手動 GPU 作業，夜間排程不會呼叫。
    回傳本次產生的檔案。seed 由 (角色, 視角, salt) 決定，同一份 character.json 同一個 salt 重跑會得到同一張圖。
    基準視角（front，沒有就取第一個）文字生圖，其他視角從基準圖編輯而來。"""
    todo = [
        (c, v)
        for cid in (only or ids())
        for c in [load(cid)]
        for v in sorted(c.references, key=lambda v: v != c.base_view())  # 基準視角先產，其他視角要用它
        if force or v in c.missing_refs()
    ]
    if not todo:
        return []
    c = comfy or Comfy()
    c.prepare()
    out = []
    try:
        for char, view in todo:
            seed = _seed(char, view, salt)
            if view == char.base_view():
                prompt = f"{char.prompt()}, {VIEW_PROMPTS[BASE_VIEW]}"
                wf = fill("keyframe", prompt=prompt, seed=seed, width=VIEW_SIZE[0], height=VIEW_SIZE[1])
            else:
                base = char.ref(char.base_view())
                edit = VIEW_EDITS.get(view, f"Show the same character in a {view} pose.")
                wf = _edit_workflow(f"{edit} {KEEP}", seed, c.upload(base, f"{char.id}_base_{base.name}"))
            c.run(wf, char.ref(view), timeout=900)  # 逐張存檔：中途失敗，已產生的不會丟，重跑只補缺的
            log.info("角色 %s/%s 參考圖已產生（%s）", char.id, view, ", ".join(models_in(wf)))
            out.append(char.ref(view))
    finally:
        c.free()  # 失敗也要卸載，否則模型會占著共用的統一記憶體
    return out
