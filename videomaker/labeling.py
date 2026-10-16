"""視覺 QC 校準：從既有結果抽出待人工標記的畫面，之後用標記結果決定門檻。

盲標：標記頁面只顯示畫面，不顯示分數與作業名稱，順序也打亂，避免標記的人被分數影響；分數只存在 manifest.json。
"""

import hashlib
import json
import logging
import random
import subprocess
from pathlib import Path

from . import config, visual_qc

log = logging.getLogger("videomaker")

_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>視覺 QC 標記</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #111; color: #eee; text-align: center; }
  .bar { padding: 10px; display: flex; gap: 8px; justify-content: center; align-items: center; flex-wrap: wrap; }
  button { font-size: 18px; padding: 10px 18px; border: 0; border-radius: 6px; cursor: pointer; color: #fff; }
  #ok { background: #2e7d32; } #bad { background: #c62828; } #unsure { background: #757575; } .plain { background: #333; }
  img { max-height: 78vh; max-width: 95vw; }
  .hint { color: #aaa; font-size: 14px; max-width: 700px; margin: 6px auto 20px; }
</style>
<div class="bar">
  <span id="pos"></span>
  <button id="ok" onclick="mark('ok')">1 沒問題</button>
  <button id="bad" onclick="mark('bad')">2 有問題</button>
  <button id="unsure" onclick="mark('unsure')">3 不確定</button>
  <button class="plain" onclick="go(i - 1)">← 上一張</button>
  <button class="plain" onclick="save()">匯出 labels.csv</button>
</div>
<img id="im">
<p class="hint">「有問題」＝ 明顯多餘或融合的肢體、五官壞掉、物體幾何不可能，或不適合 3～8 歲兒童的內容。
卡通誇張比例、大眼睛不算。畫面只看這一張，不要想它是哪支片。標記存在這個瀏覽器裡，關掉再開會接著標。</p>
<script>
const FILES = __FILES__, KEY = "__KEY__";
let labels = JSON.parse(localStorage.getItem(KEY) || "{}");
let i = Math.max(0, FILES.findIndex(f => !labels[f]));
function go(n) {
  i = Math.min(Math.max(n, 0), FILES.length - 1);
  document.getElementById("im").src = "frames/" + FILES[i];
  const done = FILES.filter(f => labels[f]).length;
  document.getElementById("pos").textContent = (i + 1) + " / " + FILES.length + "　已標 " + done + "　本張：" + (labels[FILES[i]] || "-");
}
function mark(v) { labels[FILES[i]] = v; localStorage.setItem(KEY, JSON.stringify(labels)); go(i + 1); }
function save() {
  const csv = "id,label\\n" + FILES.map(f => f + "," + (labels[f] || "")).join("\\n") + "\\n";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "labels.csv";
  a.click();
}
document.addEventListener("keydown", e => {
  if (e.key === "1") mark("ok"); else if (e.key === "2") mark("bad"); else if (e.key === "3") mark("unsure");
  else if (e.key === "ArrowLeft" || e.key === "Backspace") go(i - 1); else if (e.key === "ArrowRight") go(i + 1);
});
go(i);
</script>
"""


def collect() -> list[dict]:
    """work/ 底下所有作業已抽查過的畫面（作業、來源、時間、各項分數）。
    鏡頭檔與評分當時的 sha256（report["inputs"]）不同就略過：分數屬於舊鏡頭，不能配新畫面。"""
    rows = []
    for path in sorted(config.WORK.glob("*/visual_qc.json")):
        report = json.loads(path.read_text())
        if report.get("sampling") != visual_qc.SAMPLING:
            log.warning("略過舊抽樣時間不精確的 QC 結果：%s；需重查後再校準", path.parent.name)
            continue
        scored = dict(map(tuple, report.get("inputs", [])))
        same = {}
        for name, digest in scored.items():
            source = path.parent / name
            if source.exists():
                with source.open("rb") as stream:
                    same[name] = hashlib.file_digest(stream, "sha256").hexdigest() == digest
        for f in report.get("frames", []):
            if not same.get(f["source"]):
                continue
            rows.append(
                {
                    "job": path.parent.name,
                    "source": f["source"],
                    "time": f["time"],
                    "final_time": f.get("final_time"),
                    "scores": f["scores"],
                }
            )
    return rows


def select(rows: list[dict], top: int, rand: int, seed: int = 0) -> list[dict]:
    """分數最高的 top 張（看模型抓到的是不是真的），加上其餘裡隨機 rand 張（看低分裡有沒有漏掉的）；順序打亂。"""
    ranked = sorted(rows, key=lambda r: -max(r["scores"].values()))
    chosen = ranked[:top] + random.Random(seed).sample(ranked[top:], min(rand, len(ranked[top:])))
    random.Random(seed + 1).shuffle(chosen)  # 位置不能洩漏分數
    return chosen


def export(out: Path, top: int = 40, rand: int = 40) -> int:
    """抽樣、截圖並寫出標記頁面（index.html）與對照表（manifest.json）；回傳實際輸出的張數。"""
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    manifest = []
    for row in select(collect(), top, rand):
        source = config.WORK / row["job"] / row["source"]
        name = f"{len(manifest):03d}.jpg"
        try:
            if not source.exists():
                raise FileNotFoundError(source)
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", str(max(0, row["time"] - 1e-5)), "-i", str(source), "-frames:v", "1",
                 "-vf", visual_qc.SCALE, "-q:v", visual_qc.JPEG_Q, str(frames / name)],
                capture_output=True, check=True,
            )  # fmt: skip
        except (OSError, subprocess.CalledProcessError) as error:
            log.warning("略過 %s %s %ss：%s", row["job"], row["source"], row["time"], error)
            continue
        manifest.append({"id": name, **row})
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    # 標記存在瀏覽器 localStorage；key 綁這一批的內容，換一批時舊標記不會套到同編號的新畫面上
    key = "vmk-labels-" + hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:12]
    page = _PAGE.replace("__FILES__", json.dumps([m["id"] for m in manifest])).replace("__KEY__", key)
    (out / "index.html").write_text(page)
    return len(manifest)
