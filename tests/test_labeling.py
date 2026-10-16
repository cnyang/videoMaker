"""視覺 QC 校準用的標記清單（labeling.py）：抽樣規則與盲標頁面；不碰 GPU，ffmpeg 用假的。"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from videomaker import __main__ as cli
from videomaker import config, labeling, visual_qc


def _job(work, name, scores):
    d = work / name
    d.mkdir()
    frames = [
        {"source": "clips/01.mp4", "time": i / 2, "final_time": i / 2, "scores": {"child_safety": 0.01, "visual_defect": s}}
        for i, s in enumerate(scores)
    ]  # fmt: skip
    digest = hashlib.sha256(b"clip").hexdigest()
    report = {
        "frames": frames,
        "summary": {},
        "inputs": [["clips/01.mp4", digest]],
        "sampling": visual_qc.SAMPLING,
    }
    (d / "visual_qc.json").write_text(json.dumps(report))
    (d / "clips").mkdir()
    (d / "clips/01.mp4").write_bytes(b"clip")
    return d


def _fake_ffmpeg(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"jpeg")

    monkeypatch.setattr(labeling.subprocess, "run", run)
    return calls


def test_select_takes_highest_scores_plus_seeded_random_rest_in_shuffled_order():
    rows = [{"job": "j", "source": "s", "time": i, "scores": {"visual_defect": i / 100}} for i in range(100)]
    picked = labeling.select(rows, top=5, rand=5, seed=1)
    times = [r["time"] for r in picked]
    assert len(times) == 10 and {95, 96, 97, 98, 99} <= set(times)  # 最高分的 5 張一定入選
    assert len(set(times) - {95, 96, 97, 98, 99}) == 5 and max(set(times) - {95, 96, 97, 98, 99}) < 95
    assert times[:5] != [99, 98, 97, 96, 95]  # 順序打亂：高分的不能全排在前面，位置不能洩漏分數
    assert [r["time"] for r in labeling.select(rows, 5, 5, seed=1)] == times  # 同 seed 可重現
    assert len(labeling.select(rows[:3], top=5, rand=5)) == 3  # 不夠就全部


def test_export_writes_blind_labeling_page_and_a_manifest_with_the_scores(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(config, "WORK", work)
    _job(work, "2026-10-15_squirrel-jump", [0.05, 0.38, 0.07])
    _job(work, "2026-10-16_sea-otter", [0.06, 0.04])
    calls = _fake_ffmpeg(monkeypatch)
    out = tmp_path / "labels"
    assert labeling.export(out, top=2, rand=2) == 4
    manifest = json.loads((out / "manifest.json").read_text())
    assert [m["id"] for m in manifest] == ["000.jpg", "001.jpg", "002.jpg", "003.jpg"]
    assert all((out / "frames" / m["id"]).exists() for m in manifest)
    assert 0.38 in [m["scores"]["visual_defect"] for m in manifest]  # 分數只存在 manifest
    assert all("-ss" in c and "-frames:v" in c for c in calls)
    page = (out / "index.html").read_text()
    assert "000.jpg" in page and "labels.csv" in page
    # 盲標：頁面不能出現作業名稱、分數
    assert "squirrel" not in page and "sea-otter" not in page and not re.search(r"0\.38|0\.05", page)


def test_export_skips_frames_whose_source_clip_is_gone(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(config, "WORK", work)
    gone = _job(work, "2026-10-14_gone", [0.9])
    (gone / "clips/01.mp4").unlink()
    _job(work, "2026-10-15_ok", [0.1])
    _fake_ffmpeg(monkeypatch)
    assert labeling.export(tmp_path / "labels", top=5, rand=5) == 1


def test_export_survives_one_failing_ffmpeg_call(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(config, "WORK", work)
    _job(work, "2026-10-15_a", [0.3, 0.1])

    def run(args, **kwargs):
        if abs(float(args[args.index("-ss") + 1]) - 0.5) < 0.001:
            raise subprocess.CalledProcessError(1, args)
        Path(args[-1]).write_bytes(b"jpeg")

    monkeypatch.setattr(labeling.subprocess, "run", run)
    assert labeling.export(tmp_path / "labels", top=5, rand=5) == 1


def test_cli_visual_labels_passes_sizes(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(
        labeling, "export", lambda out, top, rand: seen.update(out=out, top=top, rand=rand) or 7
    )
    cli.main(["visual-labels", str(tmp_path / "o"), "--top", "10", "--random", "5"])
    assert seen == {"out": tmp_path / "o", "top": 10, "rand": 5} and "7" in capsys.readouterr().out


def test_collect_skips_frames_whose_clip_was_regenerated(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(config, "WORK", work)
    _job(work, "2026-10-15_a", [0.3])
    changed = _job(work, "2026-10-15_b", [0.4])
    (changed / "clips/01.mp4").write_bytes(b"regenerated")  # 分數屬於舊鏡頭，不能配新畫面
    assert [r["job"] for r in labeling.collect()] == ["2026-10-15_a"]


def test_each_export_stores_labels_under_its_own_key(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(config, "WORK", work)
    _job(work, "2026-10-15_a", [0.3, 0.1])
    _fake_ffmpeg(monkeypatch)

    def key(out):
        labeling.export(out, top=5, rand=5)
        return re.search(r'KEY = "(vmk-labels-[0-9a-f]+)"', (out / "index.html").read_text()).group(1)

    first = key(tmp_path / "one")
    assert key(tmp_path / "same") == first  # 同一批畫面：沿用標記
    _job(work, "2026-10-16_b", [0.5])
    assert key(tmp_path / "two") != first  # 換了一批：舊標記不能套到新編號上


def test_collect_rejects_legacy_scores_without_precise_sampling_times(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    d = _job(tmp_path, "2026-10-16_old", [0.3])
    path = d / "visual_qc.json"
    report = json.loads(path.read_text())
    report.pop("sampling")
    path.write_text(json.dumps(report))
    assert labeling.collect() == []


def test_real_extraction_and_labeling_use_identical_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    d = tmp_path / "2026-10-16_frames"
    (d / "clips").mkdir(parents=True)
    source = d / "clips/01.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x64:r=24:d=2",
            "-vf",
            "geq=lum='5*N':cb=128:cr=128",
            "-c:v",
            "libx264",
            "-crf",
            "0",
            str(source),
        ],
        check=True,
    )
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    frames = visual_qc._extract(source, extracted, 0)
    assert [t for t, _ in frames] == [0, 0.5, 1, 1.5]
    report = {
        "sampling": visual_qc.SAMPLING,
        "inputs": [["clips/01.mp4", hashlib.sha256(source.read_bytes()).hexdigest()]],
        "summary": {},
        "frames": [
            {"source": "clips/01.mp4", "time": t, "scores": {"visual_defect": 0.1}} for t, _ in frames
        ],
    }
    (d / "visual_qc.json").write_text(json.dumps(report))
    out = tmp_path / "labels"
    assert labeling.export(out, top=4, rand=0) == 4
    expected = {t: p.read_bytes() for t, p in frames}
    for row in json.loads((out / "manifest.json").read_text()):
        assert (out / "frames" / row["id"]).read_bytes() == expected[row["time"]]
