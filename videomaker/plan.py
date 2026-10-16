"""企劃：LLM 產 plan.json（pydantic 驗證，最多重試 3 次），鏡頭數由程式依 BPM 算出。"""

import json
import logging
import math
import random
import re
import secrets
from typing import Literal

import requests
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config

log = logging.getLogger("videomaker")

# 禁止出現：現有角色／品牌／「in the style of」。名單只是第一道防線，另有 LLM 指令與人工審核
BANNED = re.compile(
    r"in the style of|\b(disney|pixar|mickey|minnie|peppa|bluey|cocomelon|baby shark|pinkfong|paw patrol|"
    r"hello kitty|pokemon|pikachu|mario|sesame|elmo|barney|dora|doraemon|ghibli|totoro|miffy|paddington|winnie)\b",
    re.IGNORECASE,
)


# 畫面不要出現文字（AI 生成文字常出錯）：提示詞禁止這些字眼
TEXTY = re.compile(
    r"\b(text|letters?|words?|signs?|labels?|posters?|writing|written|logo|caption)\b", re.IGNORECASE
)


# 影像／影片模型容易畫壞的東西：牙齒與嘴巴（會畫成怪異的牙）、細長會彎的物體（牙刷、牙線、繩子會在影片裡彎曲變形）、
# 液體與泡沫（噴濺、倒水會糊成一團）。企劃階段就避開，比事後補救有效；刷牙這類主題請寫成「拿著一支直的牙刷微笑」之類
RISKY = re.compile(
    r"\b(teeth|tooth|toothy|mouths?|grin\w*|tongues?|lips?|"
    r"floss\w*|strings?|ribbons?|ropes?|hoses?|cables?|"
    r"pour\w*|splash\w*|bubbl\w*|foam\w*|water\w*|toothpaste|soap\w*|liquids?|"
    r"bend\w*|bent|curv\w*|stretch\w*|twist\w*)\b",
    re.IGNORECASE,
)


# 道具顏色要固定：顏色詞清單（用來檢查 prop 有寫顏色、各鏡提到道具時沒有換顏色）
COLOR = re.compile(
    r"\b(red|blue|yellow|pink|green|orange|purple|violet|white|black|brown|gr[ae]y|silver|gold(?:en)?|cyan|turquoise|teal|beige|cream)\b",
    re.IGNORECASE,
)


def check_prop(prop: str, shots: list) -> None:
    """prop（例如 'a short straight blue toothbrush'）必須帶顏色；各鏡提到這個道具（最後一個字）時，前面的顏色不能和 prop 不同。"""
    colors = {c.lower() for c in COLOR.findall(prop)}
    if not colors:
        raise ValueError("prop 必須寫出顏色（如 'a short straight blue toothbrush'），道具顏色才會每鏡一致")
    noun = re.escape(prop.split()[-1])
    for sh in shots:
        for m in re.finditer(rf"((?:\w+\s+){{0,3}}){noun}\b", sh.prompt, re.IGNORECASE):
            other = {c.lower() for c in COLOR.findall(m.group(1))} - colors
            if other:
                raise ValueError(
                    f"shot {sh.id} 的道具顏色（{'/'.join(sorted(other))}）和 prop 的顏色（{'/'.join(sorted(colors))}）不同，每鏡必須一致"
                )


# 簡體專有字：LLM 偶爾會忽略「繁體中文（台灣）」。只收「繁體不會用到、不會同時是合法繁體字」的常見字（如 兔、松、面 不收）
SIMPLIFIED = re.compile(
    "[个们这来说时间学习开关门东话语读书爱长对两热让乐园艺鸡鸭猫鸟风树叶龙马鱼飞车爷妈声岛饭汤脸齿"
    "头见现实问题觉听买卖红绿蓝黄灯点从总还过运动欢电视带队张场应当号处为与业丝丽乱产亲伤伞价体传债儿兑兰关兴养兽写军农"
    "冲决冻净减击创别刚剧务劲区医华单卫厂压县参发变]"
)


