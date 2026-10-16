"""各階段：企劃 → 音樂＋拍點 → 關鍵幀 → 圖生影片 → 合成 → QC。每階段輸出已存在就跳過（可續跑）。"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import textwrap
import time
from contextlib import contextmanager
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from . import audio, config, visual_qc, wiki
from .comfy import Comfy, fill, models_in
from .plan import FACTS_PER, FORMATS, Plan, build_layout, make_plan, shot_math

log = logging.getLogger("videomaker")
STAGES = ["plan", "music", "keyframes", "clips", "mux", "qc"]
SIZE = (704, 1280)  # Wan 2.2 TI2V-5B 官方只支援 704x1280，不能亂改
FPS = 24  # 5B 原生 24fps，不補幀
REUSE_OFFSETS = (0, 24, 12)  # 長片重複使用同一鏡頭時，第 1、2、3 次從第 0、24、12 幀開始取
LENGTH = 121  # 幀數必須是 4n+1；121 幀 @24fps ≈ 5.04 秒
CAPTION_WIDTH = 26  # 字幕每行字元數：1080 寬、字級 54 的 DejaVu Sans Bold 約排 26～27 個字元


def _git_hash() -> str:
    try:
        h = subprocess.run(
            ["git", "-C", str(config.ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(config.ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return h + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "no-commit"


class Job:
    def __init__(self, d: Path):
        self.dir = d
        self.plan = Plan.model_validate_json((d / "plan.json").read_text())
        self.meta = json.loads((d / "meta.json").read_text())
        self.size = SIZE
        self.fmt = self.meta.get("format", "short")

    def p(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    def save_meta(self):
        self.meta["git"] = _git_hash()
        self.p("meta.json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=2) + "\n")

    def windows(self) -> list[tuple[int, float, float, float]]:
        """實際放進成片的素材區段（秒）：(鏡頭 id, 素材起點, 素材終點, 成片起點)；視覺 QC 只查這些畫面。"""
        beats = json.loads(self.p("beats.json").read_text())
        bounds = audio.frame_bounds(beats["cuts"], FPS)
        return [
            (sid, start / FPS, (start + frames) / FPS, bounds[i] / FPS)
            for i, (sid, start, frames) in enumerate(slot_windows(self, bounds))
        ]

    @contextmanager
    def timed(self, stage: str):
        t = time.monotonic()
        try:
            yield
        finally:  # 失敗也記錄；續跑時累加
            self.meta["timings"][stage] = round(self.meta["timings"].get(stage, 0) + time.monotonic() - t, 1)
            self.save_meta()
        log.info("[%s] 完成，累計 %.1fs → %s", stage, self.meta["timings"][stage], self.dir)


def find_job(topic: str) -> Job | None:
    for d in sorted(config.WORK.glob("*/plan.json"), reverse=True):
        if json.loads(d.read_text())["topic"] == topic:
            return Job(d.parent)
    return None


# 照片模式：每鏡一個「隨節奏律動」的動作提示詞（動作小而慢，Wan 才不會把真實照片扭曲變形），依鏡頭輪流使用；key = 狗的數量
MOTIONS = {
    1: [
        "the dog bobs its head up and down to a steady cheerful beat, ears bouncing, tail wagging, gentle movement, static camera",
        "the dog sways its body left and right in rhythm, happy expression, tail wagging, static camera",
        "the dog nods its head in time with the music, happy expression, ears flopping, static camera",
        "the dog bounces up and down playfully to the beat, fur moving softly, static camera",
        "the dog tilts its head side to side rhythmically, blinking, tail wagging, static camera",
    ],
    2: [
        "both dogs bob their heads up and down to a steady cheerful beat, ears bouncing, tails wagging, gentle movement, static camera",
        "both dogs sway their bodies left and right in rhythm, happy expressions, tails wagging, static camera",
        "the two dogs nod their heads in time with the music, happy expressions, ears flopping, static camera",
        "both dogs bounce up and down playfully to the beat, fur moving softly, static camera",
        "both dogs tilt their heads side to side rhythmically, blinking, tails wagging, static camera",
    ],
}
# 兩隻狗同框時每鏡換一個場景，5 鏡才不會重複
SCENES = [
    "sitting side by side on a sunny green lawn",
    "sitting side by side on a cozy rug in a bright living room",
    "sitting side by side in a park with soft green trees in the background",
    "sitting side by side on a sandy beach at golden hour",
    "sitting side by side in a garden full of colorful flowers",
    "sitting side by side in a snowy field under a soft blue sky",
    "sitting side by side on an autumn forest path covered with orange leaves",
]
# 7 個場景與 5 張照片（週期 5）互質：前 35 個鏡頭的「照片組合＋場景」都不重複，5 分鐘版的 20 個獨特鏡頭才不會看起來一樣
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _use_photos(plan: Plan, srcs: list[Path], d: Path) -> list[dict]:
    """用現成照片當每鏡的關鍵幀。srcs 為 1 個資料夾（單隻狗）或 2 個（兩隻狗同框，每鏡各取一張去合成）。
    shot 提示詞改成「照片描述（同名 .txt，沒有就 a dog）+ 律動動作」，style/character 清空。
    兩隻狗時 .txt 只寫外觀（品種、毛色），場景由 SCENES 決定。照片比鏡頭少就輪流重複使用。"""
    if not 1 <= len(srcs) <= 2:
        raise ValueError("--images 只能給 1 個資料夾（單隻狗）或 2 個（兩隻狗同框）")
    lists = []
    for src in srcs:
        files = sorted(f for f in src.iterdir() if f.suffix.lower() in PHOTO_EXTS)
        if not files:
            raise ValueError(f"{src} 底下沒有圖片（{'/'.join(sorted(PHOTO_EXTS))}）")
        if len(files) > len(plan.shots):
            log.warning(
                "%s 有 %d 張，多於鏡頭數 %d，只用前 %d 張", src, len(files), len(plan.shots), len(plan.shots)
            )
        lists.append(files)
    k = len(srcs)
    (d / "source").mkdir()
    used = []
    for i, shot in enumerate(plan.shots):
        picked = [fs[i % len(fs)] for fs in lists]
        descs = [
            f.with_suffix(".txt").read_text(encoding="utf-8").strip()
            if f.with_suffix(".txt").exists()
            else "a dog"
            for f in picked
        ]
        for f, desc in zip(picked, descs, strict=True):
            if not desc.isascii():  # 中文會被畫成亂碼文字
                raise ValueError(f"{f.with_suffix('.txt')} 必須是純英文描述")
        for j, f in enumerate(picked):
            shutil.copy(f, d / "source" / f"{shot.id:02d}_{'ab'[j]}{f.suffix.lower()}")
        scene = SCENES[i % len(SCENES)]
        who = f"{descs[0]}. " if k == 1 else f"{descs[0]} and {descs[1]}, {scene}. "
        shot.prompt = who + MOTIONS[k][i % 5]
        used.append(
            {
                "shot": shot.id,
                "files": [f.name for f in picked],
                "desc": descs,
                "scene": scene,
                "sha256": [hashlib.sha256(f.read_bytes()).hexdigest() for f in picked],
            }
        )
    plan.style = plan.character = ""
    return used


def new_job(topic: str, images: list[Path] | None = None, fmt: str = "short", kind: str = "kids") -> Job:
    t = time.monotonic()
    bpm = config.DEFAULT_BPM
    _, n_slots, _ = shot_math(bpm, fmt)
    n_unique = FORMATS[fmt]["unique"] or n_slots
    reuse = n_unique < n_slots  # 獨特鏡頭比時段少 → 要重複使用（long、xl）；用這個判斷，不要寫死片型名稱
    article = None
    if kind == "trivia":  # 小知識：topic 就是英文維基百科條目名稱，先抓條目，LLM 只能依它寫
        if fmt not in FACTS_PER or images:
            raise ValueError(f"小知識只支援片型 {'／'.join(FACTS_PER)}，且不能搭配照片模式")
        article = wiki.fetch(topic)
    extra = {"article": article, "n_facts": FACTS_PER[fmt]} if article else {}  # 兒童音樂維持原本的呼叫方式
    plan = make_plan(topic, n_unique, bpm, long=reuse, **extra)
    if reuse:
        plan.layout = build_layout(n_unique, n_slots, plan.seed)
    day = f"{datetime.now().astimezone():%Y-%m-%d}"
    d = config.WORK / f"{day}_{plan.slug}"
    i = 2
    while d.exists():  # 同主題同日再跑一次 → 另開資料夾
        d = config.WORK / f"{day}_{plan.slug}-{i}"
        i += 1
    d.mkdir(parents=True)
    used = _use_photos(plan, images, d) if images else None
    (d / "plan.json").write_text(plan.model_dump_json(indent=2) + "\n")
    meta = {
        "topic": topic,
        "format": fmt,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "timings": {"plan": round(time.monotonic() - t, 1)},
        "models": {},
        "shots": {},
        "llm": config.LLM_MODEL,
        "bpm_set": bpm,
    }
    if article:  # 來源證據：條目標題、固定版本的連結與授權
        meta["kind"] = "trivia"
        meta["wikipedia"] = {"title": article.title, "url": article.permalink, "license": "CC BY-SA 4.0"}
    if used:
        meta["images"] = used  # 使用者提供的照片（自有素材），記錄檔名與 sha256 當來源證據
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    job = Job(d)
    job.save_meta()
    return job


def _ff(*args) -> str:
    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-nostats", *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode:
        raise RuntimeError(f"ffmpeg 失敗：{' '.join(map(str, args))}\n{r.stderr[-1500:]}")
    return r.stderr


def _measure(path: Path, af: str = "") -> dict:
    """loudnorm 量測（print_format=json），回傳 input_i 等欄位。"""
    err = _ff(
        "-i", path, "-vn", "-af", f"{af}loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"
    )
    return json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.DOTALL).group(0))


# ---------- 音樂 + 拍點 ----------
def music(job: Job):
    if job.p("music.wav").exists() and job.p("beats.json").exists():
        return
    pl = job.plan
    _, n, total = shot_math(pl.music.bpm, job.fmt)
    caption = (
        pl.music.caption
        if "instrumental" in pl.music.caption.lower()
        else f"{pl.music.caption}, instrumental"
    )
    c = Comfy()
    with job.timed("music"):
        for attempt in range(3):
            c.prepare()
            # spec 寫總長 + 2 秒，但 ACE-Step 實測會提早演完（22s 只演約 16s、30s 約 24s），故多要 10 秒，重試再加長
            seed, dur = pl.seed + attempt, round(total + 10 + 4 * attempt, 1)
            wf = fill(
                "music",
                tags=caption,
                lyrics="[Instrumental]",
                seed=seed,
                bpm=pl.music.bpm,
                key=pl.music.key,
                timesignature=pl.music.time_signature.split("/")[0],
                duration=dur,
            )
            c.run(wf, job.p("music_raw.flac"), timeout=1200)
            _ff("-i", job.p("music_raw.flac"), "-ar", 44100, "-ac", 2, job.p("music.wav"))
            try:
                info = audio.analyze(job.p("music.wav"), pl.music.bpm, n)
                if abs(info["bpm_measured"] - pl.music.bpm) / pl.music.bpm > 0.05:
                    raise ValueError(f"實測 BPM {info['bpm_measured']} 與設定 {pl.music.bpm} 差 >5%")
            except ValueError as e:
                log.warning("音樂第 %d 次不合格，重新生成：%s", attempt + 1, e)
                continue
            info |= {"seed": seed, "caption": caption, "attempts": attempt + 1}
            job.p("beats.json").write_text(json.dumps(info, indent=2) + "\n")
            job.meta |= {"bpm_measured": info["bpm_measured"], "cuts": info["cuts"], "music_seed": seed}
            job.meta["models"]["music"] = models_in(wf)
            c.free()
            return
        raise RuntimeError("音樂 3 次都不合格（BPM 偏差或長度不足）")


# ---------- 關鍵幀 ----------
def _keyframes_from_photos(job: Job, todo: list):
    w, h = job.size
    c = None
    with job.timed("keyframes"):
        for s in todo:
            srcs = sorted(job.p("source").glob(f"{s.id:02d}_*"))
            dest = job.p("keyframes", f"{s.id:02d}.png")
            dest.parent.mkdir(exist_ok=True)
            if len(srcs) == 1:
                src = srcs[0]
                iw, ih = map(
                    int,
                    subprocess.run(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=width,height",
                            "-of",
                            "csv=p=0:s=x",
                            src,
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    .stdout.strip()
                    .split("x"),
                )
                if abs(iw / ih - w / h) > 0.15:  # 放大裁切到 9:16，比例差太多會裁掉主體
                    log.warning(
                        "%s 是 %dx%d，與 %dx%d 比例差很多，裁切後主體可能不完整", src.name, iw, ih, w, h
                    )
                _ff(
                    "-i",
                    src,
                    "-vf",
                    f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                    "-frames:v",
                    1,
                    dest,
                )
                job.meta["shots"][str(s.id)] = {"prompt": job.plan.full_prompt(s), "source": src.name}
            else:  # 兩隻狗：合成同框，輸出直接是影片尺寸
                if c is None:
                    c = Comfy()
                    c.prepare()
                info = next(u for u in job.meta["images"] if u["shot"] == s.id)
                names = [c.upload(p, f"{job.dir.name}_{p.name}") for p in srcs]
                prompt = (
                    f"Put the dog in image 1 ({info['desc'][0]}) and the dog in image 2 ({info['desc'][1]}) together in one photo, {info['scene']}. "
                    "Keep each dog's face, fur color and markings exactly as in its photo. Both dogs fully visible, facing the camera, vertical composition. "
                    "No text, numbers or name tags anywhere in the image."
                )
                seed = job.plan.seed + s.id
                wf = fill(
                    "compose", prompt=prompt, image1=names[0], image2=names[1], seed=seed, width=w, height=h
                )
                c.run(wf, dest, timeout=1800)
                job.meta["shots"][str(s.id)] = {
                    "prompt": job.plan.full_prompt(s),
                    "source": [p.name for p in srcs],
                    "compose_prompt": prompt,
                    "compose_seed": seed,
                }
                job.meta["models"]["keyframes"] = models_in(wf)
            job.save_meta()
    if c:
        c.free()


def _has_black_bars(path: Path, w: int, h: int) -> bool:
    """FLUX 偶爾會畫出上下（或左右）黑邊的 letterbox 圖；cropdetect 偵測有效畫面範圍，縮水超過 3% 就算有黑邊。"""
    err = _ff(
        "-loop",
        1,
        "-framerate",
        25,
        "-t",
        0.5,
        "-i",
        path,
        "-vf",
        "cropdetect=limit=24:round=2:reset=0",
        "-f",
        "null",
        "-",
    )
    found = re.findall(r"crop=(\d+):(\d+):", err)
    return bool(found) and (int(found[-1][0]) < 0.97 * w or int(found[-1][1]) < 0.97 * h)


def keyframes(job: Job):
    todo = [s for s in job.plan.shots if not job.p("keyframes", f"{s.id:02d}.png").exists()]
    if not todo:
        return
    if job.meta.get("images"):  # 照片模式：不呼叫 FLUX
        return _keyframes_from_photos(job, todo)
    c = Comfy()
    c.prepare()
    w, h = job.size
    with job.timed("keyframes"):
        for s in todo:
            dest = job.p("keyframes", f"{s.id:02d}.png")
            for attempt in range(3):  # 有黑邊就換 seed 重生，最多 3 次
                seed = job.plan.seed + s.id + 1000 * attempt  # 同一支影片固定 seed 基底
                wf = fill("keyframe", prompt=job.plan.full_prompt(s), seed=seed, width=w, height=h)
                c.run(wf, dest, timeout=900)
                if not _has_black_bars(dest, w, h):
                    break
                log.warning("shot %d 關鍵幀有黑邊，換 seed 重生（%d/3）", s.id, attempt + 1)
            job.meta["shots"][str(s.id)] = {"seed": seed, "prompt": job.plan.full_prompt(s)}
            job.meta["models"]["keyframes"] = models_in(wf)
            job.save_meta()
    c.free()


# ---------- 圖生影片 ----------
# 照片模式：Wan 會把「兩隻狗並排正面坐著、頭在動」聯想成「主人拿玩具逗狗拍照」，實測 20 個影片有約 14 個出現藍色棒狀玩具、
# 約 7 個還有人的手。負向提示詞只在照片模式加（其他題材的牙刷、星星玩具等合法道具不能被壓掉）
PHOTO_NEG = "，人，人手，手臂，手持物，棍子，玩具，逗狗棒，球，蓝色玩具，蓝色棍子，蓝色骨头，多餘的物體"
PHOTO_SUFFIX = (
    ", only the dogs are in the scene"  # 正向用不含否定字的寫法（Wan 對 "no hands" 反而容易畫出手）
)


def clip_workflow(job: Job, shot, image: str, seed: int) -> dict:
    """某一鏡的 Wan workflow。照片模式會加上抑制人手與玩具的負向與正向提示詞。"""
    photo = bool(job.meta.get("images"))
    w, h = job.size
    wf = fill(
        "clip",
        prompt=job.plan.full_prompt(shot) + (PHOTO_SUFFIX if photo else ""),
        image=image,
        seed=seed,
        width=w,
        height=h,
        length=LENGTH,
        steps=config.STEPS,
    )
    if photo:
        wf["neg"]["inputs"]["text"] += PHOTO_NEG
    return wf


def clips(job: Job):
    todo = [s for s in job.plan.shots if not job.p("clips", f"{s.id:02d}.mp4").exists()]
    if not todo:
        return
    c = Comfy()
    c.prepare()
    with job.timed("clips"):
        for s in todo:
            img = c.upload(job.p("keyframes", f"{s.id:02d}.png"), f"{job.dir.name}_{s.id:02d}.png")
            seed = job.plan.seed + s.id
            wf = clip_workflow(job, s, img, seed)
            t = time.monotonic()
            c.run(wf, job.p("clips", f"{s.id:02d}.mp4"), timeout=3600)
            job.meta["shots"].setdefault(str(s.id), {}).update(
                clip_seed=seed,
                clip_sec=round(time.monotonic() - t, 1),
                steps=config.STEPS,
                clip_prompt=wf["pos"]["inputs"]["text"],
            )
            job.meta["models"]["clips"] = models_in(wf)
            job.save_meta()
    c.free()


# ---------- 合成 ----------
def slot_windows(job: Job, bounds: list[int]) -> list[tuple[int, int, int]]:
    """每個時段取自哪支鏡頭的哪一段：(鏡頭 id, 起始幀, 幀數)。mux 與視覺 QC 共用，兩邊才不會對不上。"""
    slots = job.plan.layout or [s.id for s in job.plan.shots]
    if len(slots) != len(bounds) - 1:
        raise RuntimeError(f"時段數 {len(slots)} 與拍點切出的鏡數 {len(bounds) - 1} 不一致")
    seen, durations, out = {}, {}, []
    for i, sid in enumerate(slots):
        frames = bounds[i + 1] - bounds[i]
        have = durations[sid] = durations.get(sid) or float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                 job.p("clips", f"{sid:02d}.mp4")],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )  # fmt: skip
        if have * FPS < frames - 1:
            raise RuntimeError(f"clip {sid} 太短：{have:.2f}s < 切點需要 {frames / FPS:.2f}s")
        occ = seen[sid] = seen.get(sid, -1) + 1
        out.append(
            (sid, min(REUSE_OFFSETS[occ % len(REUSE_OFFSETS)], max(0, int(have * FPS) - frames)), frames)
        )
    return out


def fact_slots(n_slots: int, n_facts: int) -> list[tuple[int, int]]:
    """把時段平分給各則知識（累積四捨五入，最後一則剛好到結尾）：回傳每則的 [起始時段, 結束時段)。"""
    edges = [round(i * n_slots / n_facts) for i in range(n_facts + 1)]
    return list(pairwise(edges))


def caption_filters(job: Job, bounds: list[int]) -> str:
    """小知識的英文字幕：每則在自己的時段內疊在畫面上。用 ffmpeg drawtext 畫（不是 AI 生成，文字不會亂碼）。"""
    if not job.plan.facts:
        return ""
    font = Path(config.FONT)
    if not font.exists():
        raise RuntimeError(f"找不到字幕字型 {font}（用 VMK_FONT 指定另一個 TTF）")
    job.p("captions").mkdir(exist_ok=True)
    out = []
    for i, (fact, (a, b)) in enumerate(
        zip(job.plan.facts, fact_slots(len(bounds) - 1, len(job.plan.facts))), 1
    ):
        text = job.p("captions", f"{i:02d}.txt")  # 用檔案傳文字，不必處理 drawtext 的逸出規則
        text.write_text("\n".join(textwrap.wrap(fact.text, CAPTION_WIDTH)), encoding="utf-8")
        out.append(
            f"drawtext=fontfile='{font}':textfile='{text}':expansion=none:fontsize=54:fontcolor=white:"
            f"line_spacing=14:box=1:boxcolor=black@0.6:boxborderw=26:x=(w-text_w)/2:y=h*0.62:"
            f"enable='gte(t,{bounds[a] / FPS:.3f})*lt(t,{bounds[b] / FPS:.3f})'"  # 相鄰兩則交界那一幀只顯示後一則
        )
    return "," + ",".join(out)


def mux(job: Job):
    if job.p("final.mp4").exists() and job.p("thumb.jpg").exists():
        return
    beats = json.loads(job.p("beats.json").read_text())
    fps = FPS
    bounds = audio.frame_bounds(beats["cuts"], fps)
    windows = slot_windows(job, bounds)
    n = len(windows)
    total = bounds[-1] / fps
    inputs, chains = [], []
    for i, (sid, start, frames) in enumerate(windows):
        inputs += ["-i", job.p("clips", f"{sid:02d}.mp4")]
        # 硬切在拍點上；非 9:16 的素材（5B 的 704x1280）放大裁切，不留黑邊
        chains.append(
            f"[{i}:v]fps={fps},trim=start_frame={start}:end_frame={start + frames},setpts=PTS-STARTPTS,"
            f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{i}]"
        )
    chains.append(
        "".join(f"[v{i}]" for i in range(n))
        + f"concat=n={n}:v=1:a=0{caption_filters(job, bounds)},format=yuv420p[v]"
    )
    with job.timed("mux"):
        # 音樂從第一個重拍起算、裁到 total、最後 1 秒淡出；兩段式 loudnorm 才準
        _ff(
            "-ss",
            beats["t0"],
            "-t",
            total,
            "-i",
            job.p("music.wav"),
            "-af",
            f"afade=t=out:st={total - 1:.3f}:d=1",
            job.p("audio_trim.wav"),
        )
        m = _measure(job.p("audio_trim.wav"))
        ln = (
            f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
        )
        job.p("status.json").unlink(missing_ok=True)  # 重合成後必須重跑 QC
        _ff(
            *inputs,
            "-i",
            job.p("audio_trim.wav"),
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[v]",
            "-map",
            f"{n}:a",
            "-af",
            ln,
            "-c:v",
            "libx264",
            "-crf",
            18,
            "-preset",
            "medium",
            "-r",
            fps,
            "-c:a",
            "aac",
            "-ar",
            48000,
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-t",
            f"{total:.4f}",
            job.p("final.mp4"),
        )
        _ff("-ss", 1, "-i", job.p("final.mp4"), "-frames:v", 1, "-q:v", 2, job.p("thumb.jpg"))
    job.p("audio_trim.wav").unlink(missing_ok=True)


# ---------- QC ----------
def qc(job: Job):
    if job.p("status.json").exists():
        previous = json.loads(job.p("status.json").read_text())
        if previous["status"] != "failed" or previous.get("visual_qc", {}).get("status") != "error":
            return
    with job.timed("qc"):
        _qc(job)


def _qc(job: Job):
    final = job.p("final.mp4")
    probe = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,width,height:format=duration",
                "-of",
                "json",
                final,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    v = [s for s in probe["streams"] if s["codec_type"] == "video"]
    dur = float(probe["format"]["duration"])
    lufs = float(_measure(final)["input_i"])
    black = [
        float(x)
        for x in re.findall(
            r"black_duration:([\d.]+)",
            _ff("-i", final, "-an", "-vf", "blackdetect=d=0.2:pix_th=0.10", "-f", "null", "-"),
        )
    ]
    lo, hi = FORMATS[job.fmt]["min_total"], FORMATS[job.fmt]["max_total"]
    checks = {
        "resolution_1080x1920": bool(v) and (v[0]["width"], v[0]["height"]) == (1080, 1920),
        f"duration_{lo}_{hi}": lo <= dur <= hi,
        "has_audio": any(s["codec_type"] == "audio" for s in probe["streams"]),
        "loudness_-14±1.5": abs(lufs + 14) <= 1.5,
        "no_long_black": max(black, default=0) < 0.5,
    }
    if all(checks.values()):
        visual = visual_qc.check(job)
    else:  # 基本檢查已失敗，結果注定是 failed；不為此佔用共用 GPU 與統一記憶體
        visual = {"status": "not_checked", "model": visual_qc.MODEL, "reason": "基本檢查未通過，略過視覺檢查"}
    checks["visual_qc"] = visual["status"] == "passed"
    if visual.get("checked_frames"):  # 確實有畫面送進模型才記錄；meta.json 是授權證據，要連同 digest
        job.meta.setdefault("models", {})["visual_qc"] = [
            f"{visual_qc.MODEL}@{visual.get('model_digest', '?')}"
        ]
    status = {
        "status": "pending_review" if all(checks.values()) else "failed",
        "checks": checks,
        "visual_qc": visual,
        "details": {"duration": round(dur, 3), "lufs": lufs, "black_segments": black},
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    job.p("status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    (log.info if status["status"] == "pending_review" else log.error)("QC：%s %s", status["status"], checks)
    if visual["status"] == "error":
        raise RuntimeError(f"視覺 QC 未完成：{visual.get('error', '未知錯誤')}")


FNS = {
    "music": music,
    "keyframes": keyframes,
    "clips": clips,
    "mux": mux,
    "qc": qc,
}


def run(
    topic: str,
    upto: str = "qc",
    dry_run: bool = False,
    resume: bool = False,
    images: list[Path] | None = None,
    fmt: str = "short",
    kind: str = "kids",
) -> Job:
    if (
        resume and kind == "trivia"
    ):  # 小知識的 plan.topic 是維基正式標題；輸入 "sea otters" 也要找得到 "Sea otter"
        topic = wiki.fetch(topic).title
    job = (find_job(topic) if resume else None) or new_job(topic, images, fmt, kind)
    fh = logging.FileHandler(job.p("run.log"), encoding="utf-8")
    log.addHandler(fh)
    try:
        if not dry_run:
            for st in STAGES[1 : STAGES.index(upto) + 1]:
                FNS[st](job)
    finally:
        log.removeHandler(fh)
    return job


def bench() -> str:
    """彙整 work/*/meta.json（只算跑完 QC 的作業）：各階段平均耗時。"""
    ts = [
        json.loads(m.read_text())["timings"]
        for m in config.WORK.glob("*/meta.json")
        if (m.parent / "status.json").exists()  # 半途中止的不算
    ]
    lines = ["| 支數 | " + " | ".join(STAGES) + " |", "|" + "---|" * (len(STAGES) + 1)]
    if ts:
        lines.append(
            f"| {len(ts)} | "
            + " | ".join(f"{sum(t.get(s, 0) for t in ts) / len(ts):.0f}s" for s in STAGES)
            + " |"
        )
    return "\n".join(lines)
