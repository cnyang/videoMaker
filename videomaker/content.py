"""對白類內容：兒童英文對話、動物短劇、有對白的動畫。

結構化腳本（pydantic schema）→ 驗證 → 組成 Plan。LLM 只負責寫腳本 JSON；鏡頭時長、角色是否存在、字幕與對白是否一致、
語速是否說得完，都由這裡的程式驗證，驗證失敗把錯誤丟回給 LLM 重寫（最多 3 次，與 plan.make_plan 同一個模式）。
renderer 看到的是已驗證的腳本，不必理解自由文字。
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import ClassVar

import requests
from pydantic import BaseModel, Field, model_validator

from . import characters, config
from .plan import BANNED, FORMATS, TEXTY, Plan, Shot, Youtube, _stem, check_bilingual, extract_json

log = logging.getLogger("videomaker")
MAX_REFS = 3  # 每鏡最多幾張參考圖（H3 ref2va workflow 的 LoadImage 節點數，也是提示詞 <Picture N> 的上限）
MAX_WPS = 3.0  # 對白語速上限（詞／秒）：超過代表一鏡塞太多話，H3 會唸得很趕或唸不完
WORD = re.compile(r"[A-Za-z']+")
BEATS = ("hook", "setup", "conflict", "reaction", "punchline")


def norm(text: str) -> str:
    """字幕與對白比對用：只留小寫英數與撇號、空白折成一格；標點與大小寫不同不算不一致。"""
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


class Line(BaseModel):
    speaker: str  # 角色 ID
    text: str


class ScriptShot(BaseModel):
    shot_id: int
    duration: float
    characters: list[str] = []
    dialogue: list[Line] = []
    subtitle: str = ""  # 兒童英文對話：畫面上的字幕，必須與 dialogue 逐字一致（程式驗證）
    visual_prompt: str = ""
    action: str = ""
    emotion: str = ""
    camera: str = ""
    reference_assets: list[str] = []  # '<角色 ID>/<視角>'，如 dog_01/front；給 h3_ref2va 當參考圖
    beats: list[str] = []  # 有對白的動畫：這一鏡負責哪些故事段落

    def spoken(self) -> str:
        return " ".join(line.text for line in self.dialogue)


class _Script(BaseModel):
    KIND: ClassVar[str]
    max_words: ClassVar[int] = 14  # 一句對白最多幾個詞
    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    style: str
    characters: list[str] = Field(min_length=1)
    shots: list[ScriptShot]
    duration: float = 0  # 由程式加總覆寫，LLM 填什麼都不算
    youtube: Youtube = Youtube()

    def hook_ok(self) -> None:
        """各片型自己的內容規則；錯誤用 ValueError 回報。"""

    @model_validator(mode="after")
    def _check(self):
        spec = SPECS[self.KIND]
        if not self.style.isascii() or BANNED.search(self.style):
            raise ValueError("style 必須是純英文且不含禁用字詞")
        if len(set(self.characters)) != len(self.characters):
            raise ValueError("characters 不可重複")
        chars = {
            cid: characters.load(cid) for cid in self.characters
        }  # 不存在 → UnknownCharacter（ValueError）
        lo, hi = spec.shots
        if not lo <= len(self.shots) <= hi:
            raise ValueError(f"shots 數量應為 {lo}–{hi}，得到 {len(self.shots)}")
        if [s.shot_id for s in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("shot_id 必須是 1, 2, 3… 連號")
        smin, smax = config.H3_SHOT_SEC
        for s in self.shots:
            where = f"shot {s.shot_id}"
            if not smin <= s.duration <= smax:
                raise ValueError(
                    f"{where} duration {s.duration} 必須在 {smin:g}–{smax:g} 秒（H3 單鏡的訓練範圍）"
                )
            s.characters = (
                s.characters or list(dict.fromkeys(line.speaker for line in s.dialogue)) or self.characters
            )
            if bad := set(s.characters) - set(self.characters):
                raise ValueError(f"{where} 的角色 {sorted(bad)} 不在 characters {self.characters} 內")
            for line in s.dialogue:
                if line.speaker not in s.characters:
                    raise ValueError(
                        f"{where} 的說話者 {line.speaker!r} 不在該鏡 characters {s.characters} 內"
                    )
                self._line_rules(where, line.text)
            if len(WORD.findall(s.spoken())) > MAX_WPS * s.duration:
                raise ValueError(
                    f"{where} 對白 {len(WORD.findall(s.spoken()))} 個詞，{s.duration:g} 秒內說不完（上限 {MAX_WPS:g} 詞／秒）"
                )
            for what, text in (
                ("visual_prompt", s.visual_prompt),
                ("action", s.action),
                ("emotion", s.emotion),
                ("camera", s.camera),
            ):
                if not text.isascii():
                    raise ValueError(f"{where} {what} 必須是純英文")
                if m := TEXTY.search(text) or BANNED.search(text):
                    raise ValueError(f"{where} {what} 含畫面不可出現的文字類物件或禁用字詞：{m.group(0)}")
            s.reference_assets = (
                s.reference_assets
                or [  # 沒寫就每個在場角色各取 front（沒有 front 就取第一個視角）
                    f"{cid}/{'front' if 'front' in chars[cid].references else next(iter(chars[cid].references))}"
                    for cid in s.characters
                ]
            )
            if len(s.reference_assets) > MAX_REFS:
                raise ValueError(
                    f"{where} reference_assets 最多 {MAX_REFS} 張（H3 每鏡的參考圖上限），目前 {len(s.reference_assets)} 張"
                )
            for asset in s.reference_assets:
                characters.check_asset(asset, set(s.characters))
        total = sum(s.duration for s in self.shots)
        if not spec.total[0] <= total <= spec.total[1]:
            raise ValueError(f"總長 {total:g} 秒不在 {spec.total[0]:g}–{spec.total[1]:g} 秒")
        self.duration = round(total, 1)
        if not sum(len(s.dialogue) for s in self.shots):
            raise ValueError("整集沒有任何對白")
        self.hook_ok()
        return self

    def _line_rules(self, where: str, text: str) -> None:
        if not text.isascii() or not 1 <= len(WORD.findall(text)) <= self.max_words:
            raise ValueError(f"{where} 對白必須是純英文、1–{self.max_words} 個詞：{text[:40]!r}")
        if m := BANNED.search(text):
            raise ValueError(f"{where} 對白含禁用字詞：{m.group(0)}")


class EnglishDialogue(_Script):
    KIND = "kids_english_dialogue"
    max_words = 10  # 句型要簡單：一句最多 10 個詞、單字最多 10 個字母
    title: str
    topic: str
    learning_goal: str
    vocabulary: list[str] = Field(min_length=3, max_length=8)

    def _line_rules(self, where: str, text: str) -> None:
        super()._line_rules(where, text)
        if long := [w for w in WORD.findall(text) if len(w) > 10]:
            raise ValueError(f"{where} 對白有太難的長單字 {long}（國小程度，單字最多 10 個字母）")

    def hook_ok(self) -> None:
        if not self.learning_goal.isascii() or not self.topic.isascii():
            raise ValueError("learning_goal 與 topic 必須是純英文")
        spoken = {_stem(w.lower()) for s in self.shots for w in WORD.findall(s.spoken())}
        if missing := [
            v for v in self.vocabulary if not re.fullmatch(r"[a-z]{2,10}", v) or _stem(v) not in spoken
        ]:
            raise ValueError(f"vocabulary {missing} 必須是小寫單字（最多 10 字母），且出現在對白裡")
        for s in self.shots:
            if not s.dialogue or not s.subtitle or not s.visual_prompt:
                raise ValueError(f"shot {s.shot_id} 必須有 dialogue、subtitle、visual_prompt")
            if norm(s.subtitle) != norm(s.spoken()):  # 字幕就是對白，不另外發揮
                raise ValueError(f"shot {s.shot_id} 的 subtitle 必須與 dialogue 逐字一致（忽略標點與大小寫）")
        if len({line.speaker for s in self.shots for line in s.dialogue}) < 2:
            raise ValueError("英文對話至少要兩個角色輪流說話（A 問、B 答）")
        last = self.shots[-1].dialogue[-1].text
        if not last.rstrip().endswith("?") or not re.search(r"\byou\b", last, re.IGNORECASE):
            raise ValueError("最後一句必須是問觀眾的問句（含 you，以 ? 結尾），如 'What do YOU want to eat?'")


# 動物短劇的固定主角（寫實哈士奇）；其他種類的角色清單不放它們，動物短劇也只能用它們
LEADS = ("husky_01", "husky_02")


class AnimalSitcom(_Script):
    KIND = "animal_sitcom"
    episode_title: str
    setting: str
    hook: str
    reference_assets: list[str] = []  # 整集用到的參考圖（由程式彙整每鏡的）

    def hook_ok(self) -> None:
        if not (self.setting.isascii() and self.hook.isascii() and self.hook.strip()):
            raise ValueError("setting 與 hook 必須是純英文且 hook 不可為空")
        if set(self.characters) != set(LEADS):
            raise ValueError(f"動物短劇的主角固定是 {list(LEADS)}，characters 必須剛好是這兩隻")
        for s in self.shots:
            if not (s.action.strip() and s.emotion.strip() and s.camera.strip()):
                raise ValueError(f"shot {s.shot_id} 必須有 action、emotion、camera")
        self.reference_assets = list(dict.fromkeys(a for s in self.shots for a in s.reference_assets))


class DialogueAnimation(_Script):
    KIND = "dialogue_animation"
    title: str
    synopsis: str
    setting: str

    def hook_ok(self) -> None:
        if not (self.synopsis.isascii() and self.setting.isascii()):
            raise ValueError("synopsis 與 setting 必須是純英文")
        if len(self.characters) < 2:
            raise ValueError("有對白的動畫至少要兩個角色互動")
        order = [BEATS.index(b) if b in BEATS else -1 for s in self.shots for b in s.beats]
        if -1 in order or order != sorted(order) or set(order) != set(range(len(BEATS))):
            raise ValueError(f"各鏡 beats 依序必須涵蓋 {list(BEATS)}（可一鏡多段，不可倒退或缺少）")
        for s in self.shots:
            if not (
                s.dialogue
                and s.action.strip()
                and s.emotion.strip()
                and s.camera.strip()
                and s.visual_prompt.strip()
            ):
                raise ValueError(
                    f"shot {s.shot_id} 必須有 dialogue、action、emotion、camera、visual_prompt（不能只是角色站著說話）"
                )


@dataclass(frozen=True)
class Spec:
    schema: type[_Script]
    shots: tuple[int, int]  # 鏡頭數範圍
    total: tuple[float, float]  # 企劃總長（秒）
    renderer: str  # 預設 renderer；VMK_RENDERER_<KIND> 可覆寫


SPECS = {
    "kids_english_dialogue": Spec(EnglishDialogue, (3, 5), (20, 40), "h3_fl2va"),
    "animal_sitcom": Spec(AnimalSitcom, (3, 6), (20, 45), "h3_ref2va"),
    "dialogue_animation": Spec(DialogueAnimation, (3, 6), (30, 60), "h3_fl2va"),
}
SLACK = (2, 5)  # 成片長度容許：H3 的幀數會往上取整到 17k+5 格（每鏡最多多約 0.7 秒），所以上限放寬


def renderer_for(kind: str) -> str:
    """kind 用哪個 renderer：環境變數 VMK_RENDERER_<KIND> 覆寫，否則取 SPECS 的預設。kids／trivia 永遠是 wan。"""
    if kind not in SPECS:
        return "wan"
    return os.getenv(f"VMK_RENDERER_{kind.upper()}", "").strip() or SPECS[kind].renderer


def qc_range(kind: str, fmt: str) -> tuple[float, float]:
    """QC 的成片長度範圍（秒）。對白類跟著企劃總長走；兒童音樂與小知識仍照 FORMATS。"""
    if kind in SPECS:
        lo, hi = SPECS[kind].total
        return lo - SLACK[0], hi + SLACK[1]
    return FORMATS[fmt]["min_total"], FORMATS[fmt]["max_total"]


# ---------- 提示詞 ----------
def catalog(kind: str) -> str:
    out = []
    for cid in characters.ids():
        if (cid in LEADS) != (
            kind == "animal_sitcom"
        ):  # 動物短劇只列主角；其他種類不列（寫實配卡通畫風會違和）
            continue
        c = characters.load(cid)
        out.append(
            f'- {cid}: {c.name}, {c.species}' + (f" ({c.breed})" if c.breed else "")
            + f"; {c.personality}; reference views: {', '.join(sorted(c.references))}"
        )  # fmt: skip
    return "\n".join(out)


STYLES = {  # 畫風規則：動物短劇的主角是寫實照片參考圖，畫風要跟著寫實；其他種類維持卡通
    "": "flat 2D cartoon, soft pastel colors, kid-friendly",
    "animal_sitcom": "photorealistic, natural soft lighting, cinematic, family-friendly",
}

_COMMON = """You write structured scripts for short vertical (9:16) YouTube videos where animated animal characters TALK.
Return ONE JSON object only, no markdown. Schema (shared fields): {"slug": "lowercase-hyphen-words", "style": str, "characters": [character ids],
 "shots": [...], "youtube": {"title": str, "description": str, "tags": [str]}}
