"""用人工標記過的畫面，比較 Nemotron 與 clef-flash 的視覺 QC 準確度（離線，不改管線與 status.json）。

前提：GX10 上 Nemotron 已啟動（容器 vmk-nemotron-image，127.0.0.1:8000），且標記資料夾有
manifest.json、frames/ 與 labels.csv（見 docs/operations.md 的「校準門檻」）。
clef-flash 的分數已在 manifest.json，不必重跑；只有 Nemotron 需要逐張評分。

用法（GX10）：
  PYTHONPATH=. .venv/bin/python scripts/compare_nemotron.py ~/code/videoMaker/labels
"""

import argparse
import base64
import csv
import hashlib
import json
import logging
import math
from pathlib import Path

import requests

from videomaker import visual_qc

log = logging.getLogger("videomaker")
MODEL = "nemotron-3.5-super"
THRESHOLDS = (0.2, 0.5)  # clef-flash 現行門檻與 0.5；Nemotron 的 Yes 機率常落在兩端，兩個門檻並列看
MIN_BAD = 10  # 有問題的畫面少於這個數，AUC 不可靠


def yes_prob(top: list[dict]) -> float:
    """第一個輸出 token 的 Yes／No 機率換算成 P(Yes)；大小寫與前導空白的變體合併。"""
    mass = {"yes": 0.0, "no": 0.0}
    for entry in top:
        word = entry["token"].strip().lower()
        if word in mass:
            mass[word] += math.exp(entry["logprob"])
    total = sum(mass.values())
    if total < 0.5:  # 機率幾乎都不在 Yes／No 上＝回答格式不對，不能硬算一個分數
        raise ValueError(f"第一個 token 不是 Yes／No：{[e['token'] for e in top]}")
    return mass["yes"] / total


def ask(base: str, model: str, frame: Path, instructions: str) -> float:
    image = "data:image/jpeg;base64," + base64.b64encode(frame.read_bytes()).decode()
    text = f"{visual_qc.STATE} {instructions} Answer with only Yes or No."
    response = requests.post(
        f"{base}/v1/chat/completions",
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 1,
            "logprobs": True,
            "top_logprobs": 10,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=300,
    )
    response.raise_for_status()
    return yes_prob(response.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"])


def score_all(out: Path, base: str, model: str) -> dict[str, dict[str, float]]:
    """逐張評分並保存；已評分的畫面沿用，可中斷續跑。"""
    manifest_bytes = (out / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    path = out / "nemotron_scores.json"
    # 快取綁「這批畫面＋模型＋問題」；換一批時編號（000.jpg…）會重複，不能套到舊分數
    key = hashlib.sha256(
        manifest_bytes + model.encode() + json.dumps(visual_qc.QUESTIONS, sort_keys=True).encode()
    ).hexdigest()
    cache = json.loads(path.read_text()) if path.exists() else {}
    scores = cache["scores"] if cache.get("key") == key else {}
    for row in manifest:
        if row["id"] in scores:
            continue
        frame = out / "frames" / row["id"]
        scores[row["id"]] = {
            name: ask(base, model, frame, question["instructions"])
            for name, question in visual_qc.QUESTIONS.items()
        }
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"key": key, "scores": scores}, indent=2) + "\n")
        temp.replace(path)
        log.info("已評分 %d / %d", len(scores), len(manifest))
    return scores


def load_labels(path: Path) -> dict[str, str]:
    """只留 ok／bad；unsure 與空白不參與比較。"""
    with path.open(newline="") as stream:
        return {r["id"]: r["label"] for r in csv.DictReader(stream) if r["label"] in ("ok", "bad")}


def auc(bad: list[float], ok: list[float]) -> float:
    """有問題的畫面分數高於沒問題的機率（平手算一半）。"""
    return sum((b > o) + 0.5 * (b == o) for b in bad for o in ok) / (len(bad) * len(ok))


def summarize(scores: dict[str, float], labels: dict[str, str]) -> dict:
    bad = [scores[i] for i, v in labels.items() if v == "bad"]
    ok = [scores[i] for i, v in labels.items() if v == "ok"]
    result = {"bad": len(bad), "ok": len(ok), "auc": auc(bad, ok) if bad and ok else None}
    for t in THRESHOLDS:
        result[f"caught@{t}"] = sum(s >= t for s in bad)
        result[f"false_alarm@{t}"] = sum(s >= t for s in ok)
    return result


def compare(manifest: list[dict], labels: dict[str, str], nemotron: dict[str, dict[str, float]]) -> dict:
    """兩邊都取各問題的最高分（對應 visual_qc._status「任一項過門檻就標記」）；
    unexpected_objects 只有照片模式才問，Nemotron 沒問，所以兩邊都排除。"""
    ids = {row["id"] for row in manifest}
    labels = {i: v for i, v in labels.items() if i in ids}
    clef = {row["id"]: max(row["scores"][k] for k in visual_qc.QUESTIONS) for row in manifest}
    nem = {i: max(s.values()) for i, s in nemotron.items()}
    return {"clef-flash": summarize(clef, labels), "nemotron": summarize(nem, labels)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("labels_dir", type=Path)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = args.labels_dir
    nemotron = score_all(out, args.base, args.model)
    labels_path = out / "labels.csv"
    if not labels_path.exists():
        log.warning(
            "沒有 labels.csv：只保存 Nemotron 分數（%s），標完再執行一次即可比較",
            out / "nemotron_scores.json",
        )
        return 0
    result = compare(json.loads((out / "manifest.json").read_text()), load_labels(labels_path), nemotron)
    (out / "compare.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for name, summary in result.items():
        log.info("%-10s %s", name, summary)
    if result["nemotron"]["bad"] < MIN_BAD:
        log.warning("有問題的畫面只有 %d 張（< %d），AUC 不可靠", result["nemotron"]["bad"], MIN_BAD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
