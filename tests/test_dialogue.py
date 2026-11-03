"""對白類的階段（dialogue.py）、QC 與 pipeline 分派。用 ffmpeg 合成的假鏡頭實際串接；GPU／ComfyUI／LLM 一律 fake。"""

import json
import subprocess
from itertools import pairwise
from types import SimpleNamespace
from typing import ClassVar

import pytest

from tests.test_content import SCRIPTS, english
from videomaker import config, content, dialogue, pipeline, renderers, visual_qc


def _clip(path, secs, audio="sine", size="576x1024"):
    """H3 規格的假鏡頭：9:16、24fps、帶音訊。audio = sine（有聲）、silence（無聲）、none（沒有音軌）。"""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=24:d={secs}"]
    if audio != "none":
        src = (
            f"sine=frequency=440:sample_rate=48000:d={secs}"
            if audio == "sine"
            else "anullsrc=r=48000:cl=stereo"
        )
        cmd += ["-f", "lavfi", "-i", src]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (
        ["-c:a", "aac", "-shortest"] if audio != "none" else []
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([*cmd, str(path)], check=True)


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    font = tmp_path / "font.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(config, "FONT", str(font))
    # 本機 ffmpeg 沒有 drawtext（GX10 才有）：字幕濾鏡換成不做事的 null，其餘串接與音訊流程照實跑
    monkeypatch.setattr(pipeline, "drawtext_filter", lambda font, text, start, end: "null")
    return tmp_path


def make_job(work, kind="kids_english_dialogue", audio="sine", secs=None):
    script = content.SPECS[kind].schema.model_validate(SCRIPTS[kind]())
    plan = content.to_plan(script, "測試")
    d = work / "2026-10-22_job"
    d.mkdir()
    (d / "plan.json").write_text(plan.model_dump_json(indent=2))
    (d / "meta.json").write_text(
        json.dumps(
            {"topic": "測試", "format": "short", "kind": kind, "timings": {}, "models": {}, "shots": {}}
        )
    )
    job = pipeline.Job(d)
    for s, shot in zip(script.shots, job.plan.shots):
        _clip(d / "clips" / f"{shot.id:02d}.mp4", secs or s.duration, audio)
    return job


def _probe(path):
    out = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json", path],
            capture_output=True, text=True, check=True,
        ).stdout
    )  # fmt: skip
    return out


def test_subtitles_split_a_shot_by_word_count_inside_its_speaking_window(work):
    job = make_job(work, "animal_sitcom")
    job.plan.script["shots"][0]["dialogue"] = [
        {"speaker": "dog_01", "text": "One"},
        {"speaker": "dog_01", "text": "two three four"},
    ]
    subs = dialogue.subtitles(job, [6.0, 6.0, 7.0, 6.0])
    first = [s for s in subs if s["shot"] == 1]
    assert [s["text"] for s in first] == ["One", "two three four"]
    assert first[0]["start"] == pytest.approx(dialogue.LEAD) and first[-1]["end"] == pytest.approx(
        6 - dialogue.TAIL
    )
    assert first[0]["end"] == first[1]["start"] and (first[1]["end"] - first[1]["start"]) == pytest.approx(
        3 * (first[0]["end"] - first[0]["start"])
    )  # 1 詞 : 3 詞
    assert subs[2]["start"] >= 6 and all(a["end"] <= b["start"] + 1e-6 for a, b in pairwise(subs))


def test_mux_concats_clips_with_dialogue_audio_and_writes_timeline_and_subtitles(work):
    job = make_job(work)
    dialogue.mux(job)
    probe = _probe(job.p("final.mp4"))
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    assert float(probe["format"]["duration"]) == pytest.approx(21.0, abs=0.3)
    assert job.p("thumb.jpg").exists() and not job.p("audio_cat.wav").exists()
    timeline = json.loads(job.p("timeline.json").read_text())
    assert [t["shot"] for t in timeline] == [1, 2, 3, 4] and timeline[1]["start"] == pytest.approx(
        5.0, abs=0.1
    )
    assert len(json.loads(job.p("subtitles.json").read_text())) == 4
    assert job.windows()[1] == (
        2,
        0.0,
        pytest.approx(5.0, abs=0.1),
        pytest.approx(5.0, abs=0.1),
    )  # 視覺 QC 的區段
    assert job.meta["timings"]["mux"] >= 0
    before = job.p("final.mp4").stat().st_mtime
    dialogue.mux(job)  # 輸出已存在 → 跳過（可續跑）
    assert job.p("final.mp4").stat().st_mtime == before


