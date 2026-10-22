"""作業盤點與遠端操作（TUI 與 CLI 共用，不含畫面）。

GX10 端：inventory() 盤點 work/ 底下的作業，輸出 JSON（`videomaker jobs-json`）。
Mac 端：用 ssh 呼叫 GX10，再合併本機已下載的狀態；下載、核准、上傳、刪除都經過這裡。

刪除是破壞性動作：作業名稱一律先過 valid()，只接受 `<日期>_<slug>`，擋掉路徑穿越與 shell 特殊字元。
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, visual_qc

JOB_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9]+(-[a-z0-9]+)*$")
GX10 = os.getenv("VMK_GX10", "gx10")  # ssh 別名
GX10_PROJECT = "~/code/videoMaker"
LOCAL = Path(os.getenv("VMK_LOCAL_DIR", "~/Downloads/videoMaker")).expanduser()  # 下載到 Mac 的位置
ACTIVE_SECONDS = 600  # 沒有 status.json 且 10 分鐘內有檔案更新 → 視為還在跑，不允許刪除或審核

STATE_ZH = {
    "pending_review": "待審核",
    "approved": "已核准",
    "rejected": "不通過",
    "failed": "QC 失敗",
}
KIND_ZH = {
    "kids": "兒童音樂",
    "trivia": "小知識",
    "kids_english_dialogue": "兒童英文對話",
    "animal_sitcom": "動物短劇",
    "dialogue_animation": "有對白的動畫",
}
VISUAL_STATE_ZH = {
    "passed": "未見疑慮",
    "flagged": "有疑慮",
    "error": "檢查失敗",
    "running": "檢查中",
    "not_checked": "未檢查",
}
VISUAL_LABELS = {
    "child_safety": "兒童內容疑慮",
    "visual_defect": "畫面變形",
    "unexpected_objects": "多餘人手／道具",
}


def visual_summary(directory: Path, status: dict) -> dict:
    path = directory / "visual_qc.json"
    if not path.exists():
        return visual_qc.restate(status.get("visual_qc", {}))
    summary = json.loads(path.read_text())["summary"]
    # 行程被 kill（SIGKILL、斷電、OOM）後檔案會永遠停在 running；久沒有進度就當作中斷，不能一直顯示「檢查中」
    if summary.get("status") == "running" and time.time() - path.stat().st_mtime > ACTIVE_SECONDS:
        return {**summary, "status": "error", "error": f"檢查中斷：超過 {ACTIVE_SECONDS // 60} 分鐘沒有進度"}
    return visual_qc.restate(summary)  # 舊作業也依現行門檻顯示；不動 status.json，不重設人工審核


def valid(name: str) -> bool:
    return bool(JOB_RE.fullmatch(name))


def _need_valid(name: str) -> str:
    if not valid(name):
        raise ValueError(f"不合法的作業名稱：{name!r}（只接受 <日期>_<slug>）")
    return name


# ---------- GX10 端 ----------
def inventory() -> list[dict]:
    out = []
    for d in sorted(p for p in config.WORK.iterdir() if (p / "plan.json").exists()):
        plan = json.loads((d / "plan.json").read_text())
        meta = json.loads((d / "meta.json").read_text())
        st = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else None
        visual = visual_summary(d, st or {})
        final = d / "final.mp4"
        newest = max((f.stat().st_mtime for f in d.rglob("*") if f.is_file()), default=d.stat().st_mtime)
        out.append(
            {
                "name": d.name,
                "topic": plan["topic"],
                "title": plan.get("youtube", {}).get("title", ""),
                "format": meta.get("format", "short"),
                "kind": plan.get("kind", "kids"),  # kids = 兒童音樂；trivia = 小知識；舊作業沒有這欄
                "status": st["status"] if st else None,
                "duration": (st or {}).get("details", {}).get("duration"),
                "youtube_id": meta.get("youtube_id"),
                "size": final.stat().st_size if final.exists() else 0,
                "photo": bool(meta.get("images")),
                "visual_qc": visual,
                "active": (st is None or visual.get("status") == "running")
                and time.time() - newest < ACTIVE_SECONDS,
            }
        )
    return out


# ---------- Mac 端 ----------
def _sh(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _gx(remote: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return _sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", GX10, remote], timeout)


def gx10_jobs() -> list[dict]:
    r = _gx(f"cd {GX10_PROJECT} && .venv/bin/python -m videomaker jobs-json")
    if r.returncode:
        raise RuntimeError(f"連不上 GX10 或盤點失敗：{(r.stderr or r.stdout)[-200:].strip()}")
    return json.loads(r.stdout)


def local_jobs() -> set[str]:
    return (
        {p.name for p in LOCAL.glob("*") if valid(p.name) and (p / "final.mp4").exists()}
        if LOCAL.exists()
        else set()
    )


def _local_topic(name: str) -> str:
    try:
        return json.loads((LOCAL / name / "plan.json").read_text())["topic"]
    except (OSError, ValueError, KeyError):
        return "?"


def rows() -> list[dict]:
    """GX10、Mac 兩處的作業合併成一張表（任何一處有就列出，GX10 刪掉後仍可看到只剩在 Mac 的）。"""
    gx, mac = {j["name"]: j for j in gx10_jobs()}, local_jobs()
    out = []
    for name in sorted(set(gx) | mac, reverse=True):
        base = gx.get(name) or {
            "name": name, "topic": _local_topic(name), "title": "", "format": "?", "status": None,
            "duration": None, "youtube_id": None, "size": 0, "photo": False, "active": False,
        }  # fmt: skip
        if name not in gx:
            path = LOCAL / name / "status.json"
            status = json.loads(path.read_text()) if path.exists() else {}
            base["visual_qc"] = visual_summary(LOCAL / name, status)
        out.append({**base, "gx10": name in gx, "mac": name in mac})
    return out


def download(job: str) -> Path:
    """從 GX10 下載成片、縮圖與企劃／QC JSON 到 ~/Downloads/videoMaker/<作業>/。"""
    _need_valid(job)
    dest = LOCAL / job
    dest.mkdir(parents=True, exist_ok=True)
    src = f"{GX10}:code/videoMaker/work/{job}/"
    keep = [
        f"--include={f}"
        for f in ("final.mp4", "thumb.jpg", "plan.json", "meta.json", "status.json", "visual_qc.json")
    ]
    r = _sh(["rsync", "-ac", *keep, "--exclude=*", src, f"{dest}/"], timeout=1800)
    if r.returncode or not (dest / "final.mp4").exists():
        raise RuntimeError(f"下載失敗：{(r.stderr or '找不到 final.mp4')[-200:].strip()}")
    return dest / "final.mp4"


def remote_cli(*args: str) -> str:
    """在 GX10 上執行 videomaker 子指令（approve／reject／upload／upload-approved），回傳輸出。"""
    for a in args:
        if not (a.startswith("-") or a in {"approve", "reject", "upload", "upload-approved"} or valid(a)):
            raise ValueError(f"不允許的參數：{a!r}")
    r = _gx(f"cd {GX10_PROJECT} && .venv/bin/python -m videomaker {' '.join(args)}", timeout=900)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout)[-300:].strip() or f"exit {r.returncode}")
    return (r.stdout + r.stderr).strip()


def delete_gx10(job: str) -> None:
    _need_valid(job)
    r = _gx(f"rm -r {GX10_PROJECT}/work/{job}")
    if r.returncode:
        raise RuntimeError(f"刪除 GX10 存檔失敗：{r.stderr[-200:].strip()}")


def delete_local(job: str) -> None:
    """只刪除 Mac 下載目錄內的指定作業；rmtree 不會跟隨符號連結刪除外部檔案。"""
    _need_valid(job)
    shutil.rmtree(LOCAL / job)