def check_traditional(what: str, text: str) -> None:
    if m := SIMPLIFIED.search(text):
        raise ValueError(f"{what} 含簡體字「{m.group(0)}」，請改用繁體中文（台灣用字）")


CJK = re.compile(r"[\u4e00-\u9fff]")
EN_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def check_bilingual(yt) -> None:
    """生成企劃時檢查：YouTube 標題、說明、標籤必須中英雙語（英文能增加觸及率）。只用在 make_plan，舊的 plan.json 載入不受影響。"""
    if len(yt.title) > 100:
        raise ValueError(f"youtube.title 超過 100 字元（{len(yt.title)}），會被截斷、英文部分可能被切掉")
    if not CJK.search(yt.title) or len(EN_WORD.findall(yt.title)) < 2:
        raise ValueError("youtube.title 必須中英雙語，格式「<繁體中文標題>｜<English title>」")
    if not CJK.search(yt.description) or len(EN_WORD.findall(yt.description)) < 8:
        raise ValueError(
            "youtube.description 必須有繁體中文與至少 8 個英文單字（中文兩句、空行、英文兩句、空行、英文 hashtag）"
        )
    if sum(t.isascii() for t in yt.tags) < 3:
        raise ValueError("youtube.tags 至少要有 3 個英文標籤")


def check_content(what: str, text: str):
    """企劃內容規則（非照片模式）：純英文、不提文字類物件、不碰高風險字眼。"""
    if not text.isascii():  # 中文進到 FLUX 會被畫成亂碼文字
        raise ValueError(f"{what} 必須是純英文（含非 ASCII 字元：{text[:30]!r}）")
    if m := TEXTY.search(text):
        raise ValueError(f"{what} 提到文字類物件（{m.group(0)}），畫面不要有文字")
    if m := RISKY.search(text):
        raise ValueError(
            f"{what} 含高風險字眼「{m.group(0)}」：牙齒、嘴巴、細長會彎的物體、液體與泡沫容易被畫壞，"
            "請改成不需要出現它的動作（例如只寫拿著一支短而直的牙刷微笑，不要寫牙齒、嘴巴、水、泡沫）"
        )


class Music(BaseModel):
    caption: str
    bpm: int = Field(ge=100, le=140)
    time_signature: Literal["4/4"]
    key: str = Field(pattern=r"^[A-G][#b]? (major|minor)$")


class Shot(BaseModel):
    id: int
    prompt: str


class Youtube(BaseModel):
    title: str = ""
    description: str = ""
    tags: list[str] = []


class Plan(BaseModel):
    topic: str
    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    style: str
    character: str
    prop: str = ""  # 手持道具（含顏色），選填；每鏡固定附在 character 後面，避免各鏡道具顏色不同
    music: Music
    shots: list[Shot] = Field(min_length=1)
    layout: list[int] = []  # 長片：每個時段播哪個鏡頭 id；空 = 每鏡各一次（短片）
    youtube: Youtube = Youtube()
    seed: int = Field(default_factory=lambda: secrets.randbelow(2**31))  # 整支影片的 seed 基底，由程式產生

    @field_validator("style", "character")
    @classmethod
    def _no_banned(cls, v):
        if BANNED.search(v):
            raise ValueError(f"含禁用字詞：{BANNED.search(v).group(0)}")
        return v

    @field_validator("shots")
    @classmethod
    def _shots_not_banned(cls, shots):
        for s in shots:
            if BANNED.search(s.prompt):
                raise ValueError(f"shot {s.id} 含禁用字詞：{BANNED.search(s.prompt).group(0)}")
        return shots

    @model_validator(mode="after")
    def _youtube_traditional(self):
        for what, text in [
            ("youtube.title", self.youtube.title),
            ("youtube.description", self.youtube.description),
            *((f"youtube.tags[{i}]", t) for i, t in enumerate(self.youtube.tags)),
        ]:
            check_traditional(what, text)
        return self

    @model_validator(mode="after")
    def _layout_ids(self):
        bad = set(self.layout) - {s.id for s in self.shots}
        if bad:
            raise ValueError(f"layout 含不存在的鏡頭 id：{sorted(bad)}")
        return self

    @model_validator(mode="after")
    def _content_rules(self):
        # 照片模式（style 為空）的提示詞來自使用者的照片描述，不套用；那邊由 _use_photos 自己檢查
        if self.style:
            if len(self.character.split()) < 6:
                raise ValueError(
                    "character 太短：要寫物種、身體顏色與 1–2 個固定特徵（如 'a round brown bear cub with a red scarf and big friendly eyes'）"
                )
            for what, text in [
                ("style", self.style),
                ("character", self.character),
                ("prop", self.prop),
                *((f"shot {s.id}", s.prompt) for s in self.shots),
            ]:
                check_content(what, text)
            if self.prop:
                check_prop(self.prop, self.shots)
        return self

    def full_prompt(self, shot: Shot) -> str:
        """style、character、prop（道具含顏色）原樣加在每鏡提示詞前面，維持畫風、角色與道具一致。"""
        held = f"holding {self.prop}" if self.prop else ""
        return ", ".join(
            x for x in (self.style, self.character, held, shot.prompt) if x
        )  # 照片模式前三者為空


