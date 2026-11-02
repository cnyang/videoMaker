"""Facebook 粉絲專頁 Reels：人工核准後上傳草稿，發布留給人在 Meta 操作。

三階段 API 與狀態欄位：https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api
憑證在專案外；沿用 YouTube 的核准 hash、跨行程鎖與原子寫入，不改生成管線。
"""

import getpass
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from contextlib import nullcontext
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, NamedTuple
from urllib.parse import urlsplit

import requests

from . import config, youtube
from .plan import Plan

log = logging.getLogger("videomaker")

MIN_SECONDS, MAX_SECONDS = 4, 60  # 官方範例的 Reels 長度；TUI 也靠它在本機先擋掉長片
POLL_ATTEMPTS, POLL_SECONDS = 60, 5  # 最多約 5 分鐘（實測處理曾超過 150 秒）
# 只有 Meta 明確回報這兩種才算草稿；其餘（含欄位缺漏）一律不當成完成，寧可停在 processing
_DRAFT_STATES = {"draft", "not_started"}
# 終態：不自動重試也不重建，要人到 Meta 確認後手動清掉 facebook_reel 欄位
_TERMINAL = {
    "failed": "Facebook 處理影片失敗（影片 ID {}），請到 Meta 處理；確認後備份 meta.json 並移除 facebook_reel 欄位才能重跑",
    "published": "Facebook 回傳非草稿狀態（影片 ID {}），請立即到 Meta 檢查；本程式不會再處理這支",
}


class _NotCreated(RuntimeError):
    """Meta 明確拒絕（4xx 且有 error 物件）或根本沒連上：遠端沒有留下任何物件，可安全重來。"""


class _Pending(NamedTuple):
    video_id: str
    status_url: str
    token: str


def _credential_path() -> Path:
    path = (config.FB_DIR / "facebook.json").resolve()
    if path.is_relative_to(config.ROOT):
        raise ValueError("Facebook 憑證必須放在專案外，請調整 VMK_FB_DIR")
    return path


def _validate_credentials(data: dict) -> tuple[str, str]:
    page, token = data.get("page_id", ""), data.get("page_access_token", "")
    if not isinstance(page, str) or not re.fullmatch(r"[0-9]+", page):
        raise ValueError("請設定數字格式的 Facebook 粉絲專頁 ID")
    # 非 ASCII 的 token 送出時會在 requests 內拋 UnicodeEncodeError，在這裡先擋才有明確訊息
    if not isinstance(token, str) or not token or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("請設定有效的 Page access token（不可含空白或非 ASCII 字元）")
    return page, token


def auth() -> None:
    """互動輸入 Page ID 與 token；token 不進命令列、終端回顯或日誌。"""
    path = _credential_path()
    data = {
        "page_id": input("Facebook 粉絲專頁 ID：").strip(),
        "page_access_token": getpass.getpass("Page access token（不顯示）：").strip(),
    }
    _validate_credentials(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 先寫同目錄暫存檔再 replace：中途中斷或磁碟滿時，原本可用的憑證不會被截斷
    temp = path.with_suffix(".tmp")
    try:
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    log.info("Facebook 憑證已儲存（權限 600）：%s", path)


def _request(session: requests.Session, method: str, url: str, token: str, **kwargs) -> dict:
    headers = {"Authorization": f"OAuth {token}", **kwargs.pop("headers", {})}
    try:
        response = session.request(
            method, url, headers=headers, timeout=(15, 180), allow_redirects=False, **kwargs
        )
        data = response.json()
    except requests.ConnectTimeout:
        # 連線階段就逾時＝請求根本沒送出；不轉印原始例外，理由同下
        raise _NotCreated("Facebook API 連線逾時，請稍後重試同一作業") from None
    except (requests.RequestException, ValueError):
        # 不轉印原始例外：request／response 可能含 token；保留作業內的 session 供重試。
        raise RuntimeError("Facebook API 連線失敗或回應無效，請稍後重試同一作業") from None
    if not isinstance(data, dict):
        raise TypeError("Facebook API 回應格式無效")
    if not 200 <= response.status_code < 300 or "error" in data:
        error = data.get("error", {})
        code = error.get("code") if isinstance(error, dict) else None
        safe_code = code if isinstance(code, int) else "unknown"
        message = f"Facebook API 失敗（HTTP {response.status_code}，code {safe_code}），請檢查權限與 token"
        # ponytail: DNS 失敗、連線被拒等仍視為結果不明（requests 分不出送出前後）；真遇到再依 urllib3 例外細分
        raise (
            _NotCreated if 400 <= response.status_code < 500 and isinstance(error, dict) else RuntimeError
        )(message)
    return data


def _status(client: requests.Session, url: str, token: str) -> dict:
    return _request(client, "GET", url, token, params={"fields": "status"})["status"]


def _check_video(stream: BinaryIO) -> None:
    """依官方範例保守限制 4–60 秒；不自動裁切已核准的成片。"""
    stream.seek(0)  # /dev/fd 在部分平台與子行程共用 offset，先歸零避免讀到檔尾
    probe = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate:format=duration",
                "-of",
                "json",
                f"/dev/fd/{stream.fileno()}",
            ],
            pass_fds=(stream.fileno(),),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    video = probe["streams"][0]
    width, height = video["width"], video["height"]
    if not (
        MIN_SECONDS <= float(probe["format"]["duration"]) <= MAX_SECONDS
        and width >= 540
        and height >= 960
        and width * 16 == height * 9
        and Fraction(video["avg_frame_rate"]) >= 23
    ):
        raise ValueError("FB Reels 目前只接受 4–60 秒、9:16、至少 540×960、23fps 的成片；請選短片")


