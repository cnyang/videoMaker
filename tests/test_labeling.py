"""視覺 QC 校準用的標記清單（labeling.py）：抽樣規則與盲標頁面；不碰 GPU，ffmpeg 用假的。"""

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import pytest

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


@pytest.fixture
def compare():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "compare_nemotron", config.ROOT / "scripts/compare_nemotron.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _batch(out, n):
    (out / "frames").mkdir(exist_ok=True)
    manifest = [
        {"id": f"{i:03d}.jpg", "scores": {"child_safety": 0.0, "visual_defect": 0.0}} for i in range(n)
    ]
    for row in manifest:
        (out / "frames" / row["id"]).write_bytes(b"jpeg")
    (out / "manifest.json").write_text(json.dumps(manifest))
    return manifest


class _Reply:
    def raise_for_status(self):
        pass

    def json(self):
        return {
            "choices": [{"logprobs": {"content": [{"top_logprobs": [{"token": "Yes", "logprob": 0.0}]}]}}]
        }


def test_auc_counts_ties_as_half(compare):
    assert compare.auc([0.9, 0.8], [0.1, 0.2]) == 1
    assert compare.auc([0.1], [0.9]) == 0
    assert compare.auc([0.5], [0.5, 0.5]) == 0.5


def test_yes_prob_merges_token_variants_and_rejects_other_answers(compare):
    top = [
        {"token": " Yes", "logprob": math.log(0.6)},
        {"token": "yes", "logprob": math.log(0.2)},
        {"token": "No", "logprob": math.log(0.2)},
    ]
    assert compare.yes_prob(top) == pytest.approx(0.8)
    with pytest.raises(ValueError, match="Yes／No"):
        compare.yes_prob([{"token": "<think>", "logprob": 0.0}])  # 格式不對不能硬算分數


def test_score_all_resumes_after_interruption_and_sends_the_frame(tmp_path, monkeypatch, compare):
    _batch(tmp_path, 2)
    asked = []
    per_frame = len(visual_qc.QUESTIONS)

    def post(url, json, timeout):
        if len(asked) == per_frame:
            raise compare.requests.ConnectionError("中斷")
        asked.append(json)
        return _Reply()

    monkeypatch.setattr(compare.requests, "post", post)
    with pytest.raises(compare.requests.ConnectionError):
        compare.score_all(tmp_path, "http://x", "m")
    assert list(json.loads((tmp_path / "nemotron_scores.json").read_text())["scores"]) == ["000.jpg"]
    sent = asked[0]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False} and sent["logprobs"] is True
    content = sent["messages"][0]["content"]
    assert "Answer with only Yes or No." in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    asked.clear()
    monkeypatch.setattr(compare.requests, "post", lambda url, json, timeout: asked.append(json) or _Reply())
    scores = compare.score_all(tmp_path, "http://x", "m")
    assert len(asked) == per_frame and set(scores) == {"000.jpg", "001.jpg"}  # 只補沒評過的那一張


def test_score_all_discards_scores_of_a_different_batch(tmp_path, monkeypatch, compare):
    asked = []
    monkeypatch.setattr(compare.requests, "post", lambda url, json, timeout: asked.append(json) or _Reply())
    _batch(tmp_path, 1)
    compare.score_all(tmp_path, "http://x", "m")
    compare.score_all(tmp_path, "http://x", "m")
    assert len(asked) == len(visual_qc.QUESTIONS)  # 同一批：第二次不再呼叫
    _batch(tmp_path, 2)  # 新一批沿用 000.jpg 編號，舊分數不能套上去
    compare.score_all(tmp_path, "http://x", "m")
    assert len(asked) == 3 * len(visual_qc.QUESTIONS)


def test_compare_uses_highest_question_score_and_drops_unsure(tmp_path, compare):
    def row(i, **scores):
        return {"id": f"{i:03d}.jpg", "scores": {"child_safety": 0.0, "visual_defect": 0.0, **scores}}

    manifest = [
        row(0, visual_defect=0.4),
        row(
            1, visual_defect=0.05, unexpected_objects=0.9
        ),  # 照片模式專屬問題，Nemotron 沒問，不能算進 clef-flash
        row(2, child_safety=0.1),
        row(3, visual_defect=0.9),
    ]
    (tmp_path / "labels.csv").write_text(
        "id,label\n000.jpg,bad\n001.jpg,ok\n002.jpg,bad\n003.jpg,unsure\n004.jpg,\n"
    )
    labels = compare.load_labels(tmp_path / "labels.csv")
    assert labels == {"000.jpg": "bad", "001.jpg": "ok", "002.jpg": "bad"}
    nemotron = {
        "000.jpg": {"child_safety": 0.1, "visual_defect": 0.9},
        "001.jpg": {"child_safety": 0.2, "visual_defect": 0.0},
        "002.jpg": {"child_safety": 0.7, "visual_defect": 0.0},
        "003.jpg": {"child_safety": 0.99, "visual_defect": 0.0},
    }
    result = compare.compare(manifest, labels, nemotron)
    assert result["clef-flash"] == {
        "bad": 2, "ok": 1, "auc": 1.0,
        "caught@0.2": 1, "false_alarm@0.2": 0, "caught@0.5": 0, "false_alarm@0.5": 0,
    }  # fmt: skip
    assert result["nemotron"] == {
        "bad": 2, "ok": 1, "auc": 1.0,
        "caught@0.2": 2, "false_alarm@0.2": 1, "caught@0.5": 2, "false_alarm@0.5": 0,
    }  # fmt: skip


def test_main_without_labels_only_keeps_scores_and_with_labels_warns_on_few_bad(
    tmp_path, monkeypatch, caplog, compare
):
    manifest = _batch(tmp_path, 2)
    scores = {row["id"]: {"child_safety": 0.0, "visual_defect": 0.0} for row in manifest}
    monkeypatch.setattr(compare, "score_all", lambda *args: scores)
    assert compare.main([str(tmp_path)]) == 0
    assert not (tmp_path / "compare.json").exists()
    (tmp_path / "labels.csv").write_text("id,label\n000.jpg,bad\n001.jpg,ok\n")
    with caplog.at_level("WARNING", logger="videomaker"):
        assert compare.main([str(tmp_path)]) == 0
    assert json.loads((tmp_path / "compare.json").read_text())["nemotron"]["bad"] == 1
    assert "不可靠" in caplog.text
