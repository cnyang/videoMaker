"""YouTube 上傳：OAuth（Desktop app）、可續傳上傳、人工核准閘門。

憑證與 token 放在專案外（預設 ~/.config/videomaker/，權限 600），絕不進 git。
未通過 Google API 稽核的專案，videos.insert 上傳的影片會被強制為私人 → 一律以 private 上傳，由人在 Studio 手動公開。
"""

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config
from .plan import Plan

log = logging.getLogger("videomaker")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CRED_DIR = Path(os.getenv("VMK_YT_DIR", "~/.config/videomaker")).expanduser()
CLIENT_SECRET = CRED_DIR / "client_secret.json"
TOKEN = CRED_DIR / "token.json"
CATEGORY = "10"  # Music
NOT_FOR_KIDS = ("animal_sitcom", "dialogue_animation")  # 非兒童內容：明確送 selfDeclaredMadeForKids=False
TRANSIENT = (500, 502, 503, 504)


def auth() -> None:
    """在有瀏覽器的機器上（Mac）跑一次：開瀏覽器授權，選專屬帳號與頻道，存 token。"""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    log.info("token 已存到 %s", TOKEN)


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds.valid:
        creds.refresh(Request())  # access token 約 1 小時過期，用 refresh token 換新的
        TOKEN.write_text(creds.to_json())
        TOKEN.chmod(0o600)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _tags(tags: list[str]) -> list[str]:
    """YouTube 規定標籤總長 ≤ 500 字元（含空白的標籤會被加引號，各多算 2）。"""
    out, total = [], 0
    for t in tags:
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        out.append(t)
        total += cost
    return out


def build_body(plan: Plan, meta: dict) -> dict:
    yt = plan.youtube
    return {
        "snippet": {
            "title": (yt.title or plan.topic)[:100],
            "description": yt.description[:4900],
            "tags": _tags(yt.tags),
            "categoryId": CATEGORY,
        },
        "status": {
            "privacyStatus": "private",  # 永遠私人：由人在 Studio 審核後手動公開
            # 動物短劇與有對白的動畫不是兒童內容：要明確送 False；不送的話 YouTube 會套用頻道預設（頻道預設是「為兒童打造」）
            "selfDeclaredMadeForKids": plan.kind not in NOT_FOR_KIDS,
            # 照片模式是「真實照片經 AI 動畫化」，寫實風格依 spec 設為 true；純卡通不設
            "containsSyntheticMedia": bool(meta.get("images")),
        },
    }