def test_mux_rejects_a_clip_without_audio_before_encoding(work):
    job = make_job(work)
    _clip(job.p("clips", "03.mp4"), 5, audio="none")
    with pytest.raises(RuntimeError, match=r"缺少影片或音訊"):
        dialogue.mux(job)
    assert not job.p("final.mp4").exists()


def test_dialogue_checks_pass_for_a_good_video_and_catch_each_failure(work):
    job = make_job(work)
    dialogue.mux(job)
    final, dur = job.p("final.mp4"), 21.0
    assert dialogue.checks(job, final, dur) == {
        "no_long_silence": True,
        "subtitles_within_duration": True,
        "subtitles_match_dialogue": True,
    }
    subs = json.loads(job.p("subtitles.json").read_text())
    job.p("subtitles.json").write_text(json.dumps(subs[:-1] + [{**subs[-1], "end": dur + 2}]))
    assert dialogue.checks(job, final, dur)["subtitles_within_duration"] is False  # 字幕超出片長
    job.p("subtitles.json").write_text(json.dumps([{**subs[0], "text": "Something else"}, *subs[1:]]))
    assert dialogue.checks(job, final, dur)["subtitles_match_dialogue"] is False  # 字幕和腳本對白對不上
    job.p("subtitles.json").unlink()
    assert not any(
        dialogue.checks(job, final, dur)[k] for k in ("subtitles_within_duration", "subtitles_match_dialogue")
    )


def test_silent_video_fails_the_silence_check(work):
    job = make_job(work, audio="silence")
    dialogue.mux(job)
    assert dialogue.checks(job, job.p("final.mp4"), 21.0)["no_long_silence"] is False


def test_qc_sets_pending_review_with_dialogue_checks_and_fails_on_silence(work, monkeypatch):
    monkeypatch.setattr(visual_qc, "check", lambda j: {"status": "passed", "checked_frames": 0})
    good = make_job(work)
    dialogue.mux(good)
    pipeline.qc(good)
    st = json.loads(good.p("status.json").read_text())
    assert st["status"] == "pending_review"
    assert {"no_long_silence", "subtitles_within_duration", "subtitles_match_dialogue", "has_audio"} <= set(
        st["checks"]
    )
    assert any(
        k.startswith("duration_18") for k in st["checks"]
    )  # 對白類的長度範圍跟著企劃總長走，不是 15–20.5 秒
    # QC 沒過一律 failed，不會進待審核
    (work / "2026-10-22_job").rename(work / "good")
    bad = make_job(work, audio="silence")
    dialogue.mux(bad)
    pipeline.qc(bad)
    assert json.loads(bad.p("status.json").read_text())["status"] == "failed"


def test_qc_duration_range_follows_the_script_total_for_dialogue_kinds():
    assert content.qc_range("kids_english_dialogue", "short") == (18, 45)
    assert content.qc_range("animal_sitcom", "short") == (18, 50)
    assert content.qc_range("dialogue_animation", "short") == (28, 65)
    assert content.qc_range("kids", "short") == (15, 20.5)  # 兒童音樂不變
    assert content.qc_range("trivia", "long") == (150, 179)


# ---------- 階段：clips 與 pipeline 分派 ----------
class FakeRenderer:
    name, keyframes = "h3_fl2va", False

    def __init__(self, missing=None):
        self.rendered, self.missing = [], missing or []

    def missing_refs(self, job):
        return self.missing

    def render(self, job, c, shot):
        self.rendered.append(shot.id)
        job.p("clips", f"{shot.id:02d}.mp4").parent.mkdir(exist_ok=True)
        job.p("clips", f"{shot.id:02d}.mp4").write_bytes(b"clip")
        return {"clip_sec": 12.5, "seconds_generated": 5.167, "renderer": self.name}