# 片型：短片 ≈ 20 秒；長片 ≈ 2:48（YouTube Shorts 上限 3:00，留餘裕），鏡頭只做 unique 個，再重複使用填滿
FORMATS = {
    "short": {"target": 20, "min_total": 15, "max_total": 20.5, "unique": None},
    "long": {"target": 168, "min_total": 150, "max_total": 179, "unique": 14},
    # 約 5 分鐘：超過 3:00 就不再是 Shorts，是一般的直式影片；獨特鏡頭 20 個，其餘重複使用
    "xl": {"target": 300, "min_total": 270, "max_total": 330, "unique": 20},
}


def shot_math(bpm: int, fmt: str = "short") -> tuple[float, int, float]:
    """2 小節 = 1 鏡（時段）。回傳 (每鏡秒數, 時段數, 理論總長)。"""
    f = FORMATS[fmt]
    shot_sec = 60 / bpm * 4 * 2
    n = math.floor(f["target"] / shot_sec)
    total = n * shot_sec
    if shot_sec > 5.0 or not f["min_total"] <= total <= f["max_total"]:
        raise ValueError(f"BPM {bpm} 無法滿足單鏡 ≤5 秒且{fmt}總長 {f['min_total']}–{f['max_total']} 秒")
    return shot_sec, n, total


def build_layout(n_unique: int, n_slots: int, seed: int) -> list[int]:
    """長片：把 n_unique 個獨特鏡頭輪流重複填滿 n_slots 個時段。第一輪照順序，之後每輪換順序（依 seed 決定、可重現），
    且相鄰時段不會是同一個鏡頭。"""
    rng = random.Random(seed)
    out: list[int] = []
    while len(out) < n_slots:
        cycle = list(range(1, n_unique + 1))
        if out:  # 第二輪起打亂；開頭若和上一輪結尾相同就換掉，避免連續兩個時段同一個鏡頭
            rng.shuffle(cycle)
            if cycle[0] == out[-1]:
                cycle[0], cycle[-1] = cycle[-1], cycle[0]
        out += cycle
    return out[:n_slots]


