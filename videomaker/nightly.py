"""夜間排程：LLM 想主題 → 依序產生成片，結束後合併同一種內容。由 cron 週一到週五 19:30 啟動（`--kind auto` 依星期決定今晚做哪一種），
deadline（預設 07:00）前必須結束。

週一兒童音樂、週二小知識、週三兒童英文對話、週四動物短劇、週五有對白的動畫；每晚生成完再跑該種內容的合併（ffmpeg，不用 GPU）。
所有成片都停在 pending_review，由人工審核；這裡不碰 YouTube。
"""

import json
import logging
import subprocess
import time
from datetime import datetime, timedelta

import requests

from . import config, content, pipeline, renderers, weekly, wiki
from .plan import BANNED, DIALOGUE_KINDS, FORMATS, KINDS, SIMPLIFIED, extract_json

log = logging.getLogger("videomaker")
# 每晚：短片、長片、5 分鐘各 1 支；由短到長，時間不夠時被略過的是最耗時的 xl
# 生成完把當晚的 short 與 long 合併成一支，xl 不合併（見 weekly.NO_MERGE_FORMATS）
NIGHT = ["short", "long", "xl"]
NIGHT_TRIVIA = KINDS["trivia"]  # 小知識夜：支援的片型就是全部（short、long），由 plan.KINDS 統一管理
DIALOGUE_PER_NIGHT = 2  # 對白類每晚產 2 支（各自的主題），生成完把這 2 支合併
PLANS = {"kids": NIGHT, "trivia": NIGHT_TRIVIA, **{k: KINDS[k] * DIALOGUE_PER_NIGHT for k in DIALOGUE_KINDS}}
# 星期（Monday = 0）→ 今晚的內容種類；週末不跑。cron 只要每個工作天 19:30 呼叫 `nightly --kind auto`
SCHEDULE = {0: "kids", 1: "trivia", 2: "kids_english_dialogue", 3: "animal_sitcom", 4: "dialogue_animation"}
SEC_PER_STEP = 17.3  # Wan 5B 單鏡實測：20 步 ≈ 346 秒
# 每個獨特鏡頭的關鍵幀秒數：兒童音樂 FLUX.3 + upsampler 實測 14 鏡 466 秒 ≈ 33 秒/張（含載入）；小知識 schnell 約 12 秒
KEYFRAME_SEC = {"kids": 35, "trivia": 12}
DIALOGUE_KEYFRAME_SEC = (
    12  # 對白類 fl2va 的 FLUX schnell 關鍵幀（與小知識同一個 workflow）；ref2va 不需要關鍵幀
)
DIALOGUE_OVERHEAD = 360  # 對白類非生成的固定開銷：企劃 LLM 與卸載、合成、視覺抽查
HISTORY_RATE_RUNS = 10  # 用最近幾支 H3 作業的實際每秒成本估算
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


