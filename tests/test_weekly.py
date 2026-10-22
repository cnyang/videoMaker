"""週五合併（weekly.py）：用 ffmpeg 合成的假成片實際串接，不碰 GPU。"""

import json
import subprocess
from datetime import datetime, timedelta

import pytest

from videomaker import __main__ as cli
from videomaker import config, weekly
from videomaker.plan import Plan

FRI = datetime.fromisoformat("2026-10-16T19:30:00+08:00")


def _clip(path, secs, size="1080x1920"):
    """和管線 mux 同規格（1080x1920、24fps、h264 + aac 48k）的假成片，才能驗證 -c copy 串接。"""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=24:d={secs}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={secs}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-shortest", str(path)],
        check=True,
    )  # fmt: skip


def _job(work, name, fmt, topic, day, status="pending_review", final=True, secs=1):
    d = work / name
    d.mkdir()
    plan = {
        "topic": topic, "slug": name.split("_")[1], "style": "", "character": "",
        "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
        "shots": [{"id": 1, "prompt": "p"}],
        "youtube": {"title": f"{topic}｜Title", "description": "中文。\n\nEnglish words go here for the test now.", "tags": ["兒歌", "kids songs"]},
    }  # fmt: skip
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    (d / "meta.json").write_text(json.dumps({"topic": topic, "format": fmt}))
    (d / "status.json").write_text(
        json.dumps(
            {
                "status": status,
                "visual_qc": {
                    "status": "passed",
                    "checked_frames": 1,
                    "scores": {"child_safety": {"score": 0.1, "source": "clips/01.mp4", "time": 0}},
                },
            }
        )
    )
    if final:
        _clip(d / "final.mp4", secs)
    date = (FRI - timedelta(days=day)).isoformat(timespec="seconds")
    with (work / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": date, "topic": topic, "format": fmt, "status": status, "job": name}, ensure_ascii=False) + "\n")  # fmt: skip
    return d


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    return tmp_path


def _duration(p):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return float(out)


def test_merge_concats_this_weeks_same_format_in_order_and_writes_reviewable_job(work):
    _job(work, "2026-10-12_a", "short", "甲", 4)
    _job(work, "2026-10-13_b", "short", "乙", 3, secs=2)
    _job(work, "2026-10-13_x", "long", "別的片型", 3)  # 片型不同：不會混進 short
    out = weekly.merge("short", FRI)
    assert out == work / "2026-10-16_weekly-short"
    assert _duration(out / "final.mp4") == pytest.approx(3.0, abs=0.3)
    assert (out / "thumb.jpg").exists()
    st = json.loads((out / "status.json").read_text())
    assert st["status"] == "pending_review" and st["details"]["sources"] == ["2026-10-12_a", "2026-10-13_b"]
    meta = json.loads((out / "meta.json").read_text())
    assert meta["format"] == "weekly-short" and meta["sources"] == ["2026-10-12_a", "2026-10-13_b"]
    plan = Plan.model_validate_json((out / "plan.json").read_text())  # 上傳用的 plan 必須是合法的 Plan
    assert plan.slug == "weekly-short" and "甲" in plan.youtube.title and "乙" in plan.youtube.title
    assert "兒歌" in plan.youtube.tags and len(plan.youtube.title) <= 100


def test_merge_skips_rejected_failed_missing_old_and_too_few(work):
    _job(work, "2026-10-12_a", "xl", "甲", 4)
    _job(work, "2026-10-13_b", "xl", "乙", 3, status="rejected")  # 人工不通過
    _job(work, "2026-10-14_c", "xl", "丙", 2, status="failed")  # QC 失敗
    _job(work, "2026-10-15_d", "xl", "丁", 1, final=False)  # GX10 上的成片已被刪掉
    assert weekly.merge("xl", FRI) is None  # 只剩 1 支：串接沒有意義
    assert not (work / "2026-10-16_weekly-xl").exists()
    _job(work, "2026-09-01_old", "xl", "很久以前", 45)  # 超過 28 天的視窗
    _job(work, "2026-10-15_e", "xl", "戊", 1)
    out = weekly.merge("xl", FRI)
    assert json.loads((out / "status.json").read_text())["details"]["sources"] == [
        "2026-10-12_a",
        "2026-10-15_e",
    ]


def test_merge_is_idempotent_and_cleans_up_when_ffmpeg_fails(work, monkeypatch):
    _job(work, "2026-10-12_a", "long", "甲", 4)
    _job(work, "2026-10-13_b", "long", "乙", 3)
    first = weekly.merge("long", FRI)
    mtime = (first / "final.mp4").stat().st_mtime
    assert weekly.merge("long", FRI) == first and (first / "final.mp4").stat().st_mtime == mtime  # 不重做
    import shutil

    shutil.rmtree(first)
    monkeypatch.setattr(weekly.pipeline, "_ff", lambda *a: (_ for _ in ()).throw(RuntimeError("ffmpeg 失敗")))
    with pytest.raises(RuntimeError, match="ffmpeg"):
        weekly.merge("long", FRI)
    assert not first.exists()  # 失敗不留半成品，否則下次會被當成「已存在」而略過


def test_merge_marks_failed_when_duration_does_not_match_sources(work, monkeypatch):
    _job(work, "2026-10-12_a", "short", "甲", 4)
    _job(work, "2026-10-13_b", "short", "乙", 3)
    real = weekly._duration
    monkeypatch.setattr(weekly, "_duration", lambda p: real(p) + 5 if p.name == "final.mp4" and p.parent.name.endswith("weekly-short") else real(p))  # fmt: skip
    out = weekly.merge("short", FRI)
    assert json.loads((out / "status.json").read_text())["status"] == "failed"


def test_weekly_cli_runs_all_formats(work, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(weekly, "merge", lambda fmt, now=None, kind="kids": seen.append((kind, fmt)) or (work / f"w-{kind}-{fmt}" if fmt != "long" else None))  # fmt: skip
    cli.main(["weekly"])
    # 每種內容各自合併；小知識沒有 xl，對白類只有 short
    assert seen == [
        ("kids", "short"),
        ("kids", "long"),
        ("kids", "xl"),
        ("trivia", "short"),
        ("trivia", "long"),
        ("kids_english_dialogue", "short"),
        ("animal_sitcom", "short"),
        ("dialogue_animation", "short"),
    ]
    out = capsys.readouterr().out
    assert "w-kids-short" in out and "w-kids-xl" in out and "w-trivia-short" in out and "-long" not in out


def test_weekly_inherits_visual_scores_and_never_marks_unchecked_sources_passed(work):
    first = _job(work, "2026-10-12_a", "short", "甲", 4)
    second = _job(work, "2026-10-13_b", "short", "乙", 3)
    combined = weekly.visual_qc.combine([first, second])
    assert combined["status"] == "passed" and combined["checked_frames"] == 2
    assert combined["scores"]["child_safety"]["source"].startswith(first.name + "/")
    (second / "status.json").write_text(json.dumps({"status": "approved"}))
    out = weekly.merge("short", FRI)
    status = json.loads((out / "status.json").read_text())
    assert status["status"] == "failed" and status["visual_qc"]["status"] == "not_checked"


@pytest.mark.parametrize(
    "visual", [{"status": "weird"}, {"status": None}, {"status": "passed", "fps": 4, "scores": {}}]
)
def test_combine_never_passes_unknown_status_or_other_sampling_rate(work, visual):
    first = _job(work, "2026-10-12_a", "short", "甲", 4)
    second = _job(work, "2026-10-13_b", "short", "乙", 3)
    (second / "status.json").write_text(json.dumps({"status": "approved", "visual_qc": visual}))
    assert weekly.visual_qc.combine([first, second])["status"] != "passed"


def test_merge_cleans_up_when_visual_combine_fails(work, monkeypatch):
    _job(work, "2026-10-12_a", "short", "甲", 4)
    _job(work, "2026-10-13_b", "short", "乙", 3)

    def broken(dirs):
        raise KeyError("score")

    monkeypatch.setattr(weekly.visual_qc, "combine", broken)
    with pytest.raises(KeyError):
        weekly.merge("short", FRI)
    assert not (work / "2026-10-16_weekly-short").exists()  # 沒有 meta.json 的半成品會讓 jobs.inventory 壞掉


@pytest.mark.parametrize(
    "states,expected",
    [
        (["passed", "passed"], "passed"),
        (["passed", "flagged"], "flagged"),
        (["flagged", "not_checked"], "not_checked"),
        (["running", "flagged"], "running"),
        (["error", "running"], "error"),
    ],
)
def test_combine_status_priority(work, states, expected):
    dirs = []
    for i, state in enumerate(states):
        d = _job(work, f"2026-10-1{i}_x{i}", "short", f"題{i}", 4 - i)
        (d / "status.json").write_text(json.dumps({"status": "approved", "visual_qc": {"status": state}}))
        dirs.append(d)
    assert weekly.visual_qc.combine(dirs)["status"] == expected


def _hist(work, name, fmt, kind, day):
    date = (FRI - timedelta(days=day)).isoformat(timespec="seconds")
    entry = {"date": date, "topic": name, "format": fmt, "status": "pending_review", "job": name}
    if kind:
        entry["kind"] = kind  # 舊紀錄沒有 kind 欄位
    with (work / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _trivia_job(work, name, topic, day):
    d = _job(work, name, "short", topic, day)
    plan = json.loads((d / "plan.json").read_text())
    plan.update(
        kind="trivia",
        facts=[],
        sources=[
            {
                "title": topic,
                "url": f"https://en.wikipedia.org/wiki/{topic}?oldid=1",
                "license": "CC BY-SA 4.0",
            }
        ],
    )
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    return d


def test_sources_keep_kids_and_trivia_apart_and_old_history_counts_as_kids(work):
    # _job 已經寫了不帶 kind 的紀錄（舊格式）→ 兒童音樂；再加上有 kind 的紀錄
    kids = _job(work, "2026-10-12_a", "short", "甲", 4)
    _trivia_job(work, "2026-10-13_t", "Otter", 3)
    work_hist = work / "history.jsonl"
    lines = [json.loads(x) for x in work_hist.read_text().splitlines()]
    lines[-1]["kind"] = "trivia"  # 最後一行是小知識那支
    work_hist.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
    assert weekly.sources("short", FRI) == [kids]
    assert [d.name for d in weekly.sources("short", FRI, "trivia")] == ["2026-10-13_t"]


def test_merge_trivia_writes_separate_job_with_all_sources(work):
    for name, topic, day in (("2026-10-13_a", "Otter", 3), ("2026-10-15_b", "Giraffe", 1)):
        _trivia_job(work, name, topic, day)
    hist = [json.loads(x) for x in (work / "history.jsonl").read_text().splitlines()]
    (work / "history.jsonl").write_text("".join(json.dumps({**x, "kind": "trivia"}) + "\n" for x in hist))
    assert weekly.merge("short", FRI) is None  # 兒童音樂本週一支都沒有
    out = weekly.merge("short", FRI, "trivia")
    assert out == work / "2026-10-16_weekly-trivia-short"
    plan = json.loads((out / "plan.json").read_text())
    assert plan["kind"] == "trivia" and plan["facts"] == [] and len(plan["sources"]) == 2
    description = plan["youtube"]["description"]
    assert (
        "Otter?oldid=1" in description and "Giraffe?oldid=1" in description and "CC BY-SA 4.0" in description
    )
    meta = json.loads((out / "meta.json").read_text())
    assert meta["kind"] == "trivia" and meta["format"] == "weekly-trivia-short"
    Plan.model_validate(plan)  # 仍是合法 Plan，才能走原本的上傳流程


def test_combine_judges_sources_by_current_threshold_not_the_one_they_were_checked_with(work):
    first = _job(work, "2026-10-12_a", "short", "甲", 4)
    second = _job(work, "2026-10-13_b", "short", "乙", 3)
    entry = {"source": "clips/01.mp4", "time": 0}

    def source(status, score):
        summary = {
            "status": status,
            "threshold": 0.5,
            "fps": 2,
            "scores": {"visual_defect": {"score": score, **entry}},
        }
        (second / "status.json").write_text(json.dumps({"status": "approved", "visual_qc": summary}))
        return weekly.visual_qc.combine([first, second])["status"]

    assert source("passed", 0.3) == "flagged"  # 以舊的 50% 門檻通過，但按現行 20% 門檻要標有疑慮
    assert source("flagged", 0.1) == "passed"  # 分數低於現行門檻，不再算有疑慮


def test_combine_does_not_trust_old_threshold_results_without_scores(work):
    first = _job(work, "2026-10-12_a", "short", "甲", 4)
    second = _job(work, "2026-10-13_b", "short", "乙", 3)
    (second / "status.json").write_text(
        json.dumps({"status": "approved", "visual_qc": {"status": "passed", "threshold": 0.5}})
    )
    assert (
        weekly.visual_qc.combine([first, second])["status"] == "not_checked"
    )  # 沒有分數，無法依現行門檻重判


def test_combined_qc_times_include_preceding_videos_and_drop_unknown_offsets(tmp_path):
    dirs = []
    for i in range(2):
        d = tmp_path / str(i)
        d.mkdir()
        status = {
            "details": {"duration": 22.0},
            "visual_qc": {
                "status": "passed",
                "scores": {
                    "visual_defect": {
                        "score": 0.01 + i / 10,
                        "source": "clips/01.mp4",
                        "time": 0.5,
                        "final_time": 3.5,
                    }
                },
            },
        }
        (d / "status.json").write_text(json.dumps(status))
        dirs.append(d)
    result = weekly.visual_qc.combine(dirs)
    assert result["scores"]["visual_defect"]["final_time"] == 25.5
    status = json.loads((dirs[0] / "status.json").read_text())
    status.pop("details")
    (dirs[0] / "status.json").write_text(json.dumps(status))
    assert "final_time" not in weekly.visual_qc.combine(dirs)["scores"]["visual_defect"]


# ---------- 每日合併：身分去重、不混種類、解析度、視窗 ----------
def _kind_job(work, name, kind, day, fmt="short", size="1080x1920", status="pending_review"):
    """任一種內容的假作業（plan 帶 kind、history 有 kind），成片規格可指定。"""
    d = _job(work, name, fmt, f"{kind}-{name[-1]}", day, status=status, final=False)
    plan = json.loads((d / "plan.json").read_text())
    plan |= {"kind": kind, "slug": name.split("_")[1]}
    if kind not in ("kids", "trivia"):
        del plan["music"]
        plan["script"] = {"title": "x"}  # 對白類沒有 music、帶結構化腳本
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    _clip(d / "final.mp4", 1, size)
    hist = work / "history.jsonl"
    lines = [json.loads(x) for x in hist.read_text().splitlines()]
    lines[-1]["kind"] = kind
    hist.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
    return d


def test_dialogue_kinds_merge_only_this_weeks_pair_while_music_kinds_look_back_four_weeks(work):
    for n, day in [("2026-10-08_a", 8), ("2026-10-08_b", 8), ("2026-10-15_c", 1), ("2026-10-15_d", 1)]:
        _kind_job(work, n, "animal_sitcom", day)
        _kind_job(work, n.replace("2026", "2025"), "kids", day)
    (rec,) = weekly.merge_kind("animal_sitcom", FRI)
    assert rec["status"] == "merged" and sorted(rec["sources"]) == [
        "2026-10-15_c",
        "2026-10-15_d",
    ]  # 上週的不重疊
    kids = [r for r in weekly.merge_kind("kids", FRI) if r["format"] == "short"]
    assert len(kids) == 1 and len(kids[0]["sources"]) == 4  # 兒童音樂 28 天視窗：上週的也在


def test_merge_kind_skips_with_a_reason_when_fewer_than_two_sources(work):
    _kind_job(work, "2026-10-15_a", "animal_sitcom", 1)
    (rec,) = weekly.merge_kind("animal_sitcom", FRI)
    assert rec["status"] == "skipped" and "1 支" in rec["reason"] and rec["type"] == "merge"
    assert rec["kind"] == "animal_sitcom" and rec["format"] == "short" and "topic" in rec
    assert not list(work.glob("*_weekly-*"))


def test_merge_is_deterministic_and_never_repeats_for_the_same_source_set(work):
    a = _kind_job(work, "2026-10-12_a", "kids_english_dialogue", 4)
    b = _kind_job(work, "2026-10-13_b", "kids_english_dialogue", 3)
    (first,) = weekly.merge_kind("kids_english_dialogue", FRI)
    mid = weekly.merge_id("kids_english_dialogue", "short", [a.name, b.name])
    assert first["status"] == "merged" and first["merge_id"] == mid and first["qc"] == "pending_review"
    assert weekly.merge_id("kids_english_dialogue", "short", [b.name, a.name]) == mid  # 與順序無關
    out = work / first["job"]
    assert out.name == "2026-10-16_weekly-kids-english-dialogue-short"
    assert json.loads((out / "meta.json").read_text())["merge_id"] == mid
    mtime = (out / "final.mp4").stat().st_mtime
    # 隔天再跑：來源沒變 → 不重做
    (again,) = weekly.merge_kind("kids_english_dialogue", FRI + timedelta(days=1))
    assert again["status"] == "skipped" and "沒有變動" in again["reason"] and again["job"] == out.name
    assert (out / "final.mp4").stat().st_mtime == mtime and len(list(work.glob("*_weekly-*"))) == 1
    # 合輯被刪掉（例如上傳後清掉 GX10 存檔）：history 記過就不再重做同一份
    import shutil

    shutil.rmtree(out)
    with (work / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(first, ensure_ascii=False) + "\n")
    (gone,) = weekly.merge_kind("kids_english_dialogue", FRI + timedelta(days=2))
    assert gone["status"] == "skipped" and "history" in gone["reason"] and not list(work.glob("*_weekly-*"))
    # 多了新來源 → 新身分、產生新的合併
    _kind_job(work, "2026-10-17_c", "kids_english_dialogue", 0)
    (new,) = weekly.merge_kind("kids_english_dialogue", FRI + timedelta(days=2))
    assert new["status"] == "merged" and new["merge_id"] != mid and len(new["sources"]) == 3


def test_failed_merge_attempts_can_be_retried_and_never_raise_out_of_merge_kind(work, monkeypatch):
    _kind_job(work, "2026-10-12_a", "animal_sitcom", 4)
    _kind_job(work, "2026-10-13_b", "animal_sitcom", 3)
    monkeypatch.setattr(weekly.pipeline, "_ff", lambda *a: (_ for _ in ()).throw(RuntimeError("ffmpeg 失敗")))
    (rec,) = weekly.merge_kind("animal_sitcom", FRI)
    assert (
        rec["status"] == "failed" and "ffmpeg" in rec["error"] and not list(work.glob("*_weekly-*"))
    )  # 不留半成品
    monkeypatch.undo()
    monkeypatch.setattr(config, "WORK", work)
    (retry,) = weekly.merge_kind("animal_sitcom", FRI)
    assert retry["status"] == "merged"  # failed 不算「已合併過」，下次可重試


def test_merge_only_mixes_same_content_type_format_and_resolution(work):
    kids = _kind_job(work, "2026-10-12_a", "kids", 4)
    s1 = _kind_job(work, "2026-10-13_b", "animal_sitcom", 3)
    s2 = _kind_job(work, "2026-10-14_c", "animal_sitcom", 2)
    _kind_job(work, "2026-10-14_d", "dialogue_animation", 2)  # 別的種類
    _kind_job(work, "2026-10-14_e", "animal_sitcom", 2, fmt="long")  # 別的片型
    odd = _kind_job(work, "2026-10-15_f", "animal_sitcom", 1, size="540x960")  # 解析度不同，-c copy 不能混
    names = [d.name for d in weekly.sources("short", FRI, "animal_sitcom")]
    assert names == [s1.name, s2.name] and odd.name not in names and kids.name not in names
    assert [d.name for d in weekly.sources("short", FRI)] == [kids.name]


def test_merge_ignores_history_merge_records_and_old_or_excess_sources(work):
    dirs = [
        _kind_job(work, f"2026-09-{10 + i}_{chr(97 + i)}", "kids", 40 - i) for i in range(2)
    ]  # 40 天前：視窗外
    assert weekly.sources("short", FRI) == [] and dirs
    recent = [_kind_job(work, f"2026-10-{10 + i}_{chr(97 + i)}", "kids", 6 - i) for i in range(6)]
    got = weekly.sources("short", FRI)
    assert got == recent[-weekly.MAX_SOURCES :]  # 最多取最新的 MAX_SOURCES 支
    with (work / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "merge", "date": FRI.isoformat(), "topic": "", "format": "short", "kind": "kids", "job": "2026-10-16_weekly-short", "status": "merged"}) + "\n")  # fmt: skip
    assert weekly.sources("short", FRI) == got  # 合併紀錄不會被當成來源


def test_legacy_merge_without_merge_id_still_blocks_a_duplicate(work):
    a = _kind_job(work, "2026-10-12_a", "kids", 4)
    b = _kind_job(work, "2026-10-13_b", "kids", 3)
    old = work / "2026-10-14_weekly-short"
    old.mkdir()
    (old / "meta.json").write_text(
        json.dumps({"format": "weekly-short", "kind": "kids", "sources": [a.name, b.name]})
    )
    (rec,) = [r for r in weekly.merge_kind("kids", FRI) if r["format"] == "short"]
    assert rec["status"] == "skipped" and rec["job"] == old.name


def test_dialogue_merge_is_a_valid_reviewable_plan_with_its_own_youtube_copy(work):
    for name, day in (("2026-10-12_a", 4), ("2026-10-13_b", 3)):
        _kind_job(work, name, "dialogue_animation", day)
    (rec,) = weekly.merge_kind("dialogue_animation", FRI)
    out = work / rec["job"]
    plan = Plan.model_validate_json((out / "plan.json").read_text())
    assert plan.kind == "dialogue_animation" and plan.script == {} and "動畫小故事精選" in plan.youtube.title
    st = json.loads((out / "status.json").read_text())
    assert st["status"] == "pending_review" and "approved" not in st  # 合併後仍要人工核准
    assert json.loads((out / "meta.json").read_text())["kind"] == "dialogue_animation"


def test_a_corrupt_meta_json_does_not_break_merging(work):
    _kind_job(work, "2026-10-12_a", "animal_sitcom", 4)
    _kind_job(work, "2026-10-13_b", "animal_sitcom", 3)
    broken = work / "2026-10-14_broken"
    broken.mkdir()
    (broken / "meta.json").write_text("{")  # 別的行程寫到一半
    (rec,) = weekly.merge_kind("animal_sitcom", FRI)
    assert rec["status"] == "merged"