_SYSTEM = """You write plans for 15-20 second instrumental kids music YouTube Shorts (vertical, no narration, no lyrics).
Return ONE JSON object only, no markdown. Schema:
{"topic": str, "slug": "lowercase-hyphen-words", "style": str, "character": str, "prop": str,
 "music": {"caption": str, "bpm": int, "time_signature": "4/4", "key": "C major"},
 "shots": [{"id": 1, "prompt": str}], "youtube": {"title": str, "description": str, "tags": [str]}}
Rules: style, character and every shot prompt must be PURE ENGLISH (ASCII only, never Chinese characters: the image model would draw them as garbled text);
the youtube fields are BILINGUAL to reach more viewers (Traditional Chinese as used in Taiwan, never Simplified characters, plus English):
youtube.title = "<繁體中文標題>｜<English title>" (max 100 characters in total, English part like "Wind Chime Song for Kids | Relaxing Kids Music");
youtube.description = 2 short Traditional Chinese sentences, a blank line, 2 short English sentences, a blank line, then 3-5 English hashtags
(e.g. #KidsMusic #Shorts #NurseryRhymes); youtube.tags = 6-10 tags mixing Traditional Chinese and English (at least 4 English, e.g. "kids songs", "nursery rhymes", "baby songs").
character: ONE detailed description reused for every shot: species, body color, and 1-2 fixed distinctive features
(e.g. "a round brown bear cub with a red scarf and big friendly eyes"), at least 8 words.
style: keep it close to "flat 2D cartoon, soft pastel colors, rounded shapes, kid-friendly" for consistency. Use an anthropomorphic animal or stylized cartoon character, never a
realistic child's face. No text/letters/signs in the scenes. No existing cartoon characters, brands, artist names,
"in the style of", or song titles. Each shot prompt describes ONE simple action in ONE setting, in 1 sentence.
Image and video models draw some things badly, so NEVER write these words anywhere (style, character, shots): teeth, tooth, mouth, lips, tongue, grin
(characters keep a small closed smile; never describe or show the inside of a mouth), floss, string, ribbon, rope, hose, cable,
water, liquid, pour, splash, bubbles, foam, soap, toothpaste, and never bend/stretch/twist/curve anything.
prop: the ONE hand-held prop used in the shots, WITH its color and rigid shape, e.g. "a short straight blue toothbrush" (empty string if the topic needs no prop).
The program attaches character + prop to every shot, so every shot that mentions the prop must use the same color (or just say "the toothbrush").
Props: at most ONE prop per shot, and it must be large, simple and rigid (e.g. "a short straight toothbrush"); no thin or flexible objects.
Actions: one simple whole-body action (waving, sitting, bouncing, nodding, tilting the head, holding the prop); no object deformation.
Framing: medium shot, the whole character visible and centered, never a close-up of the face. For a topic like brushing teeth, show the character
holding a short straight toothbrush and smiling, without ever mentioning teeth, mouth, water or foam.
music.caption: instrument/mood tags only (e.g. "cheerful kids instrumental, ukulele, glockenspiel, light percussion")."""


def extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE)
    return text[text.index("{") : text.rindex("}") + 1]


def make_plan(topic: str, n_shots: int, bpm: int, long: bool = False) -> Plan:
    user = f'Topic: "{topic}". Exactly {n_shots} shots (ids 1..{n_shots}). music.bpm must be {bpm}.'
    if long:
        user += (
            " This is a ~3 minute video: the shots will be replayed several times in different orders, so write varied, "
            "self-contained scenes (different settings and simple actions of the same character), not one story that needs a fixed order."
        )
    msgs = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
    err = None
    for attempt in range(3):
        r = requests.post(
            f"{config.LLM_URL}/chat/completions",
            json={"model": config.LLM_MODEL, "messages": msgs, "temperature": 0.7},
            timeout=600,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        try:
            data = json.loads(extract_json(content))
            data["prop"] = data.get("prop") or ""  # LLM 可能回 null
            plan = Plan.model_validate(data)
            if not plan.style or not plan.character:
                raise ValueError("style 與 character 不可為空")
            check_bilingual(plan.youtube)
            if len(plan.shots) != n_shots:
                raise ValueError(f"shots 數量應為 {n_shots}，得到 {len(plan.shots)}")
            plan.music.bpm = bpm  # BPM 由程式決定，LLM 只是附和
            plan.shots = [Shot(id=i + 1, prompt=s.prompt) for i, s in enumerate(plan.shots)]
            return plan
        except ValueError as e:  # JSON 解析、pydantic 驗證（皆為 ValueError 子類）失敗：把錯誤丟回給 LLM 重試
            err = e
            log.warning("企劃第 %d 次驗證失敗，請 LLM 重寫：%s", attempt + 1, e)
            msgs += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"Invalid: {e}. Fix and return JSON only."},
            ]
    raise RuntimeError(f"plan 重試 3 次仍失敗：{err}")
