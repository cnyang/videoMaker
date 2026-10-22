"""合併：把近期夜間產生的同內容、同片型、同解析度的成片串成一支（純 ffmpeg `-c copy`，不用 GPU）。

每晚生成結束後由 `nightly.merge_after` 呼叫（也可手動 `videomaker weekly`）。合併身分（merge_id）由
內容種類＋片型＋來源作業名稱決定：來源沒變就不重做，加入新來源才產生新的合併。
合併後的作業和一般作業一樣停在 pending_review，由人工審核；不碰 YouTube。
"""

import hashlib
import json
import logging
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from . import config, pipeline, visual_qc
from .plan import DIALOGUE_KINDS, KINDS, Plan, Youtube

log = logging.getLogger("videomaker")
MERGEABLE = ("pending_review", "approved")  # 人工標記不通過、QC 失敗的不合併
# 兒童音樂與小知識每週只在固定一晚各產 1 支（每種片型），要湊到 2 支至少要跨兩週，所以往前看 4 週、最多取最新 4 支
WINDOW_DAYS = 28
DIALOGUE_WINDOW_DAYS = 6  # 對白類每晚產 2 支：只合併同一週（同一晚）的，不與上週的重疊
MAX_SOURCES = 4
# 對白類合輯的 YouTube 文案：(中文名, 英文名, 中文說明, 英文說明, hashtags)
COMPILATION = {
    "kids_english_dialogue": ("兒童英文對話精選", "Kids English Dialogues", "適合小朋友跟著說的簡單英文對話", "Simple English conversations kids can say along with.", "#KidsEnglish #LearnEnglish #Compilation"),
    "animal_sitcom": ("動物短劇精選", "Animal Sitcom Mix", "毛孩日常的搞笑小短劇", "Funny everyday sketches starring our animal friends.", "#AnimalSitcom #FunnyAnimals #Compilation"),
    "dialogue_animation": ("動畫小故事精選", "Animated Stories", "有對白的動畫小故事", "Short animated stories with dialogue.", "#AnimatedStory #Cartoon #Compilation"),
}  # fmt: skip
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


def _size(path: Path) -> tuple[int, int] | None:
    try:
        w, h = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, check=True,
        ).stdout.strip().split("x")  # fmt: skip
        return int(w), int(h)
    except (subprocess.CalledProcessError, ValueError):
        return None


def sources(fmt: str, now: datetime, kind: str = "kids") -> list[Path]:
    """近 WINDOW_DAYS 天（對白類 DIALOGUE_WINDOW_DAYS 天）夜間排程產生、成片還在 GX10 上、且沒被人工否決的作業（依產生時間排序，最多最新 MAX_SOURCES 支）。
    各內容種類分開算；解析度和多數不同（或讀不出來）的排除，-c copy 不能混不同規格。"""
    hist = config.WORK / "history.jsonl"
    if not hist.exists():
        return []
    since, out = now - timedelta(days=DIALOGUE_WINDOW_DAYS if kind in DIALOGUE_KINDS else WINDOW_DAYS), []
    for e in (json.loads(x) for x in hist.read_text(encoding="utf-8").splitlines() if x):
        if e.get("type") == "merge" or e["format"] != fmt or e.get("kind", "kids") != kind or "job" not in e:
            continue  # 略過合併紀錄、其他片型與內容種類（舊紀錄沒有 kind，都是兒童音樂）、skipped／failed（沒有 job）
        if datetime.fromisoformat(e["date"]) < since:
            continue  # 上週以前的
        d = config.WORK / e["job"]
        st = d / "status.json"  # 以 status.json 為準：人工審核會改它，history 裡的是當時的狀態
        if (d / "final.mp4").exists() and st.exists() and json.loads(st.read_text())["status"] in MERGEABLE:
            out.append(d)
    dims = {d: _size(d / "final.mp4") for d in out}
    if dims:
        common = Counter(dims.values()).most_common(1)[0][0]
        for d in [d for d, v in dims.items() if v != common or v is None]:
            log.warning("[%s] %s 的解析度 %s 與其他來源（%s）不同，不合併", fmt, d.name, dims[d], common)
            out.remove(d)
    return out[-MAX_SOURCES:]