class FakeComfyCls:
    instances: ClassVar[list] = []  # 測試中每次建立 ComfyUI client 都記在這裡

    def __init__(self):
        self.calls = []
        FakeComfyCls.instances.append(self)

    def prepare(self, extra_gb=0):
        self.calls.append(("prepare", extra_gb))

    def free(self):
        self.calls.append(("free",))


def _clips_job(work):
    script = content.SPECS["animal_sitcom"].schema.model_validate(SCRIPTS["animal_sitcom"]())
    d = work / "2026-10-22_job"
    d.mkdir()
    (d / "plan.json").write_text(content.to_plan(script, "測試").model_dump_json())
    (d / "meta.json").write_text(
        json.dumps(
            {
                "format": "short",
                "kind": "animal_sitcom",
                "renderer": "h3_fl2va",
                "timings": {},
                "models": {},
                "shots": {},
            }
        )
    )
    return pipeline.Job(d)


def test_clips_use_h3_memory_guard_record_generation_time_and_skip_finished_shots(work, monkeypatch):
    job = _clips_job(work)
    fake, FakeComfyCls.instances = FakeRenderer(), []
    monkeypatch.setattr(renderers, "RENDERERS", {"h3_fl2va": fake})
    monkeypatch.setattr(config, "H3_ENABLED", True)
    monkeypatch.setattr(dialogue, "Comfy", FakeComfyCls)
    job.p("clips").mkdir()
    job.p("clips", "02.mp4").write_bytes(b"done")  # 已完成的鏡頭不重做（續跑）
    dialogue.clips(job)
    assert fake.rendered == [1, 3, 4]
    c = FakeComfyCls.instances[0]
    assert c.calls == [
        ("prepare", config.H3_EXTRA_GB),
        ("free",),
    ]  # 先卸載＋檢查記憶體（H3 門檻），結束再卸載
    meta = json.loads(job.p("meta.json").read_text())
    assert meta["shots"]["1"]["clip_sec"] == 12.5 and meta["timings"]["clips"] >= 0
    FakeComfyCls.instances = []
    dialogue.clips(job)  # 全部完成 → 不再碰 ComfyUI
    assert FakeComfyCls.instances == []


def test_clips_refuse_when_h3_is_disabled_or_references_are_missing(work, monkeypatch):
    job = _clips_job(work)
    monkeypatch.setattr(dialogue, "Comfy", lambda: pytest.fail("不該啟動 ComfyUI"))
    monkeypatch.setattr(config, "H3_ENABLED", False)
    with pytest.raises(renderers.RendererDisabled):
        dialogue.clips(job)
    monkeypatch.setattr(config, "H3_ENABLED", True)
    monkeypatch.setattr(renderers, "RENDERERS", {"h3_fl2va": FakeRenderer(missing=["dog_01/front"])})
    with pytest.raises(RuntimeError, match="characters build"):
        dialogue.clips(job)


def test_fl2va_keyframes_reuse_the_existing_stage_and_ref2va_skips_it(work, monkeypatch):
    job, called = _clips_job(work), []
    monkeypatch.setattr(pipeline, "keyframes", lambda j: called.append("keyframes"))
    monkeypatch.setattr(renderers, "RENDERERS", {"h3_fl2va": SimpleNamespace(keyframes=True)})
    dialogue.keyframes(job)
    monkeypatch.setattr(renderers, "RENDERERS", {"h3_fl2va": SimpleNamespace(keyframes=False)})
    dialogue.keyframes(job)
    assert called == ["keyframes"] and dialogue.music(job) is None


