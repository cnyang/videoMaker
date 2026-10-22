"""A/B：兒童音樂關鍵幀「A：FLUX.3 每鏡全文」vs「B：FLUX.3 畫一張角色參考圖＋Qwen-Image-Edit 每鏡編輯」。

離線實驗，不改管線、不碰 work/ 內任何作業：用既有作業的 plan.json 各產一組關鍵幀，拼成對照圖，
並記錄每張秒數與記憶體，結果決定要不要把參考圖流程接進 pipeline.keyframes。輸出已存在就跳過（可續跑）。

用法（GX10；週一到週四 19:30 起不可跑，並持夜間鎖避免與排程重疊）：
  cd ~/code/videoMaker && flock -n /tmp/vmk-nightly.lock env VMK_COMFY_URL=http://127.0.0.1:8188 \\
    PYTHONPATH=. .venv/bin/python scripts/ab_ref.py work/<作業>/plan.json ab_out [--shots 14]
輸出：<out>/A/NN.png、<out>/B/NN.png、<out>/ref.png、sheet_A.png、sheet_B.png、report.json
"""

import argparse
import json
import logging
import math
import statistics
import time
from pathlib import Path

import requests

from videomaker import config
from videomaker.comfy import Comfy, fill
from videomaker.pipeline import FLUX3_EXTRA_GB, SIZE, _ff, _has_black_bars, _has_white_bars
from videomaker.plan import Plan, Shot

log = logging.getLogger("videomaker")


def edit_prompt(plan: Plan, shot: Shot) -> str:
    """Qwen-Image-Edit 吃的是編輯指令，不是完整描述：保留參考圖的角色，只換動作與場景。"""
    held = f", holding {plan.prop}" if plan.prop else ""
    return (
        f"Keep the same character as in the image: {plan.character}{held}. "
        f"New scene: {shot.prompt}. Art style: {plan.style}. No text, numbers or letters anywhere in the image."
    )


def ref_workflow(prompt: str, image: str, seed: int, w: int, h: int) -> dict:
    """單張參考圖版的 qwen_compose：拿掉 img2（空的 LoadImage 會讓 workflow 失敗），pos／neg 的 image2 一併移除。"""
    wf = fill("compose", prompt=prompt, image1=image, image2=image, seed=seed, width=w, height=h)
    del wf["img2"]
    for node in ("pos", "neg"):
        del wf[node]["inputs"]["image2"]
    return wf


def flux3(c: Comfy, prompt: str, seed: int, dest: Path, w: int, h: int) -> float:
    """與 pipeline.keyframes 相同的黑白邊重生規則（最多 3 次）；回傳秒數。"""
    t = time.monotonic()
    for attempt in range(3):
        wf = fill("keyframe_flux3", prompt=prompt, seed=seed + 1000 * attempt, width=w, height=h)
        c.run(wf, dest, timeout=900)
        if not (_has_black_bars(dest, w, h) or _has_white_bars(dest, w, h)):
            break
    return round(time.monotonic() - t, 1)


def ram_free_gb(c: Comfy) -> float:
    return requests.get(c.url + "/system_stats", timeout=60).json()["system"]["ram_free"] / 2**30


def sheet(src: Path, dest: Path, n: int):
    """把 n 張關鍵幀拼成一張對照圖（每列最多 7 張）。"""
    cols = min(7, n)
    _ff(
        *("-pattern_type", "glob", "-i", f"{src}/*.png"),
        *("-vf", f"scale=176:-1,tile={cols}x{math.ceil(n / cols)}", "-frames:v", 1, dest),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--shots", type=int, default=0, help="只跑前 N 鏡，0 = 全部")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    plan = Plan.model_validate_json(args.plan.read_text())
    if plan.kind != "kids" or not plan.style:
        raise SystemExit("只比較兒童音樂的 plan（kind=kids 且有 style）")
    shots = plan.shots[: args.shots or None]
    w, h = SIZE
    out = args.out
    for sub in ("A", "B"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    report_path = out / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {"A": {}, "B": {}, "mem": {}}
    ref = out / "ref.png"
    held = f", holding {plan.prop}" if plan.prop else ""

    c = Comfy()
    # 階段 1：FLUX.3（A 組＋參考圖）。FLUX.3 與 Qwen 不可同時佔著記憶體，所以兩階段之間一定 free
    c.prepare(extra_gb=FLUX3_EXTRA_GB)
    base, low = ram_free_gb(c), 1e9
    jobs = [(ref, "ref", None)] + [(out / "A" / f"{s.id:02d}.png", str(s.id), s) for s in shots]
    for dest, key, shot in jobs:
        if dest.exists():
            continue
        if shot is None:
            prompt = f"{plan.style}, {plan.character}{held}, full body, centered, facing the camera, simple soft pastel background"
            sec = flux3(c, prompt, plan.seed, dest, w, h)
        else:
            sec = flux3(c, plan.full_prompt(shot), plan.seed + shot.id, dest, w, h)
            report["A"][key] = {"sec": sec}
        low = min(low, ram_free_gb(c))  # 模型還在記憶體時取樣
        log.info("[A] %s %.1fs", key, sec)
    if low < 1e9:
        # ponytail: 只在每張完成後取樣，抓不到載入瞬間的尖峰；要精確峰值再開執行緒輪詢
        report["mem"]["flux3_used_gb"] = round(base - low, 1)
    c.free()

    # 階段 2：Qwen-Image-Edit（B 組）
    c.prepare()
    low = 1e9  # 基準沿用階段 1 開頭（還沒載入任何模型）：/free 是非同步的，這裡才取樣會把殘留釋放量算成負的
    name = c.upload(ref, f"ab_{out.name}_ref.png")
    for s in shots:
        dest = out / "B" / f"{s.id:02d}.png"
        if dest.exists():
            continue
        prompt = edit_prompt(plan, s)
        t = time.monotonic()
        c.run(ref_workflow(prompt, name, plan.seed + s.id, w, h), dest, timeout=1800)
        sec = round(time.monotonic() - t, 1)
        report["B"][str(s.id)] = {"sec": sec, "prompt": prompt}
        low = min(low, ram_free_gb(c))
        log.info("[B] %d %.1fs", s.id, sec)
    if low < 1e9:
        report["mem"]["qwen_used_gb"] = round(base - low, 1)
    c.free()

    for sub in ("A", "B"):
        sheet(out / sub, out / f"sheet_{sub}.png", len(shots))
        secs = [v["sec"] for v in report[sub].values()]
        if secs:
            log.info("%s 組 %d 張，中位數 %.1fs", sub, len(secs), statistics.median(secs))
    report["mem"]["min_free_gb_setting"] = config.MIN_FREE_GB
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    log.info("完成：%s（人工比對 sheet_A.png／sheet_B.png）", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