def _upload_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "rupload.facebook.com"
        or not re.fullmatch(r"/video-upload/v[0-9]+\.[0-9]+/[0-9]+", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Facebook 回傳的上傳網址無效，未傳送憑證或影片")
    return url


def _finish_received(status: dict) -> bool:
    """Meta 是否已收到 finish：開始處理、處理完成或已有發布階段狀態。"""
    phases = (status.get(key, {}) for key in ("processing_phase", "publishing_phase"))
    return status.get("video_status") in {"processing", "ready"} or any(
        p.get("status") in {"in_progress", "complete"} for p in phases
    )


def upload(job_dir: Path, session: requests.Session | None = None) -> str:
    """上傳或續跑同一支 Reel；只有確認處理完成後才記為草稿。
    鎖只護住「讀寫 meta 與送出請求」；輪詢處理進度（最久約 5 分鐘）不持鎖，不擋審核與 YouTube 上傳。"""
    with requests.Session() if session is None else nullcontext(session) as client:
        with youtube.locked(job_dir):
            pending = _submit(job_dir, client)
        return pending if isinstance(pending, str) else _await_draft(job_dir, client, pending)


def _record(job_dir: Path, **fields) -> None:
    """輪詢結果寫回 meta：此時才取鎖並重讀，避免蓋掉別的行程（如 YouTube 上傳）剛寫的欄位。"""
    with youtube.locked(job_dir):
        meta_path = job_dir / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["facebook_reel"] |= fields
        youtube.save_json(meta_path, meta)


def _submit(job_dir: Path, client: requests.Session) -> str | _Pending:
    """送出（或續送）到 finish 為止。已是草稿回傳影片 ID；否則回傳待輪詢的資訊。"""
    meta_path = job_dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    review = json.loads((job_dir / "status.json").read_text())
    if review["status"] != "approved":
        raise ValueError("必須先人工核准（videomaker approve）才能上傳 FB 草稿")
    if review.get("approved_basis") == "sources":
        # 硬規則 3：合輯「來源全核准」的例外只限 YouTube；FB 一定要有人看過這支成片
        raise ValueError("合輯是依來源核准的，不適用於 FB；請先按 o 看片、再按 a 人工核准")
    with (job_dir / "final.mp4").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if review.get("approved_sha256") != digest:
            raise ValueError("核准未綁定目前成片，請重新人工核准（videomaker approve）")
        reel = meta.get("facebook_reel")
        if reel and reel["sha256"] != digest:
            raise ValueError("既有 FB Reel 的成片不同，請先人工處理既有草稿，不自動重建")
        # 以下三種不需要憑證與企劃，也不連網：已完成就直接回傳，終態與結果不明則拒絕
        if reel and reel["phase"] == "draft":
            return reel["video_id"]
        if reel and reel["phase"] in _TERMINAL:
            raise RuntimeError(_TERMINAL[reel["phase"]].format(reel["video_id"]))
        if reel and reel["phase"] == "starting":
            raise ValueError("先前建立 FB 上傳 session 的結果不明，請先到 Meta 檢查，避免重複建立草稿")
        page, token = _validate_credentials(json.loads(_credential_path().read_text()))
        if reel and reel["page_id"] != page:
            raise ValueError("既有 FB Reel 屬於另一個專頁，請先人工處理既有草稿，不自動重建")
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", config.FB_API_VERSION):
            raise ValueError("VMK_FB_API_VERSION 格式必須為 v25.0 之類的版本號")
        graph = f"https://graph.facebook.com/{config.FB_API_VERSION}"
        endpoint = f"{graph}/{page}/video_reels"
        fresh = not reel
        if fresh:
            _check_video(stream)  # 續傳的成片 hash 相同，早已檢查過
            stream.seek(0)
            meta["facebook_reel"] = {"phase": "starting", "page_id": page, "sha256": digest}
            youtube.save_json(meta_path, meta)
            try:
                started = _request(client, "POST", endpoint, token, data={"upload_phase": "start"})
            except _NotCreated:
                del meta["facebook_reel"]  # 確定沒建立遠端物件：撤回標記，修好 token／權限後可直接重跑
                youtube.save_json(meta_path, meta)
                raise
            video_id = str(started.get("video_id", ""))
            if not re.fullmatch(r"[0-9]+", video_id):
                raise ValueError("Facebook 回傳的影片 ID 無效")
            reel = meta["facebook_reel"] = {
                "video_id": video_id,
                "page_id": page,
                "sha256": digest,
                "upload_url": str(started.get("upload_url", "")),
                "phase": "created",
            }
            youtube.save_json(
                meta_path, meta
            )  # 取得 ID 立刻記錄；之後（含網址無效）失敗都沿用同一個遠端物件。
            _upload_url(reel["upload_url"])
        video_id = reel["video_id"]
        status_url = f"{graph}/{video_id}"
        if reel["phase"] == "created":
            size, offset = os.fstat(stream.fileno()).st_size, 0
            phase = {}
            if not fresh:
                phase = _status(client, status_url, token).get("uploading_phase", {})
                offset = int(phase.get("bytes_transfered", 0))
            if not 0 <= offset <= size:
                raise ValueError("Facebook 回傳的續傳位置無效")
            if phase.get("status") != "complete" and offset < size:
                stream.seek(offset)
                result = _request(
                    client,
                    "POST",
                    _upload_url(reel["upload_url"]),
                    token,
                    data=stream,
                    headers={
                        "offset": str(offset),
                        "file_size": str(size),
                        "Content-Type": "application/octet-stream",
                    },
                )
                if result.get("success") is not True:
                    raise RuntimeError("Facebook 未確認收到影片，請重試同一作業")
            reel["phase"] = "uploaded"
            youtube.save_json(meta_path, meta)
        # finish 的回應可能遺失：先問 Meta 收到沒有，收到就不重送（二次 finish 的行為未知）
        if reel["phase"] == "finishing" and _finish_received(_status(client, status_url, token)):
            reel["phase"] = "processing"
            youtube.save_json(meta_path, meta)
        if reel["phase"] in {"uploaded", "finishing"}:
            reel["phase"] = "finishing"
            youtube.save_json(meta_path, meta)
            plan = Plan.model_validate_json((job_dir / "plan.json").read_text())
            result = _request(
                client,
                "POST",
                endpoint,
                token,
                data={
                    "upload_phase": "finish",
                    "video_id": video_id,
                    "video_state": "DRAFT",
                    "title": (plan.youtube.title or plan.topic)[:100],
                    "description": plan.youtube.description[:4900],
                    "is_ai_generated": "true",
                },
            )
            if result.get("success") is not True:
                raise RuntimeError("Facebook 未確認建立草稿，請重試同一作業")
            reel["phase"] = "processing"
            youtube.save_json(meta_path, meta)
    return _Pending(video_id, status_url, token)


def _await_draft(job_dir: Path, client: requests.Session, pending: _Pending) -> str:
    video_id, status_url, token = pending
    for attempt in range(POLL_ATTEMPTS):
        status = _status(client, status_url, token)
        phases = [status.get(key, {}) for key in ("uploading_phase", "processing_phase", "publishing_phase")]
        publish_status = status.get("publishing_phase", {}).get("publish_status")
        if status.get("video_status") in {"error", "upload_failed", "expired"} or any(
            p.get("status") == "error" or p.get("publish_status") == "error" for p in phases
        ):
            _record(job_dir, phase="failed")
            raise RuntimeError(_TERMINAL["failed"].format(video_id))
        if publish_status in {"published", "scheduled"}:
            _record(job_dir, phase="published")
            raise RuntimeError(_TERMINAL["published"].format(video_id))
        if status.get("video_status") == "ready" and publish_status in _DRAFT_STATES:
            _record(
                job_dir,
                phase="draft",
                requested_state="DRAFT",
                completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            log.info("FB 草稿已處理完成（未發布），影片 ID：%s", video_id)
            return video_id
        if attempt < POLL_ATTEMPTS - 1:
            time.sleep(POLL_SECONDS)
    raise TimeoutError(
        f"Facebook 仍在處理，或已處理完成但未確認為草稿（影片 ID {video_id}）；"
        "請到 Meta 確認。重跑同一作業只會查狀態，不重複上傳"
    )
