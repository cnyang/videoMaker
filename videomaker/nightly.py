"""夜間排程：LLM 想主題 → 依序產生短片、長片、5 分鐘各 1 支。由 cron 週一到週四 19:30 啟動，deadline（預設 07:00）前必須結束。

所有成片都停在 pending_review，由人工審核；這裡不碰 YouTube。
"""

import json
import logging
import subprocess
import time
from datetime import datetime, timedelta

import requests

from . import config, pipeline, wiki
from .plan import BANNED, FORMATS, KINDS, SIMPLIFIED, extract_json

log = logging.getLogger("videomaker")
# 每晚：短片、長片、5 分鐘各 1 支；由短到長，時間不夠時被略過的是最耗時的 xl
NIGHT = ["short", "long", "xl"]
NIGHT_TRIVIA = KINDS["trivia"]  # 小知識夜：支援的片型就是全部（short、long），由 plan.KINDS 統一管理
PLANS = {"kids": NIGHT, "trivia": NIGHT_TRIVIA}
SEC_PER_STEP = 17.3  # Wan 5B 單鏡實測：20 步 ≈ 346 秒
RETRY_WAIT = 600  # 一支失敗後等 10 分鐘再試一次（可能是白天殘留的記憶體占用或暫時性錯誤）
COOL_POLL = 30  # 等降溫時每 30 秒量一次 GPU 溫度