def _plan(dirs: list[Path], fmt: str, kind: str = "kids") -> Plan:
    """沿用第一支的 plan（合法的 Plan 才能走原本的上傳流程），只換主題與 YouTube 文案。"""
    plans = [Plan.model_validate_json((d / "plan.json").read_text()) for d in dirs]
    topics = [p.topic for p in plans]
    plan = plans[0]
    names = "、".join(topics)
    if kind == "trivia":
        plan.topic, plan.slug = f"本週小知識 {fmt}（{len(dirs)} 支）", f"weekly-trivia-{fmt}"
        plan.facts, plan.sources = (
            [],
            [s for p in plans for s in p.sources],
        )  # 字幕早已燒進各支成片；來源要全部列出
        credits = "\n".join(f"- {s.title}: {s.url} ({s.license})" for s in plan.sources)
        plan.youtube = Youtube(
            title=f"{names} 兒童小知識精選｜Kids Fun Facts Mix"[:100],
            description=(
                f"本週 {len(dirs)} 支兒童小知識影片精選：{names}。\n每則知識都附有維基百科來源。\n\n"
                f"A weekly mix of {len(dirs)} fun facts videos for kids.\nEvery fact comes from Wikipedia.\n\n"
                f"Sources (Wikipedia, CC BY-SA 4.0):\n{credits}\n\n#KidsFacts #FunFacts #Compilation"
            ),
            tags=list(dict.fromkeys(t for p in plans for t in p.youtube.tags)),
        )
        return plan
    if kind in COMPILATION:
        zh, en, zh_desc, en_desc, tags = COMPILATION[kind]
        plan.topic, plan.slug = f"{zh} {fmt}（{len(dirs)} 支）", f"weekly-{kind.replace('_', '-')}-{fmt}"
        plan.script = {}  # 各支的腳本與字幕早已燒進成片；合輯沒有單一腳本
        plan.youtube = Youtube(
            title=f"{names} {zh}｜{en}"[:100],
            description=f"{len(dirs)} 支{zh_desc}：{names}。\n\nA mix of {len(dirs)} videos. {en_desc}\n\n{tags}",
            tags=list(dict.fromkeys(t for p in plans for t in p.youtube.tags)),
        )
        return plan
    plan.topic, plan.slug = f"本週精選 {fmt}（{len(dirs)} 支）", f"weekly-{fmt}"
    plan.youtube = Youtube(
        title=f"{names} 兒童音樂精選｜Kids Music Mix"[:100],
        description=(
            f"本週 {len(dirs)} 支兒童音樂動畫精選：{names}。\n適合 3 至 8 歲的小朋友欣賞。\n\n"
            f"A weekly mix of {len(dirs)} gentle kids music videos.\nPerfect for relaxing and playing along.\n\n"
            "#KidsMusic #NurseryRhymes #Compilation"
        ),
        tags=list(dict.fromkeys(t for p in plans for t in p.youtube.tags)),
    )
    return plan


def label_of(kind: str, fmt: str) -> str:
    """作業名與日誌用：weekly-short（兒童音樂）／weekly-trivia-short／weekly-kids-english-dialogue-short。"""
    return fmt if kind == "kids" else f"{kind.replace('_', '-')}-{fmt}"


def merge_id(kind: str, fmt: str, names: list[str]) -> str:
    """合併身分：內容種類＋片型＋來源作業名稱（排序後）。來源沒變 → 同一個 id → 不重做。"""
    return hashlib.sha256(f"{kind}|{fmt}|{','.join(sorted(names))}".encode()).hexdigest()[:12]


def _existing(kind: str, fmt: str, mid: str) -> Path | None:
    """work/ 裡已有同身分的合併作業（含舊版沒有 merge_id 的，依 sources 重算）。不論目前審核狀態，人工否決的也不重做。"""
    for meta_path in sorted(config.WORK.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):  # 別的行程正在寫、或當機留下的半份檔：略過這一支，不讓整個合併失敗
            continue
        if (
            "sources" not in meta
            or meta.get("kind", "kids") != kind
            or not meta.get("format", "").endswith(fmt)
        ):
            continue
        if (meta.get("merge_id") or merge_id(kind, fmt, meta["sources"])) == mid:
            return meta_path.parent
    return None


def _merged_before(mid: str) -> bool:
    """history 記過這個合併已做成（成片可能已上傳後被刪掉）。失敗的不算，下次可以重試。"""
    hist = config.WORK / "history.jsonl"
    if not hist.exists():
        return False
    entries = (json.loads(x) for x in hist.read_text(encoding="utf-8").splitlines() if x)
    return any(
        e.get("type") == "merge" and e.get("merge_id") == mid and e.get("status") == "merged" for e in entries
    )


