"""對白類（兒童英文對話、動物短劇、有對白的動畫）的階段實作。

階段名稱與 pipeline.STAGES 相同，`pipeline.run` 依 plan.kind 改用這裡的 FNS：
  music     沒有（音訊是 H3 隨影片一起產生的，沒有拍點，鏡頭長度由腳本決定）
  keyframes 只有 h3_fl2va 需要（沿用 pipeline.keyframes：FLUX.1 schnell，含黑白邊重生）
  clips     由 renderer 一鏡一鏡產生帶音訊的短片（renderers.py）
  mux       把各鏡頭（影片＋對白音訊）依序串起來、燒字幕、整段 loudnorm
  qc        沿用 pipeline.qc，另外加上 checks() 的對白檢查
每階段輸出已存在就跳過，維持可續跑。
"""

import json
import logging
import re
import subprocess
import textwrap
from itertools import accumulate, pairwise
from pathlib import Path

from . import config, content, pipeline, renderers
from .comfy import Comfy

log = logging.getLogger("videomaker")
FPS = pipeline.FPS
LEAD, TAIL = 0.3, 0.2  # 字幕在每鏡開頭晚 0.3 秒出現、結尾提前 0.2 秒消失（H3 的對白通常不是第 0 幀就開口）
SILENCE_DB, SILENCE_MAX = -50, 3.0  # 對白類不該有超過 3 秒的完全無聲區間（-50dB 以下）


def renderer(job):
    # renderer 在建立作業時寫進 meta；舊作業沒有時退回現行設定
    return renderers.get(job.meta.get("renderer") or content.renderer_for(job.plan.kind))


def script_shots(job) -> list[content.ScriptShot]:
    return [content.ScriptShot.model_validate(s) for s in job.plan.script["shots"]]


def music(job):
    return  # 對白類沒有音樂階段


def keyframes(job):
    if renderer(job).keyframes:
        pipeline.keyframes(job)


def clips(job):
    r = renderer(job)
    todo = [s for s in job.plan.shots if not job.p("clips", f"{s.id:02d}.mp4").exists()]
    if not todo:
        return
    renderers.require(r.name)
    if missing := r.missing_refs(job):  # 缺參考圖就在佔用 GPU 之前中止
        raise RuntimeError(f"缺少角色參考圖 {missing}，請先執行 `videomaker characters build`")
    c = Comfy()
    c.prepare(extra_gb=config.H3_EXTRA_GB)  # 先 /free 再檢查記憶體；H3 門檻 = 基準 + VMK_H3_EXTRA_GB
    with job.timed("clips"):
        for s in todo:
            info = r.render(job, c, s)
            job.meta["shots"].setdefault(str(s.id), {}).update(info)
            job.save_meta()
    c.free()


def _probe(path: Path) -> dict[str, float]:
    """各串流的長度（秒）。H3 的影片與音訊長度可能差幾個 frame，串接前要各自裁到同一長度。"""
    out = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", path],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    return {s["codec_type"]: float(s["duration"]) for s in out["streams"] if s.get("duration")}


def timeline(job, durs: list[float]) -> list[dict]:
    starts = [0.0, *accumulate(durs)][:-1]
    return [
        {"shot": s.id, "start": round(t, 3), "duration": round(d, 3)}
        for s, t, d in zip(job.plan.shots, starts, durs)
    ]


def subtitles(job, durs: list[float]) -> list[dict]:
    """每句對白一段字幕。沒有 ASR，所以依詞數把一鏡的可說話時段（扣掉頭尾）按比例分給各句：
    時間是估計的，文字就是腳本對白本身（QC 驗證兩者一致）。"""
    out = []
    for shot, tl in zip(script_shots(job), timeline(job, durs)):
        a, b = tl["start"] + LEAD, tl["start"] + tl["duration"] - TAIL
        words = [len(content.WORD.findall(line.text)) or 1 for line in shot.dialogue]
        pos = a
        for line, w in zip(shot.dialogue, words):
            end = pos + (b - a) * w / sum(words)
            out.append(
                {"shot": shot.shot_id, "start": round(pos, 3), "end": round(end, 3), "text": line.text}
            )
            pos = end
    return out