def test_run_dispatches_dialogue_kinds_to_their_own_stages_and_music_kinds_do_not(work, monkeypatch):
    _clips_job(work)
    seen = []
    monkeypatch.setattr(config, "H3_ENABLED", True)
    monkeypatch.setattr(
        dialogue, "FNS", {s: (lambda j, s=s: seen.append(("dialogue", s))) for s in pipeline.STAGES[1:]}
    )
    monkeypatch.setattr(
        pipeline, "FNS", {s: (lambda j, s=s: seen.append(("music-kind", s))) for s in pipeline.STAGES[1:]}
    )
    pipeline.run("測試", resume=True, kind="animal_sitcom")
    assert seen == [("dialogue", s) for s in pipeline.STAGES[1:]]


def test_run_stops_before_planning_when_the_renderer_is_disabled_but_dry_run_still_plans(work, monkeypatch):
    monkeypatch.setattr(config, "H3_ENABLED", False)
    monkeypatch.setattr(pipeline, "new_job", lambda *a, **k: pytest.fail("不該開始企劃"))
    with pytest.raises(renderers.RendererDisabled):
        pipeline.run("測試", kind="animal_sitcom")
    job = _clips_job(work)
    monkeypatch.setattr(pipeline, "new_job", lambda *a, **k: job)
    assert (
        pipeline.run("測試", dry_run=True, kind="animal_sitcom").dir == job.dir
    )  # --dry-run 只產 plan.json，不需要 H3


def test_new_job_for_dialogue_kinds_writes_script_plan_and_renderer_without_music_fields(work, monkeypatch):
    script = content.SPECS["kids_english_dialogue"].schema.model_validate(english())
    monkeypatch.setattr(content, "make_script", lambda kind, topic: content.to_plan(script, topic))
    job = pipeline.new_job("想吃什麼", fmt="short", kind="kids_english_dialogue")
    assert job.dir.name.startswith(f"{pipeline.datetime.now():%Y-%m-%d}_what-to-eat")
    assert job.meta["kind"] == "kids_english_dialogue" and job.meta["renderer"] == "h3_fl2va"
    assert "bpm_set" not in job.meta and job.plan.script["learning_goal"].startswith("ask")
    for bad in ({"fmt": "long"}, {"images": [work]}):
        with pytest.raises(ValueError, match="只支援片型"):
            pipeline.new_job("想吃什麼", kind="kids_english_dialogue", **bad)


def test_new_job_passes_the_outline_to_the_script_writer_and_records_it_in_meta(work, monkeypatch):
    script = content.SPECS["animal_sitcom"].schema.model_validate(SCRIPTS["animal_sitcom"]())
    seen = {}

    def fake_make_script(kind, topic, outline=None, strict=False):
        seen.update(outline=outline, strict=strict)
        return content.to_plan(script, topic)

    monkeypatch.setattr(content, "make_script", fake_make_script)
    job = pipeline.new_job("餅乾", kind="animal_sitcom", outline="  大綱內容\n")
    assert (
        seen == {"outline": "大綱內容", "strict": False} and job.meta["outline"] == "大綱內容"
    )  # 去頭尾空白；原文留作來源證據
    assert "outline_strict" not in job.meta
    job = pipeline.new_job("餅乾", kind="animal_sitcom", outline="完整劇情", strict=True)
    assert seen["strict"] is True and job.meta["outline_strict"] is True


def test_new_job_without_outline_leaves_meta_and_the_script_call_unchanged(work, monkeypatch):
    script = content.SPECS["kids_english_dialogue"].schema.model_validate(english())
    monkeypatch.setattr(content, "make_script", lambda kind, topic: content.to_plan(script, topic))
    assert "outline" not in pipeline.new_job("想吃什麼", kind="kids_english_dialogue").meta


@pytest.mark.parametrize("kind", ["kids", "trivia"])
def test_outline_is_only_for_dialogue_kinds(work, kind):
    with pytest.raises(ValueError, match="對白類"):
        pipeline.new_job("x", kind=kind, outline="大綱")