def gpu_temp() -> int | None:
    """GPU 溫度（°C）；讀不到（沒有 nvidia-smi、非 GX10）回 None，視為不需要等。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        return int(out.splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def wait_cool(latest_start: datetime, sleep=time.sleep) -> None:
    """GPU 降到 config.COOL_TEMP 以下才返回；超過 latest_start（再不開跑就趕不上 deadline）也返回，由呼叫端決定略過。"""
    t = gpu_temp()
    if t is not None and t > config.COOL_TEMP:
        log.info("GPU %d°C > %d°C，等降溫再開跑", t, config.COOL_TEMP)
    while t is not None and t > config.COOL_TEMP and datetime.now().astimezone() < latest_start:
        sleep(COOL_POLL)
        t = gpu_temp()


def estimate(fmt: str) -> float:
    """預估一支要多久（秒，含 15% 餘裕）：生成、視覺抽查與載入、企劃、音樂、關鍵幀、合成。"""
    unique = FORMATS[fmt]["unique"] or 5
    return (unique * (SEC_PER_STEP * config.STEPS + 12) + 30 + (180 if fmt == "short" else 600)) * 1.15


def deadline_after(now: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    dl = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return dl if dl > now else dl + timedelta(days=1)


def _history_path():
    return config.WORK / "history.jsonl"


def _past_topics() -> list[str]:
    topics = []
    if _history_path().exists():
        topics += [json.loads(x)["topic"] for x in _history_path().read_text().splitlines() if x]
    topics += [json.loads(p.read_text())["topic"] for p in config.WORK.glob("*/plan.json")]
    return list(dict.fromkeys(topics))


def _ask_topics(prompt: str) -> list:
    """問 LLM 並解析出 {"topics": [...]}；網路錯誤維持 requests 的例外，格式不對丟 ValueError／KeyError／TypeError。"""
    r = requests.post(
        f"{config.LLM_URL}/chat/completions",
        json={
            "model": config.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 1.0,
        },
        timeout=300,
    )
    r.raise_for_status()
    return json.loads(extract_json(r.json()["choices"][0]["message"]["content"]))["topics"]


def suggest_topics(n: int, avoid: list[str]) -> list[str]:
    """請 LLM 想 n 個新主題（繁體中文短語），不得與 avoid 重複。"""
    prompt = (
        f"Suggest {n} different topics for 15-20 second instrumental kids music videos for ages 3-8. "
        "Each topic is a short phrase in Traditional Chinese as used in Taiwan (繁體中文, NEVER Simplified characters like 猫/鸟/风) of 2-8 characters about a simple everyday habit, an animal, "
        "nature or a gentle activity, like 小熊刷牙 (but do not reuse it). Safe for children, nothing scary, no brands or "
        "existing cartoon characters. Avoid themes about teeth, mouths, eating, drinking, water, bathing or washing, and "
        "anything needing thin flexible objects (the image models draw those badly). "
        f"Do NOT repeat or closely resemble any of: {'、'.join(avoid[-200:]) or '(none)'}. "
        'Return ONLY JSON: {"topics": ["...", "..."]}'
    )
    for attempt in range(3):
        try:
            raw = _ask_topics(prompt)
        except (ValueError, KeyError, TypeError) as e:
            log.warning("主題第 %d 次解析失敗：%s", attempt + 1, e)
            continue
        good = [t.strip() for t in dict.fromkeys(raw) if isinstance(t, str) and 2 <= len(t.strip()) <= 12]
        good = [t for t in good if t not in avoid and not BANNED.search(t) and not SIMPLIFIED.search(t)]
        if len(good) >= n:
            return good[:n]
        log.warning("主題第 %d 次只有 %d 個可用（需要 %d）", attempt + 1, len(good), n)
    raise RuntimeError("LLM 3 次都想不出足夠的新主題")


def suggest_trivia_topics(n: int, avoid: list[str]) -> list[str]:
    """請 LLM 想英文維基百科條目名稱，逐一抓取驗證（存在、不是消歧義頁、夠長）；抓不到的略過，來源一定是真的條目。"""
    prompt = (
        f"Suggest {n + 4} different topics for kids trivia videos (ages 3-8). Each must be the EXACT title of an existing "
        "English Wikipedia article about one specific animal, plant, planet, natural phenomenon or everyday object that "
        "children love, with plenty of text on the page (for example: Sea otter, Giraffe, Rainbow). Safe for children: nothing "
        "scary, dangerous, gory or political; no people, brands or fictional characters. "
        f"Do NOT repeat any of: {'、'.join(avoid[-200:]) or '(none)'}. "
        'Return ONLY JSON: {"topics": ["...", "..."]}'
    )
    good: list[str] = []
    for attempt in range(3):
        try:
            raw = _ask_topics(prompt)
        except (ValueError, KeyError, TypeError) as e:
            log.warning("小知識主題第 %d 次解析失敗：%s", attempt + 1, e)
            continue
        for title in dict.fromkeys(t.strip() for t in raw if isinstance(t, str)):
            if title in avoid or BANNED.search(title):
                continue
            try:
                article = wiki.fetch(title)
            except (ValueError, requests.RequestException) as e:
                log.warning("維基百科條目 %s 不可用，略過：%s", title, e)
                continue
            if article.title not in avoid and article.title not in good:
                good.append(article.title)
            if len(good) >= n:
                return good
    raise RuntimeError(f"LLM 3 次都想不出足夠的小知識主題（需要 {n}，可用 {len(good)}）")


def nightly(plan=None, deadline="07:00", dry_run=False, sleep=time.sleep, kind="kids") -> list[dict]:
    plan = plan or PLANS[kind]
    if bad := [f for f in plan if f not in KINDS[kind]]:  # 確定會失敗的別拿去重試、浪費 10 分鐘與主題
        raise ValueError(f"{kind} 不支援片型 {bad}（可用 {KINDS[kind]}）")
    dl = deadline_after(datetime.now().astimezone(), deadline)
    topics = (suggest_topics if kind == "kids" else suggest_trivia_topics)(len(plan), _past_topics())
    log.info("今晚主題：%s；deadline %s", topics, dl.strftime("%m-%d %H:%M"))
    results = []
    for fmt, topic in zip(plan, topics, strict=True):
        entry = {
            "date": datetime.now().astimezone().isoformat(timespec="seconds"),
            "topic": topic,
            "format": fmt,
            "kind": kind,  # 週五合併靠它把兒童音樂與小知識分開
        }
        if not dry_run:
            wait_cool(dl - timedelta(seconds=estimate(fmt)), sleep)  # 每支開跑前先等 GPU 降溫
        if datetime.now().astimezone() + timedelta(seconds=estimate(fmt)) > dl:
            entry["status"] = "skipped"  # 預估來不來得及：不硬跑，避免拖到白天占用 GPU
            log.warning("[%s] %s 預估 %.0f 分鐘，趕不上 deadline，略過", fmt, topic, estimate(fmt) / 60)
        elif dry_run:
            entry["status"] = "dry-run"
        else:
            entry |= _run_with_retry(topic, fmt, dl, sleep, kind)
        results.append(entry)
        if not dry_run:
            config.WORK.mkdir(parents=True, exist_ok=True)
            with _history_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return results


def _run_with_retry(topic: str, fmt: str, dl: datetime, sleep, kind: str = "kids") -> dict:
    err = ""
    for attempt in range(2):
        try:
            job = pipeline.run(topic, fmt=fmt, resume=attempt > 0, kind=kind)
            return {"status": json.loads(job.p("status.json").read_text())["status"], "job": job.dir.name}
        except Exception as e:  # noqa: BLE001 夜間排程不能被單支失敗拖垮：記錄後換下一支
            err = repr(e)
            log.error("[%s] %s 第 %d 次失敗：%s", fmt, topic, attempt + 1, err)
            if attempt == 0:
                if datetime.now().astimezone() + timedelta(seconds=RETRY_WAIT + estimate(fmt)) > dl:
                    break
                sleep(RETRY_WAIT)
                wait_cool(dl - timedelta(seconds=estimate(fmt)), sleep)  # 重試也算一次開跑
    return {"status": "failed", "error": err}