Characters: use ONLY these existing character ids; never invent a character or a new id:
%(catalog)s
Rules: every field except youtube.* is PURE ENGLISH (ASCII only). style: one sentence, %(style)s, the same for every shot.
A shot = ONE continuous clip of %(smin)g-%(smax)g seconds (duration, a number) with ONE simple setting and 1-2 characters on screen.
Dialogue is a list of {"speaker": "<character id>", "text": "..."}; speakers must be characters on screen in that shot. Spoken speed is at most %(wps)g words per second of the shot.
reference_assets (optional, may be []) entries are written "<character id>/<view>" with a SLASH, e.g. "%(eg)s", using only the reference views listed for that character above; at most %(refs)d per shot.
Never put text, letters, numbers, signs, logos or posters in visual descriptions: subtitles are added by the program.
No existing cartoon characters, brands, artist names or song titles. Nothing scary, violent or unsafe for children.
The youtube fields are BILINGUAL: youtube.title = "<繁體中文標題>｜<English title>" (max 100 characters in total, Traditional Chinese as used in Taiwan, never Simplified);
youtube.description = 2 short Traditional Chinese sentences, a blank line, 2 short English sentences, a blank line, then 3-5 English hashtags;
youtube.tags = 6-10 tags mixing Traditional Chinese and English (at least 4 English)."""

_PER_KIND = {
    "kids_english_dialogue": """THIS IS A KIDS ENGLISH LESSON for elementary-school learners. Extra keys: "title": str, "topic": str, "learning_goal": str (one clear learning point, e.g. 'ask and answer what you want to eat'),