def _cli(monkeypatch, tmp_path, argv, *, existing=False):
    """把 CLI 的 new_job／run 換成記錄器，回傳呼叫紀錄。"""
    from videomaker import __main__ as cli

    calls = []
    job = SimpleNamespace(dir=tmp_path)
    monkeypatch.setattr(cli.pipeline, "new_job", lambda *a, **k: calls.append(("new_job", a, k)) or job)
    monkeypatch.setattr(cli.pipeline, "run", lambda *a, **k: calls.append(("run", a, k)) or job)
    monkeypatch.setattr(cli.pipeline, "find_job", lambda *a: job if existing else None)
    monkeypatch.setattr(cli.renderers, "require", lambda name: calls.append(("require", name)))
    cli.main(argv)
    return calls


def test_cli_outline_creates_the_job_then_resumes_it_without_changing_run_signature(tmp_path, monkeypatch):
    f = tmp_path / "outline.md"
    f.write_text("  Meatball 偷餅乾。\n", encoding="utf-8")
    calls = _cli(monkeypatch, tmp_path, ["make", "餅乾", "--kind", "animal_sitcom", "--outline", str(f)])
    assert [c[0] for c in calls] == ["require", "new_job", "run"]  # 先擋 renderer，才花 LLM 時間
    assert calls[1][2]["outline"] == "Meatball 偷餅乾。"
    assert calls[2][1][3] is True  # run(resume=True) 接手剛建立的作業


def test_cli_outline_dry_run_skips_the_renderer_check(tmp_path, monkeypatch):
    f = tmp_path / "outline.md"
    f.write_text("大綱", encoding="utf-8")
    argv = ["make", "餅乾", "--kind", "animal_sitcom", "--outline", str(f), "--dry-run"]
    assert [c[0] for c in _cli(monkeypatch, tmp_path, argv)] == ["new_job", "run"]


@pytest.mark.parametrize("extra", [["--resume"], []])
def test_cli_outline_refuses_resume_and_empty_files(tmp_path, monkeypatch, extra):
    f = tmp_path / "outline.md"
    f.write_text("" if not extra else "大綱", encoding="utf-8")
    argv = ["make", "餅乾", "--kind", "animal_sitcom", "--outline", str(f), *extra]
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, argv)


def test_cli_story_is_the_strict_variant_and_excludes_outline(tmp_path, monkeypatch):
    f = tmp_path / "story.md"
    f.write_text("完整劇情", encoding="utf-8")
    calls = _cli(monkeypatch, tmp_path, ["make", "餅乾", "--kind", "animal_sitcom", "--story", str(f)])
    assert calls[1][2] == {"outline": "完整劇情", "strict": True}
    argv = ["make", "餅乾", "--kind", "animal_sitcom", "--story", str(f), "--outline", str(f)]
    with pytest.raises(SystemExit):
        _cli(monkeypatch, tmp_path, argv)


def test_cli_without_outline_still_calls_run_exactly_as_before(tmp_path, monkeypatch):
    calls = _cli(monkeypatch, tmp_path, ["make", "餅乾", "--kind", "animal_sitcom", "--dry-run"])
    assert [c[0] for c in calls] == ["run"]


def test_silence_inside_a_shot_without_dialogue_is_allowed_but_not_inside_a_speaking_shot(work, monkeypatch):
    job = make_job(work, "animal_sitcom")
    job.plan.script["shots"][1]["dialogue"] = []  # 第 2 鏡是反應鏡頭，沒有話
    _clip(job.p("clips", "02.mp4"), 6, audio="silence")
    dialogue.mux(job)
    final = job.p("final.mp4")
    assert dialogue.checks(job, final, 25.0)["no_long_silence"] is True  # 第 2 鏡（6–12 秒）的無聲不算
    _clip(job.p("clips", "01.mp4"), 6, audio="silence")  # 有對白的第 1 鏡無聲 → 不通過
    job.p("final.mp4").unlink()
    job.p("thumb.jpg").unlink()
    dialogue.mux(job)
    assert dialogue.checks(job, job.p("final.mp4"), 25.0)["no_long_silence"] is False