def _merge(fmt: str, now: datetime | None, kind: str) -> tuple[Path | None, dict]:
    """回傳 (合併作業資料夾或 None, history 紀錄)。ffmpeg 等錯誤照原樣丟出，由 merge_kind 攔下記為 failed。"""
    now = now or datetime.now().astimezone()
    dirs = sources(fmt, now, kind)
    label = label_of(kind, fmt)
    rec = {"type": "merge", "date": now.isoformat(timespec="seconds"), "topic": "", "format": fmt, "kind": kind,
           "content_type": kind}  # fmt: skip
    if len(dirs) < 2:  # 只有 1 支串起來就是原片，沒有意義
        log.warning("[%s] 可合併的只有 %d 支，略過", label, len(dirs))
        return None, rec | {"status": "skipped", "reason": f"可合併的只有 {len(dirs)} 支（至少 2 支）"}
    names = [d.name for d in dirs]
    mid = merge_id(kind, fmt, names)
    rec |= {"merge_id": mid, "sources": names}
    if old := _existing(kind, fmt, mid):
        log.info("[%s] 來源沒有變動，%s 已存在，略過", label, old.name)
        return old, rec | {"status": "skipped", "reason": "來源沒有變動，已合併過", "job": old.name}
    if _merged_before(mid):
        log.info("[%s] 來源沒有變動，history 記錄已合併過，略過", label)
        return None, rec | {"status": "skipped", "reason": "來源沒有變動，history 記錄已合併過"}
    out = config.WORK / f"{now:%Y-%m-%d}_weekly-{label}"
    for i in range(2, 100):  # 同一天來源有變動而再合併一次 → 另開資料夾
        if not out.exists():
            break
        out = config.WORK / f"{now:%Y-%m-%d}_weekly-{label}-{i}"
    out.mkdir()
    try:
        # 各支都由同一條 mux 產生（h264 + aac 48k、同解析度與幀率），所以可以直接串接不重新編碼
        (out / "concat.txt").write_text("".join(f"file '{d / 'final.mp4'}'\n" for d in dirs))
        pipeline._ff("-f", "concat", "-safe", 0, "-i", out / "concat.txt", "-c", "copy",
                     "-movflags", "+faststart", out / "final.mp4")  # fmt: skip
        pipeline._ff("-ss", 1, "-i", out / "final.mp4", "-frames:v", 1, "-q:v", 2, out / "thumb.jpg")
        (out / "plan.json").write_text(_plan(dirs, fmt, kind).model_dump_json(indent=2) + "\n")
        (out / "concat.txt").unlink()
        dur = _duration(out / "final.mp4")
        ok = abs(dur - sum(_duration(d / "final.mp4") for d in dirs)) <= TOLERANCE
        visual = visual_qc.combine(dirs)
        now_s = datetime.now().astimezone().isoformat(timespec="seconds")
        meta = {
            "topic": f"本週精選 {fmt}" if kind == "kids" else f"本週小知識 {fmt}",
            "format": f"weekly-{label}",
            "kind": kind,
            "created_at": now_s,
            "timings": {},
            "sources": names,
            "merge_id": mid,
        }
        if kind in COMPILATION:
            meta["topic"] = f"{COMPILATION[kind][0]} {fmt}"
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
        "[%s] 合併 %d 支 → %s（%.0f 秒，%s）", label, len(dirs), out.name, dur, status["status"]
    )
    return out, rec | {"status": "merged", "job": out.name, "output": str(out / "final.mp4"),
                       "duration": round(dur, 3), "qc": status["status"]}  # fmt: skip


def merge(fmt: str, now: datetime | None = None, kind: str = "kids") -> Path | None:
    return _merge(fmt, now, kind)[0]


def merge_kind(kind: str, now: datetime | None = None) -> list[dict]:
    """某一種內容的所有片型各合併一次，回傳要寫進 history 的紀錄（merged／skipped／failed）。
    單一片型失敗（ffmpeg 等）只記為 failed，不影響其他片型，也不往外丟。"""
    out = []
    for fmt in KINDS[kind]:
        try:
            out.append(_merge(fmt, now, kind)[1])
        except Exception as e:  # noqa: BLE001 合併是附加工作：失敗只記錄
            log.error("[%s] 合併失敗：%r", label_of(kind, fmt), e)
            out.append({"type": "merge", "date": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
                        "topic": "", "format": fmt, "kind": kind, "content_type": kind,
                        "status": "failed", "error": repr(e)})  # fmt: skip
    return out


def merge_all() -> list[Path]:
    return [p for kind, formats in KINDS.items() for fmt in formats if (p := merge(fmt, kind=kind))]