"vocabulary": [3-8 lowercase words that appear in the dialogue].
Shot keys: {"shot_id": 1, "duration": 6, "characters": [...], "dialogue": [{"speaker": ..., "text": ...}], "subtitle": str, "visual_prompt": str}.
Structure: hook, character A asks a simple question, character B answers, a variation (another answer), and the LAST line asks the viewer to answer, e.g. "What do YOU want to eat?".
Natural, simple sentences: at most 10 words per sentence, every word at most 10 letters, common everyday words. "subtitle" must equal the spoken dialogue of that shot word for word.
Use 2 characters. Total %(tmin)g-%(tmax)g seconds in %(nmin)d-%(nmax)d shots.""",
    "animal_sitcom": """THIS IS A FUNNY ANIMAL SITCOM about everyday life (office, home, food, napping, going out, games, small misunderstandings, a small twist at the end).
Extra keys: "episode_title": str, "setting": str, "hook": str (the first-seconds attention grabber). The cast is EXACTLY the two husky leads: "characters" must be ["husky_01", "husky_02"] (Meatball and HappySunny), and nobody else appears on screen.
Meatball and HappySunny are real-looking huskies with different personalities: let their personalities drive the jokes.
Craft rules: the FIRST shot must work as a hook on its own (a visible conflict or an odd image), never something that only makes sense after the next line.
Every shot must change something visible (who has the snack, who is winning, how they feel); merge or drop a shot that changes nothing.
Show, do not tell: never let dialogue repeat what the picture already shows; keep only lines that carry an action, a want or a conflict, and cut the rest.
Shot keys: {"shot_id": 1, "duration": 6, "characters": [...], "action": str, "emotion": str, "camera": str, "dialogue": [...], "reference_assets": ["husky_01/front"]}.
reference_assets entries are "<character id>/<view>" using the listed reference views, e.g. "husky_02/happy" for a joyful shot. Keep the same character look in every shot.
Total %(tmin)g-%(tmax)g seconds in %(nmin)d-%(nmax)d shots. Example arc: Meatball spots the last snack -> sneaks toward it -> HappySunny cheerfully catches him -> they argue about sharing -> a small twist (the snack is gone anyway).""",
    "dialogue_animation": """THIS IS A SHORT ANIMATED STORY (entertainment, characters interacting). Extra keys: "title": str, "synopsis": str, "setting": str.