def video_sha256(job_dir: Path) -> str:
    with (job_dir / "final.mp4").open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def locked(job_dir: Path):
    # 鎖檔不能刪：不同 TUI／CLI 行程必須鎖住同一個 inode，才能保證只上傳一次
    with (job_dir / ".upload.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def set_review(
    job_dir: Path, status: str, expected_sha256: str | None = None, basis: str | None = None
) -> None:
    """人工審核結果：approved / rejected，寫回 status.json。basis 記錄核准依據（合輯來源核准記 "sources"）。"""
    with locked(job_dir):
        path = job_dir / "status.json"
        st = json.loads(path.read_text())
        st.pop("approved_basis", None)  # 重新審核就不沿用上一次的依據
        if status == "approved":
            digest = video_sha256(job_dir)
            if expected_sha256 is not None and digest != expected_sha256:
                raise ValueError("成片已變更，請重新下載開啟後再人工核准")
            st["approved_sha256"] = digest
            if basis:
                st["approved_basis"] = basis
        else:
            st.pop("approved_sha256", None)
        st |= {"status": status, "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        save_json(path, st)


def sources_approved(job_dir: Path) -> bool | None:
    """合輯（meta 有 sources）的來源是否全部已人工核准；不是合輯回傳 None。
    來源在 GX10 上已不存在（刪除後）無從確認，視為沒核准——寧可退回手動審核。"""
    sources = json.loads((job_dir / "meta.json").read_text()).get("sources")
    if not sources:
        return None

    def approved(name: str) -> bool:
        path = job_dir.parent / name / "status.json"
        return path.exists() and json.loads(path.read_text())["status"] == "approved"

    return all(approved(s) for s in sources)


def approve_from_sources(job_dir: Path) -> None:
    """合輯的來源都已人工核准 → 直接核准這支合輯（仍綁定目前成片 hash）。
    只放行待審核的：合輯自己 QC 失敗或被人工否決的不覆蓋；已核准的不動。"""
    status = json.loads((job_dir / "status.json").read_text())["status"]
    if status == "approved":
        return
    if sources_approved(job_dir) is not True:
        raise ValueError(f"{job_dir.name} 不是合輯，或來源尚未全部人工核准，請人工審核")
    if status != "pending_review":
        raise ValueError(f"{job_dir.name} 狀態是 {status}，不能依來源核准，請人工審核")
    set_review(job_dir, "approved", basis="sources")


def upload(job_dir: Path, service=None) -> str:
    """上傳一支已核准的成片（私人）。已上傳過就直接回傳既有的 id（冪等）。"""
    with locked(job_dir):
        return _upload(job_dir, service)


def _upload(job_dir: Path, service) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    meta_path = job_dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    if meta.get("youtube_id"):
        return meta["youtube_id"]
    review = json.loads((job_dir / "status.json").read_text())
    status = review["status"]
    if status != "approved":
        raise ValueError(f"{job_dir.name} 狀態是 {status}，必須先人工核准（videomaker approve）才能上傳")
    media = MediaFileUpload(
        job_dir / "final.mp4", mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True
    )
    with media.stream() as stream:
        stream.seek(0)  # MediaFileUpload 建構時會為了量長度把游標移到檔尾
        # 驗證與上傳使用同一個已開啟檔案；合成途中 replace 路徑，也不會換成沒看過的新內容
        if review.get("approved_sha256") != hashlib.file_digest(stream, "sha256").hexdigest():
            raise ValueError("核准未綁定目前成片，請重新人工核准（videomaker approve）")
        stream.seek(0)
        body = build_body(Plan.model_validate_json((job_dir / "plan.json").read_text()), meta)
        req = (service or _service()).videos().insert(part="snippet,status", body=body, media_body=media)
        resp, retries = None, 0
        while resp is None:
            try:
                _, resp = req.next_chunk()
            except HttpError as e:  # 暫時性的伺服器錯誤：退避後續傳；其他（額度、權限）直接拋出
                if e.resp.status in TRANSIENT and retries < 5:
                    retries += 1
                    log.warning("上傳遇到 %s，第 %d 次重試", e.resp.status, retries)
                    time.sleep(2**retries)
                    continue
                raise
        meta |= {
            "youtube_id": resp["id"],
            "youtube_uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "youtube_privacy": resp.get("status", {}).get("privacyStatus"),  # API 回傳的實際隱私狀態，當證據
        }
        save_json(meta_path, meta)
        log.info("已上傳（私人）：https://studio.youtube.com/video/%s/edit", resp["id"])
        return resp["id"]


def pending_approved() -> list[Path]:
    """work/ 底下已核准、還沒上傳的作業。"""
    out = []
    for st in sorted(config.WORK.glob("*/status.json")):
        meta = json.loads((st.parent / "meta.json").read_text())
        if json.loads(st.read_text())["status"] == "approved" and not meta.get("youtube_id"):
            out.append(st.parent)
    return out


def resolve(job: str) -> Path:
    """接受完整路徑或 work/ 底下的資料夾名。"""
    p = Path(job)
    return p if p.is_dir() else config.WORK / job


def listing() -> list[str]:
    """每支作業一行：資料夾 | 片型 | 狀態 | 長度 | 主題。狀態：待審核、已核准（待上傳）、已上傳、不通過、QC 失敗、未完成。"""
    rows = []
    for d in sorted(p for p in config.WORK.iterdir() if (p / "plan.json").exists()):
        meta = json.loads((d / "meta.json").read_text())
        topic = json.loads((d / "plan.json").read_text())["topic"]
        if not (d / "status.json").exists():
            state, dur = "未完成", "-"
        else:
            st = json.loads((d / "status.json").read_text())
            dur = f"{st.get('details', {}).get('duration', 0):.0f}s"
            state = {
                "pending_review": "待審核",
                "approved": "已核准（待上傳）",
                "rejected": "不通過",
                "failed": "QC 失敗",
            }.get(st["status"], st["status"])
        if meta.get("youtube_id"):
            state = f"已上傳 {meta['youtube_id']}"
        rows.append(f"{d.name} | {meta.get('format', 'short')} | {state} | {dur} | {topic}")
    return rows


# ---------- token 健康檢查與自動更新（OAuth 同意畫面為 Testing 時 refresh token 7 天就失效）----------
GX10 = os.getenv("VMK_GX10", "gx10")  # ssh 別名；GX10 上的 token 才是自動上傳實際使用的
REMOTE_TOKEN = ".config/videomaker/token.json"
REMOTE_PY = "cd ~/code/videoMaker && .venv/bin/python -m videomaker"


def token_status() -> str:
    """實際用 refresh token 向 Google 換一次（只看本機時間抓不到被撤銷／7 天過期）：ok / expired / missing。"""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN.exists():
        return "missing"
    creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    try:
        creds.refresh(Request())
    except RefreshError:  # invalid_grant：Token has been expired or revoked
        return "expired"
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    return "ok"


def _sh(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _ask(message: str, wait: int = 3600) -> bool:
    """macOS 對話框（需在使用者的圖形登入工作階段內執行，例如 LaunchAgent）。回傳是否按下「重新授權」。"""
    script = (
        f'display dialog "{message}" with title "videoMaker" buttons {{"稍後再說", "重新授權"}} '
        f'default button "重新授權" with icon caution giving up after {wait}'
    )
    return "button returned:重新授權" in _sh(["osascript", "-e", script], timeout=wait + 100).stdout


def _notify(message: str) -> None:
    _sh(["osascript", "-e", f'display notification "{message}" with title "videoMaker"'])


def remote_status() -> str:
    """GX10 上的 token 狀態：ok / expired / missing / unreachable。"""
    r = _sh(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", GX10, f"{REMOTE_PY} token-status"],
        timeout=90,
    )
    out = r.stdout.strip().splitlines()
    return out[-1] if out and out[-1] in ("ok", "expired", "missing") else "unreachable"


def renew_token() -> str:
    """每天自動跑：GX10 的 token 正常就什麼都不做；過期就跳視窗，你按「重新授權」後開瀏覽器授權，並自動複製到 GX10。"""
    status = remote_status()
    log.info("GX10 token 狀態：%s", status)
    if status == "ok":
        return "ok"
    if status == "unreachable":
        _notify("連不上 GX10，無法檢查 YouTube token")
        return "unreachable"
    reason = "已過期" if status == "expired" else "不存在"
    if not _ask(
        f"GX10 上的 YouTube 上傳授權{reason}，自動上傳會失敗。要現在重新授權嗎？（會開啟瀏覽器，請選專屬帳號）"
    ):
        return "declined"
    if token_status() != "ok":  # 本機 token 已經有效（例如你先前授權過但沒複製）就不必再授權一次
        auth()
    if (
        _sh(["scp", "-q", str(TOKEN), f"{GX10}:{REMOTE_TOKEN}"]).returncode
        or _sh(["ssh", GX10, f"chmod 600 {REMOTE_TOKEN}"]).returncode
    ):
        _notify("token 複製到 GX10 失敗")
        return "copy-failed"
    final = remote_status()
    _notify("YouTube 授權已更新並複製到 GX10" if final == "ok" else f"已複製但 GX10 驗證仍是 {final}")
    return final
