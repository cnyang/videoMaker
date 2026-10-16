"""週五合併：把本週一到週四夜間產生的 short、long、xl 各自串成一支（純 ffmpeg 串接，不用 GPU）。

合併後的作業和一般作業一樣停在 pending_review，由人工審核；不碰 YouTube。
"""

import json
import logging
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import config, pipeline, visual_qc
from .plan import FORMATS, Plan, Youtube

log = logging.getLogger("videomaker")
MERGEABLE = ("pending_review", "approved")  # 人工標記不通過、QC 失敗的不合併
WINDOW_DAYS = 6  # 週五往前 6 天 = 上週六起，只涵蓋本週一到週四的夜間作業（上週四已是 8 天前）
TOLERANCE = 1.0  # 串接後長度和各支加總最多差 1 秒，超過就標記 failed（-c copy 遇到編碼參數不一致才會發生）


def _duration(path: Path) -> float:
    return float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def sources(fmt: str, now: datetime) -> list[Path]:
    """本週夜間排程產生、成片還在 GX10 上、且沒被人工否決的作業（依產生時間排序）。"""
    hist = config.WORK / "history.jsonl"
    if not hist.exists():
        return []
    since, out = now - timedelta(days=WINDOW_DAYS), []
    for e in (json.loads(x) for x in hist.read_text(encoding="utf-8").splitlines() if x):
        if e["format"] != fmt or "job" not in e or datetime.fromisoformat(e["date"]) < since:
            continue  # 略過其他片型、skipped／failed（沒有 job）、上週以前的
        d = config.WORK / e["job"]
        st = d / "status.json"  # 以 status.json 為準：人工審核會改它，history 裡的是當時的狀態
        if (d / "final.mp4").exists() and st.exists() and json.loads(st.read_text())["status"] in MERGEABLE:
            out.append(d)
    return out


def _plan(dirs: list[Path], fmt: str) -> Plan:
    """沿用第一支的 plan（合法的 Plan 才能走原本的上傳流程），只換主題與 YouTube 文案。"""
    plans = [Plan.model_validate_json((d / "plan.json").read_text()) for d in dirs]
    topics = [p.topic for p in plans]
    plan = plans[0]
    plan.topic, plan.slug = f"本週精選 {fmt}（{len(dirs)} 支）", f"weekly-{fmt}"
    plan.youtube = Youtube(
        title=f"{'、'.join(topics)} 兒童音樂精選｜Kids Music Mix"[:100],
        description=(
            f"本週 {len(dirs)} 支兒童音樂動畫精選：{'、'.join(topics)}。\n適合 3 至 8 歲的小朋友欣賞。\n\n"
            f"A weekly mix of {len(dirs)} gentle kids music videos.\nPerfect for relaxing and playing along.\n\n"
            "#KidsMusic #NurseryRhymes #Compilation"
        ),
        tags=list(dict.fromkeys(t for p in plans for t in p.youtube.tags)),
    )
    return plan


def merge(fmt: str, now: datetime | None = None) -> Path | None:
    now = now or datetime.now().astimezone()
    dirs = sources(fmt, now)
    if len(dirs) < 2:  # 只有 1 支串起來就是原片，沒有意義
        log.warning("[%s] 本週可合併的只有 %d 支，略過", fmt, len(dirs))
        return None
    out = config.WORK / f"{now:%Y-%m-%d}_weekly-{fmt}"
    if out.exists():
        log.info("[%s] %s 已存在，略過", fmt, out.name)
        return out
    out.mkdir()
    try:
        # 各支都由同一條 mux 產生（h264 + aac 48k、同解析度與幀率），所以可以直接串接不重新編碼
        (out / "concat.txt").write_text("".join(f"file '{d / 'final.mp4'}'\n" for d in dirs))
        pipeline._ff("-f", "concat", "-safe", 0, "-i", out / "concat.txt", "-c", "copy",
                     "-movflags", "+faststart", out / "final.mp4")  # fmt: skip
        pipeline._ff("-ss", 1, "-i", out / "final.mp4", "-frames:v", 1, "-q:v", 2, out / "thumb.jpg")
        (out / "plan.json").write_text(_plan(dirs, fmt).model_dump_json(indent=2) + "\n")
        (out / "concat.txt").unlink()
        names, dur = [d.name for d in dirs], _duration(out / "final.mp4")
        ok = abs(dur - sum(_duration(d / "final.mp4") for d in dirs)) <= TOLERANCE
        visual = visual_qc.combine(dirs)
        now_s = datetime.now().astimezone().isoformat(timespec="seconds")
        meta = {
            "topic": f"本週精選 {fmt}",
            "format": f"weekly-{fmt}",
            "created_at": now_s,
            "timings": {},
            "sources": names,
        }
        status = {
            "status": "pending_review" if ok and visual["status"] == "passed" else "failed",
            "checks": {"duration_matches_sources": ok, "visual_qc": visual["status"] == "passed"},
            "visual_qc": visual,
            "details": {"duration": round(dur, 3), "sources": names},
            "updated": now_s,
        }
        for name, data in (("meta.json", meta), ("status.json", status)):
            (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(
            out
        )  # 不留半成品，否則下次會被當成「已存在」而略過，jobs.inventory 也會讀不到 meta.json
        raise
    (log.info if ok else log.error)(
        "[%s] 合併 %d 支 → %s（%.0f 秒，%s）", fmt, len(dirs), out.name, dur, status["status"]
    )
    return out


def merge_all() -> list[Path]:
    return [p for fmt in FORMATS if (p := merge(fmt))]