Shot keys: {"shot_id": 1, "duration": 8, "characters": [...], "action": str, "emotion": str, "camera": str, "dialogue": [...], "visual_prompt": str, "beats": ["hook"], "reference_assets": [...]}.
Story structure over the shots, in order: hook -> setup -> conflict -> reaction -> punchline. Each shot lists the "beats" it covers (a shot may cover more than one, but order never goes back
and all five appear). Every shot needs a visible action, an emotion, a camera instruction and at least one line of dialogue; never just characters standing and talking.
Total %(tmin)g-%(tmax)g seconds in %(nmin)d-%(nmax)d shots.""",
}


def system_prompt(kind: str) -> str:
    spec, smin_smax = SPECS[kind], config.H3_SHOT_SEC
    base = _COMMON % {
        "catalog": catalog(kind),
        "style": STYLES.get(kind, STYLES[""]),
        "eg": "husky_01/front" if kind == "animal_sitcom" else "dog_01/front",
        "smin": smin_smax[0],
        "smax": smin_smax[1],
        "wps": MAX_WPS,
        "refs": MAX_REFS,
    }
    kind_part = _PER_KIND[kind] % {
        "tmin": spec.total[0],
        "tmax": spec.total[1],
        "nmin": spec.shots[0],
        "nmax": spec.shots[1],
    }
    return f"{base}\n{kind_part}"


def topic_prompt(kind: str, n: int, avoid: list[str]) -> str:
    what = {
        "kids_english_dialogue": "a simple everyday English conversation lesson for children (ordering food, favorite animals, greetings, "
        "asking for permission, finding things, telling time, going to the park)",
        "animal_sitcom": "a funny everyday situation for a short animal sitcom starring two husky dogs, Meatball and HappySunny (home, snacks, napping, walks, games, small misunderstandings)",
        "dialogue_animation": "a small story with a hook, a problem, a reaction and a punchline for a short animated dialogue video",
    }[kind]
    return (
        f"Suggest {n} different topics, each {what}. Each topic is a short phrase in Traditional Chinese as used in Taiwan "
        "(繁體中文, NEVER Simplified characters) of 4-16 characters. Safe for children, nothing scary, no brands or existing cartoon characters. "
        f"Do NOT repeat or closely resemble any of: {'、'.join(avoid[-200:]) or '(none)'}. "
        'Return ONLY JSON: {"topics": ["...", "..."]}'
    )


# ---------- 組成 Plan ----------
def shot_prompt(script: _Script, shot: ScriptShot) -> str:
    """關鍵幀（FLUX）用的靜態畫面描述：這一鏡出現的角色（固定外觀）＋ 場景與動作。對白不進圖片提示詞。"""
    who = ", ".join(characters.load(cid).prompt() for cid in shot.characters)
    scene = getattr(script, "setting", "")
    action = ". ".join(
        x
        for x in (shot.visual_prompt, shot.action, f"{shot.emotion} expression" if shot.emotion else "")
        if x
    )
    return ", ".join(x for x in (who, scene, action) if x)


def to_plan(script: _Script, topic: str) -> Plan:
    plan = Plan(
        topic=topic,  # 由程式覆寫成輸入的主題：--resume 靠 topic 找作業
        slug=script.slug,
        style=script.style,
        character="",  # 每鏡的角色描述放在 shot.prompt（多角色時不能每鏡都畫全員）
        shots=[Shot(id=s.shot_id, prompt=shot_prompt(script, s)) for s in script.shots],
        youtube=script.youtube,
        kind=script.KIND,
        script=script.model_dump(exclude={"youtube", "slug", "style"}) | {"style": script.style},
    )
    return plan


def unload_llm(model: str) -> None:
    """企劃寫完就卸載 LLM：GPU 階段開始前不要讓它白占統一記憶體（失敗只警告，ComfyUI 的記憶體檢查會再把關）。"""
    try:
        base = config.LLM_URL.rstrip("/").removesuffix("/v1")
        requests.post(
            f"{base}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30
        ).raise_for_status()
    except requests.RequestException as e:
        log.warning("無法卸載企劃模型 %s：%s", model, e)


def make_script(kind: str, topic: str) -> Plan:
    """LLM 寫腳本 → 驗證；驗證失敗把錯誤丟回去重寫，最多 3 次。"""
    spec, model = SPECS[kind], config.plan_model(kind)
    msgs = [
        {"role": "system", "content": system_prompt(kind)},
        {"role": "user", "content": f'Topic (Traditional Chinese label): "{topic}". Write the script JSON.'},
    ]
    err = None
    try:
        for attempt in range(3):
            r = requests.post(
                f"{config.LLM_URL}/chat/completions",
                json={"model": model, "messages": msgs, "temperature": 0.7},
                timeout=600,
            )
            r.raise_for_status()
            try:
                content = r.json()["choices"][0]["message"]["content"]
            except (
                ValueError,
                KeyError,
                IndexError,
                TypeError,
            ) as e:  # 200 但內容壞掉（錯誤頁、choices 為空）：算一次嘗試，不整支失敗
                err = e
                log.warning("%s 腳本第 %d 次回應格式不對，重試：%r", kind, attempt + 1, e)
                continue
            try:
                script = spec.schema.model_validate(json.loads(extract_json(content)))
                check_bilingual(script.youtube)
                return to_plan(script, topic)
            except ValueError as e:  # JSON 解析、pydantic 驗證、UnknownCharacter 都是 ValueError 子類
                err = e
                log.warning("%s 腳本第 %d 次驗證失敗，請 LLM 重寫：%s", kind, attempt + 1, e)
                msgs += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"Invalid: {e}. Fix and return JSON only."},
                ]
        raise RuntimeError(f"{kind} 腳本重試 3 次仍失敗：{err}")
    finally:
        unload_llm(model)