def mux(job):
    if job.p("final.mp4").exists() and job.p("thumb.jpg").exists():
        return
    font = Path(config.FONT)
    if not font.exists():
        raise RuntimeError(f"找不到字幕字型 {font}（用 VMK_FONT 指定另一個 TTF）")
    probes = [_probe(job.p("clips", f"{s.id:02d}.mp4")) for s in job.plan.shots]
    if bad := [s.id for s, p in zip(job.plan.shots, probes) if "audio" not in p or "video" not in p]:
        raise RuntimeError(f"鏡頭 {bad} 缺少影片或音訊串流（對白類每鏡都必須帶對白音訊）")
    durs = [min(p["video"], p["audio"]) for p in probes]
    total, n = sum(durs), len(durs)
    subs = subtitles(job, durs)
    job.p("timeline.json").write_text(json.dumps(timeline(job, durs), indent=2) + "\n")
    job.p("subtitles.json").write_text(json.dumps(subs, ensure_ascii=False, indent=2) + "\n")
    job.p("captions").mkdir(exist_ok=True)
    caps = []
    for i, sub in enumerate(subs, 1):
        text = job.p("captions", f"{i:02d}.txt")  # 用檔案傳文字，不必處理 drawtext 的逸出規則
        text.write_text("\n".join(textwrap.wrap(sub["text"], pipeline.CAPTION_WIDTH)), encoding="utf-8")
        caps.append(pipeline.drawtext_filter(font, text, sub["start"], sub["end"]))
    inputs = [x for s in job.plan.shots for x in ("-i", job.p("clips", f"{s.id:02d}.mp4"))]
    vchains = [
        f"[{i}:v]trim=end={d:.4f},setpts=PTS-STARTPTS,fps={FPS},"
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{i}]"
        for i, d in enumerate(durs)
    ]
    achains = [
        f"[{i}:a]atrim=end={d:.4f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a{i}]"
        for i, d in enumerate(durs)
    ]
    with job.timed("mux"):
        job.p("status.json").unlink(missing_ok=True)  # 一旦開始重合成，舊核准就不能沿用
        job.p("thumb.jpg").unlink(missing_ok=True)
        # 先把各鏡對白音訊串成一條量 loudness，再兩段式 loudnorm（與兒童音樂同一條 -14 LUFS 標準）
        pipeline._ff(
            *inputs,
            "-filter_complex",
            ";".join([*achains, "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"]),
            "-map", "[a]", job.p("audio_cat.wav"),
        )  # fmt: skip
        m = pipeline._measure(job.p("audio_cat.wav"))
        # 完全無聲（H3 沒有產生對白）量到 -inf，loudnorm 會直接失敗；改成不處理，讓 QC 用「無聲」「響度」明確判不通過
        ln = "anull" if float(m["input_i"]) == float("-inf") else pipeline.loudnorm_filter(m)
        vconcat = (
            "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0" + "".join("," + c for c in caps)
        )
        pipeline._ff(
            *inputs,
            "-i", job.p("audio_cat.wav"),
            "-filter_complex", ";".join([*vchains, f"{vconcat},format=yuv420p[v]"]),
            "-map", "[v]", "-map", f"{n}:a", "-af", ln,
            "-c:v", "libx264", "-crf", 18, "-preset", "medium", "-r", FPS,
            "-c:a", "aac", "-ar", 48000, "-b:a", "192k",
            "-movflags", "+faststart", "-t", f"{total:.4f}",
            job.p("final.tmp.mp4"),
        )  # fmt: skip
        pipeline._ff(
            "-ss", 1, "-i", job.p("final.tmp.mp4"), "-frames:v", 1, "-q:v", 2, job.p("thumb.tmp.jpg")
        )
        job.p("final.tmp.mp4").replace(job.p("final.mp4"))
        job.p("thumb.tmp.jpg").replace(job.p("thumb.jpg"))  # 兩份都成功，才會滿足「已完成就跳過」
    job.p("audio_cat.wav").unlink(missing_ok=True)


def _silent_gaps(job, err: str, dur: float) -> list[tuple[float, float]]:
    """silencedetect 報出的無聲區間（>= SILENCE_MAX 秒），扣掉完全落在「沒有對白的鏡頭」內的：
    反應鏡頭（如狗狗盯著時鐘）本來就可以沒有話。尾端的無聲沒有 silence_end，視為到片尾。"""
    starts = [max(0.0, float(x)) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    ends += [dur] * (len(starts) - len(ends))
    quiet = []  # 沒有對白的鏡頭在成片中的時間窗
    if job.p("timeline.json").exists():
        timeline = json.loads(job.p("timeline.json").read_text())
        quiet = [
            (t["start"] - 0.05, t["start"] + t["duration"] + 0.05)
            for t, shot in zip(timeline, script_shots(job))
            if not shot.dialogue
        ]
    return [(a, b) for a, b in zip(starts, ends) if not any(lo <= a and b <= hi for lo, hi in quiet)]


def checks(job, final: Path, dur: float) -> dict[str, bool]:
    """對白類 QC（純 metadata／訊號，不跑 ASR）：沒有長時間無聲、字幕時間軸在片長內、字幕文字與腳本對白一致。"""
    err = pipeline._ff(
        "-i", final, "-vn", "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MAX}", "-f", "null", "-"
    )
    silences = _silent_gaps(job, err, dur)
    path = job.p("subtitles.json")
    subs = json.loads(path.read_text()) if path.exists() else None
    expected = [content.norm(line.text) for shot in script_shots(job) for line in shot.dialogue]
    in_range = bool(subs) and all(0 <= a < b <= dur + 0.05 for a, b in ((x["start"], x["end"]) for x in subs))
    ordered = bool(subs) and all(a["end"] <= b["start"] + 1e-6 for a, b in pairwise(subs))
    return {
        "no_long_silence": not silences,
        "subtitles_within_duration": in_range and ordered,
        "subtitles_match_dialogue": subs is not None and [content.norm(x["text"]) for x in subs] == expected,
    }


FNS = {"music": music, "keyframes": keyframes, "clips": clips, "mux": mux, "qc": pipeline.qc}