def h3_sec_per_video_sec(renderer: str) -> float:
    """H3 每產出 1 秒成片要多少生成秒數：取 history 最近 HISTORY_RATE_RUNS 支同 renderer 實際值的中位數；
    還沒有資料（或檔案壞掉）就用 config.H3_DEFAULT_SEC_PER_SEC 的保守值。"""
    rates = []
    try:
        for line in _history_path().read_text(encoding="utf-8").splitlines():
            e = json.loads(line) if line else {}
            if e.get("renderer") == renderer and isinstance(e.get("gen_sec_per_video_sec"), (int, float)):
                rates.append(e["gen_sec_per_video_sec"])
    except (OSError, ValueError):
        pass
    rates = sorted(rates[-HISTORY_RATE_RUNS:])
    return rates[len(rates) // 2] if rates else config.H3_DEFAULT_SEC_PER_SEC


def estimate(fmt: str, kind: str = "kids") -> float:
    """預估一支要多久（秒，含 15% 餘裕）：生成、視覺抽查與載入、企劃、音樂、關鍵幀、合成。"""
    if kind in DIALOGUE_KINDS:  # H3：以企劃最長時長 × 每秒成本估（腳本還沒寫，只能用上限）
        spec, renderer = content.SPECS[kind], content.renderer_for(kind)
        key = 0 if renderer == "h3_ref2va" else DIALOGUE_KEYFRAME_SEC * spec.shots[1]
        gen = (spec.total[1] + content.SLACK[1]) * h3_sec_per_video_sec(renderer)
        return (gen + key + DIALOGUE_OVERHEAD) * 1.15
    unique = FORMATS[fmt]["unique"] or 5
    key_sec = KEYFRAME_SEC[kind]
    return (unique * (SEC_PER_STEP * config.STEPS + key_sec) + 30 + (180 if fmt == "short" else 600)) * 1.15


def deadline_after(now: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    dl = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return dl if dl > now else dl + timedelta(days=1)


def _history_path():
    return config.WORK / "history.jsonl"


def _past_topics() -> list[str]:
    topics = []
    if _history_path().exists():
        topics += [
            t for x in _history_path().read_text().splitlines() if x and (t := json.loads(x).get("topic"))
        ]
    topics += [json.loads(p.read_text())["topic"] for p in config.WORK.glob("*/plan.json")]
    return list(dict.fromkeys(topics))


def _ask_topics(prompt: str, model: str) -> list[str]:
    """問 LLM 並解析出 {"topics": [...]}；網路錯誤維持 requests 的例外，格式不對丟 ValueError／KeyError／TypeError。"""
    r = requests.post(
        f"{config.LLM_URL}/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 1.0,
        },
        timeout=300,
    )
    r.raise_for_status()
    topics = json.loads(extract_json(r.json()["choices"][0]["message"]["content"]))["topics"]
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise ValueError("topics 必須是字串陣列")
    return topics


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
            raw = _ask_topics(prompt, config.KIDS_TOPIC_MODEL)
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
            raw = _ask_topics(prompt, config.TRIVIA_TOPIC_MODEL)
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


def suggest_dialogue_topics(kind: str, n: int, avoid: list[str]) -> list[str]:
    """對白類的主題（繁體中文短語）。提示詞在 content.topic_prompt；過濾規則與兒童音樂相同（禁用字詞、簡體字、重複）。"""
    prompt = content.topic_prompt(kind, n, avoid)
    for attempt in range(3):
        try:
            raw = _ask_topics(prompt, config.plan_model(kind))
        except (ValueError, KeyError, TypeError) as e:
            log.warning("%s 主題第 %d 次解析失敗：%s", kind, attempt + 1, e)
            continue
        good = [t.strip() for t in dict.fromkeys(raw) if isinstance(t, str) and 4 <= len(t.strip()) <= 16]
        good = [t for t in good if t not in avoid and not BANNED.search(t) and not SIMPLIFIED.search(t)]
        if len(good) >= n:
            return good[:n]
        log.warning("%s 主題第 %d 次只有 %d 個可用（需要 %d）", kind, attempt + 1, len(good), n)
    raise RuntimeError(f"LLM 3 次都想不出足夠的 {kind} 主題")


def _suggest(kind: str, n: int, avoid: list[str]) -> list[str]:
    if kind == "kids":
        return suggest_topics(n, avoid)
    if kind == "trivia":
        return suggest_trivia_topics(n, avoid)
    return suggest_dialogue_topics(kind, n, avoid)


def _append_history(entry: dict) -> None:
    config.WORK.mkdir(parents=True, exist_ok=True)
    with _history_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def nightly(plan=None, deadline="07:00", dry_run=False, sleep=time.sleep, kind="kids") -> list[dict]:
    plan = plan or PLANS[kind]
    if bad := [f for f in plan if f not in KINDS[kind]]:  # 確定會失敗的別拿去重試、浪費 10 分鐘與主題
        raise ValueError(f"{kind} 不支援片型 {bad}（可用 {KINDS[kind]}）")
    renderer = content.renderer_for(kind)
    why = ""
    if not renderers.enabled(renderer):
        why = f"renderer {renderer} 未啟用"
    elif missing := renderers.missing_weights(renderer):  # 權重還沒下載完：別白白想主題、載模型
        why = f"renderer {renderer} 的模型尚未安裝：{', '.join(missing)}"
    if why:  # 不想主題、不佔 GPU，記一筆 skipped 讓早上看得到原因
        log.warning("[%s] %s，今晚略過（見 VMK_H3_ENABLED／LICENSES.md）", kind, why)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        results = [
            {"date": now, "topic": "", "format": f, "kind": kind, "content_type": kind, "renderer": renderer,
             "status": "skipped", "reason": why}
            for f in plan
        ]  # fmt: skip
        if not dry_run:
            for r in results:
                _append_history(r)
        return results
    dl = deadline_after(datetime.now().astimezone(), deadline)
    topics = _suggest(kind, len(plan), _past_topics())
    log.info("今晚主題：%s；deadline %s", topics, dl.strftime("%m-%d %H:%M"))
    results = []
    for fmt, topic in zip(plan, topics, strict=True):
        entry = {
            "date": datetime.now().astimezone().isoformat(timespec="seconds"),
            "topic": topic,
            "format": fmt,
            "kind": kind,  # 每日合併靠它把各種內容分開
            "content_type": kind,
            "renderer": renderer,
        }
        if not dry_run:
            wait_cool(dl - timedelta(seconds=estimate(fmt, kind)), sleep)  # 每支開跑前先等 GPU 降溫
        if datetime.now().astimezone() + timedelta(seconds=estimate(fmt, kind)) > dl:
            entry["status"] = "skipped"  # 預估來不來得及：不硬跑，避免拖到白天占用 GPU
            log.warning("[%s] %s 預估 %.0f 分鐘，趕不上 deadline，略過", fmt, topic, estimate(fmt, kind) / 60)
        elif dry_run:
            entry["status"] = "dry-run"
        else:
            entry |= _run_with_retry(topic, fmt, dl, sleep, kind)
        results.append(entry)
        if not dry_run:
            _append_history(entry)
    return results


def kind_for(now: datetime) -> str | None:
    """星期 → 今晚的內容種類；週末是 None（不跑）。"""
    return SCHEDULE.get(now.weekday())


def merge_after(kind: str) -> list[dict]:
    """今晚生成結束後，合併同一種內容（ffmpeg，不用 GPU）。結果逐筆寫進 history；
    任何例外都只記錄，不能讓整晚被標成 failed。"""
    try:
        records = weekly.merge_kind(kind)
    except Exception as e:  # noqa: BLE001 合併是附加工作，失敗不影響已產生的成片
        log.error("[%s] 合併失敗（不影響今晚的成片）：%r", kind, e)
        records = [{"type": "merge", "date": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "topic": "", "format": "", "kind": kind, "content_type": kind, "status": "failed", "error": repr(e)}]  # fmt: skip
    for r in records:
        _append_history(r)
    return records


def tonight(
    kind: str = "auto", plan=None, deadline="07:00", dry_run=False, sleep=time.sleep, now=None
) -> list[dict]:
    """排程入口：決定今晚的內容種類（auto = 依星期）、生成、再合併。回傳生成結果加合併結果。"""
    now = now or datetime.now().astimezone()
    if kind == "auto":
        kind = kind_for(now)
        if kind is None:
            log.info("週末沒有排程")
            return []
    results = nightly(plan, deadline, dry_run, sleep, kind)
    return results if dry_run else results + merge_after(kind)


def _stats(job) -> dict:
    """成片之後的 history 欄位：輸出檔、QC 結果、GPU 生成秒數；H3 另記模式、鏡頭數與每秒成本（之後估算耗時用）。
    讀不到的欄位就略過，不能因為統計失敗而把一支好片記成失敗。"""

    def load(name):
        try:
            return json.loads(job.p(name).read_text())
        except (OSError, ValueError):
            return {}

    meta, st = load("meta.json"), load("status.json")
    out: dict = {}
    if job.p("final.mp4").exists():
        out["output"] = str(job.p("final.mp4"))
    if st:
        out["qc"] = {
            "status": st.get("status"),
            "checks": st.get("checks"),
            "visual": st.get("visual_qc", {}).get("status"),
        }
    t = meta.get("timings", {})
    out["gpu_seconds"] = round(sum(t.get(k, 0) for k in ("music", "keyframes", "clips")), 1)
    out["models"] = meta.get("models")
    if str(meta.get("renderer", "")).startswith("h3"):
        shots = list(meta.get("shots", {}).values())
        secs = round(sum(s.get("seconds_generated", 0) for s in shots), 3)
        gen = round(sum(s.get("clip_sec", 0) for s in shots), 1)
        out |= {
            "h3_mode": meta["renderer"].removeprefix("h3_"),
            "shot_count": len(shots),
            "seconds_generated": secs,
            "generation_seconds": gen,
            "gen_sec_per_video_sec": round(gen / secs, 2) if secs else None,
        }
    return out


def _run_with_retry(topic: str, fmt: str, dl: datetime, sleep, kind: str = "kids") -> dict:
    started = datetime.now().astimezone()
    done = {"start": started.isoformat(timespec="seconds")}

    def finish(entry: dict, retries: int) -> dict:
        end = datetime.now().astimezone()
        return done | entry | {"end": end.isoformat(timespec="seconds"),
                               "elapsed": round((end - started).total_seconds(), 1), "retry_count": retries}  # fmt: skip

    err = ""
    for attempt in range(2):
        try:
            job = pipeline.run(topic, fmt=fmt, resume=attempt > 0, kind=kind)
            status = json.loads(job.p("status.json").read_text())["status"]
            return finish({"status": status, "job": job.dir.name} | _stats(job), attempt)
        except Exception as e:  # noqa: BLE001 夜間排程不能被單支失敗拖垮：記錄後換下一支
            err = repr(e)
            log.error("[%s] %s 第 %d 次失敗：%s", fmt, topic, attempt + 1, err)
            if attempt == 0:
                if datetime.now().astimezone() + timedelta(seconds=RETRY_WAIT + estimate(fmt, kind)) > dl:
                    break
                sleep(RETRY_WAIT)
                wait_cool(dl - timedelta(seconds=estimate(fmt, kind)), sleep)  # 重試也算一次開跑
                if datetime.now().astimezone() + timedelta(seconds=estimate(fmt, kind)) > dl:
                    break  # 降溫也會耗時，真正重試前必須再守一次截止時間
    return finish({"status": "failed", "error": err}, attempt)
