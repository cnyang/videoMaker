import itertools
import json
import subprocess

import numpy as np
import pytest
import requests
import soundfile as sf

from videomaker import audio, config, pipeline, visual_qc
from videomaker.comfy import fill, models_in
from videomaker.plan import Plan, shot_math

SR = 22050


def click_track(path, bpm, offset, secs):
    """合成節拍：每拍一個短 click，每 4 拍（重拍）加重音量與低頻。"""
    t_all = np.arange(int(secs * SR)) / SR
    y = (0.05 * np.sin(2 * np.pi * 220 * t_all) + 0.05 * np.sin(2 * np.pi * 330 * t_all)).astype(
        np.float32
    )  # 持續音墊，避免峰值因數過大
    period = 60 / bpm
    for i in range(int((secs - offset) / period)):
        s = int((offset + i * period) * SR)
        n = int(0.05 * SR)
        t = np.arange(n) / SR
        amp, f = (1.0, 200) if i % 4 == 0 else (0.35, 1000)
        y[s : s + n] += (amp * np.sin(2 * np.pi * f * t) * np.exp(-t * 60)).astype(np.float32)
    sf.write(path, y, SR)


@pytest.mark.parametrize("bpm", range(100, 141))
def test_shot_math_bounds(bpm):
    shot, n, total = shot_math(bpm)
    assert shot <= 5.0 and 15 <= total <= 20 and n >= 4


def test_shot_math_rejects_out_of_range():
    with pytest.raises(ValueError):
        shot_math(80)


def test_plan_rejects_banned_and_bad_bpm():
    ok = {
        "topic": "t",
        "slug": "bear-brush",
        "style": "flat 2D",
        "character": "a round brown bear cub with a red scarf",
        "shots": [{"id": 1, "prompt": "bear waves"}],
        "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
    }
    Plan.model_validate(ok)
    for bad in (
        {**ok, "style": "in the style of Pixar"},
        {**ok, "shots": [{"id": 1, "prompt": "Mickey runs"}]},
        {**ok, "music": {**ok["music"], "bpm": 90}},
        {**ok, "slug": "Bad Slug"},
    ):
        with pytest.raises(ValueError):
            Plan.model_validate(bad)


def test_plan_rejects_chinese_short_character_and_text_words():
    base = {
        "topic": "t",
        "slug": "bear-brush",
        "style": "flat 2D cartoon",
        "character": "a round brown bear cub with a red scarf",
        "shots": [{"id": 1, "prompt": "the bear waves"}],
        "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
    }
    Plan.model_validate(base)
    for bad in (
        {**base, "character": "小熊"},  # 中文 → FLUX 會畫出亂碼文字
        {**base, "character": "a bear"},  # 太短 → 無法跨鏡維持角色一致
        {**base, "style": "扁平卡通"},
        {**base, "shots": [{"id": 1, "prompt": "小熊 waves"}]},
        {**base, "shots": [{"id": 1, "prompt": "the bear holds a sign with letters"}]},
    ):
        with pytest.raises(ValueError):
            Plan.model_validate(bad)


def test_full_prompt_prefixes_style_and_character():
    p = Plan.model_validate(
        {
            "topic": "t",
            "slug": "a",
            "style": "S",
            "character": "a round brown bear cub with a red scarf",
            "shots": [{"id": 1, "prompt": "P"}],
            "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
        }
    )
    assert p.full_prompt(p.shots[0]) == "S, a round brown bear cub with a red scarf, P"


def test_fill_and_models():
    wf = fill(
        "music",
        tags="t",
        lyrics="[Instrumental]",
        seed=7,
        bpm=120,
        key="C major",
        timesignature="4",
        duration=22.0,
    )
    assert wf["enc"]["inputs"]["seed"] == wf["ks"]["inputs"]["seed"] == 7
    assert wf["latent"]["inputs"]["seconds"] == 22.0
    with pytest.raises(KeyError):
        fill("music", nope=1)
    assert models_in(fill("clip", prompt="p", image="i", seed=1, width=704, height=1280, length=121)) == [
        "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "wan2.2_ti2v_5B_fp16.safetensors",
        "wan2.2_vae.safetensors",
    ]


def test_workflow_node_refs_exist():
    """每個 workflow 的 [節點, 輸出] 參照都指到存在的節點。"""
    for spec in __import__("yaml").safe_load((config.ROOT / "workflow_map.yaml").read_text()).values():
        wf = json.loads((config.ROOT / spec["file"]).read_text())
        for node in wf.values():
            for v in node["inputs"].values():
                if isinstance(v, list):
                    assert v[0] in wf
        for targets in spec["fields"].values():
            for node, field in targets:
                assert field in wf[node]["inputs"]


def test_beats_offset_and_cuts(tmp_path):
    click_track(tmp_path / "m.wav", 120, offset=0.37, secs=22)
    info = audio.analyze(tmp_path / "m.wav", 120, 5)
    assert abs(info["bpm_measured"] - 120) < 3
    assert abs(info["t0"] - 0.37) < 0.03  # 音樂起點對齊第一個重拍（不是 0 秒）
    gaps = np.diff(info["cuts"])
    assert len(gaps) == 5 and all(abs(g - 4.0) < 0.1 for g in gaps)


def test_beats_too_short_raises(tmp_path):
    click_track(tmp_path / "m.wav", 120, offset=0.0, secs=12)
    with pytest.raises(ValueError):
        audio.analyze(tmp_path / "m.wav", 120, 5)


def test_beats_music_ends_before_last_cut_raises(tmp_path):
    """檔案夠長但後段是靜音（ACE-Step 實際會這樣）：必須判為不合格，不能默默產出無聲的最後一鏡。"""
    click_track(tmp_path / "m.wav", 120, offset=0.0, secs=28)
    y, sr = sf.read(tmp_path / "m.wav")
    y[int(15 * sr) :] = 0
    sf.write(tmp_path / "m.wav", y, sr)
    with pytest.raises(ValueError, match="結束了"):
        audio.analyze(tmp_path / "m.wav", 120, 5)


def test_frame_bounds_error_under_one_frame():
    cuts = [0.37 + 3.7 * i for i in range(6)]
    for fps in (24, 30):
        b = audio.frame_bounds(cuts, fps)
        assert all(abs(x / fps - (c - cuts[0])) <= 0.5 / fps + 1e-9 for x, c in zip(b, cuts, strict=True))


def _ff(*a):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *map(str, a)], check=True)


def test_mux_qc_end_to_end(tmp_path, monkeypatch):
    size, fps = "704x1280", 24
    """用 ffmpeg 合成的假素材跑 mux → qc：1080x1920、15–20.5 秒、有音軌、響度合格。"""
    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(visual_qc, "check", lambda job: {"status": "passed"})
    d = tmp_path / "2026-01-01_t"
    (d / "clips").mkdir(parents=True)
    plan = {
        "topic": "t",
        "slug": "t",
        "style": "s",
        "character": "a round brown bear cub with a red scarf",
        "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
        "shots": [{"id": i, "prompt": "p"} for i in range(1, 6)],
    }
    (d / "plan.json").write_text(json.dumps(plan))
    (d / "meta.json").write_text(json.dumps({"timings": {}, "models": {}, "shots": {}}))
    for i, color in enumerate(["red", "green", "blue", "yellow", "purple"], 1):
        _ff(
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r={fps}:d=5.05",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            d / "clips" / f"{i:02d}.mp4",
        )
    click_track(d / "music.wav", 120, offset=0.37, secs=22)
    info = audio.analyze(d / "music.wav", 120, 5)
    (d / "beats.json").write_text(json.dumps(info))
    job = pipeline.Job(d)
    pipeline.mux(job)
    pipeline.qc(job)
    status = json.loads((d / "status.json").read_text())
    assert status["status"] == "pending_review", status
    assert (d / "thumb.jpg").exists() and not (d / "audio_trim.wav").exists()
    assert "qc" in job.meta["timings"]  # QC 也要計時，bench 才有這欄
    sr = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "csv=p=0",
            d / "final.mp4",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert sr == "48000"  # loudnorm 會把取樣率拉到 192k，不指定的話成片會變 96kHz
    # 再跑一次不會重做（輸出已存在就跳過）
    mtime = (d / "final.mp4").stat().st_mtime_ns
    pipeline.mux(job)
    assert (d / "final.mp4").stat().st_mtime_ns == mtime


def test_workflows_match_live_object_info():
    """對照實際 ComfyUI 的 /object_info：節點存在、輸入名稱正確、必填輸入齊全（連不上就跳過）。"""
    import requests

    try:
        info = requests.get(f"{config.COMFY_URL}/object_info", timeout=15).json()
    except requests.RequestException:
        pytest.skip("ComfyUI 連不上")
    for f in sorted((config.ROOT / "workflows").glob("*.json")):
        for nid, node in json.loads(f.read_text()).items():
            spec = info[node["class_type"]]["input"]
            known = set(spec["required"]) | set(spec.get("optional", {}))
            # 動態 combo（如 TextGenerate 的 sampling_mode）會依選到的選項多出 "<名稱>.<子欄位>" 輸入
            for name, (kind, *opt) in {**spec["required"], **spec.get("optional", {})}.items():
                if kind == "COMFY_DYNAMICCOMBO_V3":
                    chosen = next(o for o in opt[0]["options"] if o["key"] == node["inputs"].get(name))
                    known |= {f"{name}.{k}" for group in chosen["inputs"].values() for k in group}
                if (
                    kind == "COMFY_AUTOGROW_V3"
                ):  # 動態長出的輸入（如 H3 的 ref_images.ref_image_0）：<名稱>.<前綴><序號>
                    tpl = opt[0]["template"]
                    prefix, limit = tpl["prefix"], tpl["max"]
                    known |= {f"{name}.{prefix}{i}" for i in range(limit)}
            given = set(node["inputs"])
            assert given <= known, f"{f.name}:{nid} 未知輸入 {given - known}"
            assert set(spec["required"]) <= given, f"{f.name}:{nid} 缺必填 {set(spec['required']) - given}"


def test_missing_models_detects_absent_and_old_option_format():
    from videomaker.comfy import LOADERS, missing_models

    def info_with(present):  # 新格式 ["COMBO", {"options": [...]}] 與舊格式 [[...]] 都要能讀
        out = {}
        for cls, field in LOADERS.items():
            opts = sorted(present)
            out[cls] = {
                "input": {"required": {field: ["COMBO", {"options": opts}] if cls != "VAELoader" else [opts]}}
            }
        return out

    all_names = {
        n
        for spec in __import__("yaml").safe_load((config.ROOT / "workflow_map.yaml").read_text()).values()
        for n in models_in(json.loads((config.ROOT / spec["file"]).read_text()))
    }
    assert missing_models(info_with(all_names)) == []
    miss = missing_models(info_with(all_names - {"wan2.2_vae.safetensors", "flux1-schnell-fp8.safetensors"}))
    assert {f for _, f in miss} == {"wan2.2_vae.safetensors", "flux1-schnell-fp8.safetensors"}


def _photo_job(tmp_path, monkeypatch, n_photos, with_txt=True):
    """照片模式：用 ffmpeg 合成的假狗照跑 _use_photos + keyframes（不碰 ComfyUI）。"""
    from videomaker.plan import Shot

    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    src = tmp_path / "dogs"
    src.mkdir()
    for i in range(n_photos):
        _ff("-f", "lavfi", "-i", "color=c=orange:s=1080x1920:d=1", "-frames:v", 1, src / f"dog{i + 1}.jpg")
        if with_txt and i == 0:
            (src / "dog1.txt").write_text("a golden retriever sitting on grass\n")
    plan = Plan.model_validate(
        {
            "topic": "t",
            "slug": "t",
            "style": "S",
            "character": "a round brown bear cub with a red scarf",
            "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
            "shots": [{"id": i, "prompt": "p"} for i in range(1, 6)],
        }
    )
    assert all(isinstance(s, Shot) for s in plan.shots)
    d = tmp_path / "job"
    d.mkdir()
    return plan, src, d


def test_photo_mode_assigns_photos_and_clears_style(tmp_path, monkeypatch):
    plan, src, d = _photo_job(tmp_path, monkeypatch, n_photos=2)
    used = pipeline._use_photos(plan, [src], d)
    assert [u["files"][0] for u in used] == [
        "dog1.jpg",
        "dog2.jpg",
        "dog1.jpg",
        "dog2.jpg",
        "dog1.jpg",
    ]  # 照片不夠 → 輪流
    assert plan.shots[0].prompt.startswith("a golden retriever sitting on grass. the dog bobs")
    assert plan.shots[1].prompt.startswith("a dog. ")  # 沒有 .txt → 預設描述
    assert (
        plan.full_prompt(plan.shots[0]) == plan.shots[0].prompt
    )  # style/character 已清空，前面不會有 ", , "
    assert len(used[0]["sha256"][0]) == 64 and (d / "source" / "05_a.jpg").exists()


def test_photo_mode_empty_dir_raises(tmp_path, monkeypatch):
    plan, _, d = _photo_job(tmp_path, monkeypatch, n_photos=1)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="沒有圖片"):
        pipeline._use_photos(plan, [empty], d)


def test_photo_mode_keyframes_resized_without_comfy(tmp_path, monkeypatch):
    size = (704, 1280)
    plan, src, d = _photo_job(tmp_path, monkeypatch, n_photos=5)
    used = pipeline._use_photos(plan, [src], d)
    (d / "plan.json").write_text(plan.model_dump_json())
    (d / "meta.json").write_text(json.dumps({"timings": {}, "models": {}, "shots": {}, "images": used}))
    job = pipeline.Job(d)
    pipeline.keyframes(job)  # 若誤呼叫 ComfyUI 會因連不上而失敗
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            d / "keyframes" / "03.png",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == f"{size[0]}x{size[1]}"
    assert job.meta["shots"]["3"]["source"].startswith("03_a.")
    mtime = (d / "keyframes" / "03.png").stat().st_mtime_ns
    pipeline.keyframes(job)  # 已存在就跳過
    assert (d / "keyframes" / "03.png").stat().st_mtime_ns == mtime


def test_two_dogs_prompts_and_sources(tmp_path, monkeypatch):
    plan, a, d = _photo_job(tmp_path, monkeypatch, n_photos=2)
    b = tmp_path / "dogsB"
    b.mkdir()
    _ff("-f", "lavfi", "-i", "color=c=white:s=1080x1920:d=1", "-frames:v", 1, b / "b1.jpg")
    (b / "b1.txt").write_text("a small white fluffy dog")
    used = pipeline._use_photos(plan, [a, b], d)
    assert used[0]["desc"] == ["a golden retriever sitting on grass", "a small white fluffy dog"]
    assert used[1]["files"] == ["dog2.jpg", "b1.jpg"] and used[2]["files"] == [
        "dog1.jpg",
        "b1.jpg",
    ]  # A 輪流、B 只有一張就重複
    assert len({u["scene"] for u in used}) == 5  # 每鏡不同場景
    assert plan.shots[0].prompt.startswith(
        "a golden retriever sitting on grass and a small white fluffy dog, sitting side by side on a sunny"
    )
    assert "both dogs bob" in plan.shots[0].prompt
    assert (d / "source" / "03_a.jpg").exists() and (d / "source" / "03_b.jpg").exists()
    with pytest.raises(ValueError, match="1 個資料夾"):
        pipeline._use_photos(plan, [a, b, b], d)


def test_two_dogs_keyframes_call_compose_workflow(tmp_path, monkeypatch):
    """兩隻狗：keyframes 走 compose workflow（假 Comfy 取代 ComfyUI），輸出尺寸與 prompt 正確。"""
    calls = []

    class FakeComfy:
        def prepare(self): ...
        def free(self): ...
        def upload(self, path, name):
            return name

        def run(self, wf, dest, timeout):
            calls.append(wf)
            _ff(
                "-f",
                "lavfi",
                "-i",
                f"color=c=green:s={wf['latent']['inputs']['width']}x{wf['latent']['inputs']['height']}:d=1",
                "-frames:v",
                1,
                dest,
            )
            return dest

    monkeypatch.setattr(pipeline, "Comfy", FakeComfy)
    plan, a, d = _photo_job(tmp_path, monkeypatch, n_photos=2)
    b = tmp_path / "dogsB"
    b.mkdir()
    _ff("-f", "lavfi", "-i", "color=c=white:s=1080x1920:d=1", "-frames:v", 1, b / "b1.jpg")
    used = pipeline._use_photos(plan, [a, b], d)
    (d / "plan.json").write_text(plan.model_dump_json())
    (d / "meta.json").write_text(json.dumps({"timings": {}, "models": {}, "shots": {}, "images": used}))
    job = pipeline.Job(d)
    pipeline.keyframes(job)
    assert len(calls) == 5
    inp = calls[0]
    assert (inp["latent"]["inputs"]["width"], inp["latent"]["inputs"]["height"]) == (704, 1280)
    assert (
        "image 1" in inp["pos"]["inputs"]["prompt"] and "sunny green lawn" in inp["pos"]["inputs"]["prompt"]
    )
    assert inp["img1"]["inputs"]["image"].endswith("01_a.jpg") and inp["img2"]["inputs"]["image"].endswith(
        "01_b.jpg"
    )
    assert "qwen_image_edit_2509_fp8_e4m3fn.safetensors" in job.meta["models"]["keyframes"]
    assert job.meta["shots"]["2"]["compose_seed"] == plan.seed + 2
    pipeline.keyframes(job)  # 已存在就跳過，不會再呼叫
    assert len(calls) == 5


def test_prepare_frees_before_checking_memory(monkeypatch):
    """先 /free 再量記憶體：模擬 /free 之後記憶體才慢慢回來（舊順序會直接中止）。"""
    from videomaker.comfy import Comfy, ComfyError

    events = []

    class R:
        def __init__(self, data):
            self.data, self.ok = data, True

        def json(self):
            return self.data

    c = Comfy("http://x")
    free_gb = iter([10, 10, 80])  # /free 後第三次量測才夠

    def fake_req(method, path, **kw):
        events.append(path)
        if path == "/system_stats":
            return R({"system": {"ram_free": next(free_gb) * 2**30}})
        return R({})

    monkeypatch.setattr(c, "_req", fake_req)
    monkeypatch.setattr("videomaker.comfy.time.sleep", lambda s: None)
    c.prepare()
    assert events[0] == "/free" and events.count("/system_stats") == 3
    monkeypatch.setattr(
        c,
        "_req",
        lambda m, p, **kw: R({"system": {"ram_free": 5 * 2**30}}) if p == "/system_stats" else R({}),
    )
    monkeypatch.setattr("videomaker.comfy.time.monotonic", iter(range(0, 1000, 20)).__next__)
    with pytest.raises(ComfyError, match="中止"):
        c.prepare()


def test_prepare_extra_gb_raises_the_memory_floor(monkeypatch):
    """FLUX.3＋upsampler 實測比 Wan 多吃約 8GB（峰值 37.4 對 29.6GB）：門檻 = VMK_MIN_FREE_GB + extra_gb，基準不被降低。"""
    from videomaker.comfy import Comfy, ComfyError

    class R:
        ok = True

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    c = Comfy("http://x")
    monkeypatch.setattr(config, "MIN_FREE_GB", 40.0)
    monkeypatch.setattr(
        c,
        "_req",
        lambda m, p, **kw: R({"system": {"ram_free": 45 * 2**30}}) if p == "/system_stats" else R({}),
    )
    monkeypatch.setattr("videomaker.comfy.time.monotonic", iter(range(0, 1000, 20)).__next__)
    c.prepare()  # 45GB ≥ 40GB：Wan 等一般階段照常通過
    with pytest.raises(ComfyError, match="48GB"):
        c.prepare(extra_gb=8)  # 45GB < 48GB：FLUX.3 階段中止


def test_flux3_keyframes_ask_for_extra_memory_but_schnell_does_not(tmp_path, monkeypatch):
    asked = {}

    class FakeComfy:
        def __init__(self):
            self.last_outputs = {}

        def prepare(self, extra_gb=0):
            asked["extra_gb"] = extra_gb

        def free(self): ...

        def run(self, wf, dest, timeout):
            dest.parent.mkdir(parents=True, exist_ok=True)
            _png(dest)

    monkeypatch.setattr(pipeline, "Comfy", FakeComfy)
    for kind, expected in (("kids", pipeline.FLUX3_EXTRA_GB), ("trivia", 0)):
        d = tmp_path / kind
        d.mkdir()
        plan = {
            "topic": "t",
            "slug": "t",
            "kind": kind,
            "style": "s",
            "character": "a round brown bear cub with a red scarf",
            "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
            "seed": 7,
            "shots": [{"id": i, "prompt": "p"} for i in range(1, 6)],
        }
        (d / "plan.json").write_text(json.dumps(plan))
        (d / "meta.json").write_text(json.dumps({"timings": {}, "models": {}, "shots": {}}))
        pipeline.keyframes(pipeline.Job(d))
        assert asked["extra_gb"] == expected
    assert pipeline.FLUX3_EXTRA_GB == 8


def test_plan_roundtrip_with_empty_style_and_character(tmp_path):
    """照片模式把 style/character 清空後存成 plan.json，重新載入必須成功（Job 每階段都會重新載入）。"""
    plan = Plan.model_validate(
        {
            "topic": "t",
            "slug": "a",
            "style": "S",
            "character": "a round brown bear cub with a red scarf",
            "shots": [{"id": 1, "prompt": "p"}],
            "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
        }
    )
    plan.style = plan.character = ""
    again = Plan.model_validate_json(plan.model_dump_json())
    assert again.character == "" and again.full_prompt(again.shots[0]) == "p"


def _png(path, w=704, h=1280, bars=0, color="black"):
    vf = f"pad=iw:ih+{2 * bars}:0:{bars}:{color}" if bars else "null"
    _ff(
        "-f",
        "lavfi",
        "-i",
        f"gradients=s={w}x{h - 2 * bars}:c0=0xF4D6A0:c1=0x8CC9A8",
        "-vf",
        vf,
        "-frames:v",
        1,
        path,
    )


def test_has_black_bars_detects_letterbox(tmp_path):
    _png(tmp_path / "ok.png")
    _png(tmp_path / "bars.png", bars=58)  # 與實際發生的 crop=704:1164 同量級
    assert not pipeline._has_black_bars(tmp_path / "ok.png", 704, 1280)
    assert pipeline._has_black_bars(tmp_path / "bars.png", 704, 1280)


def test_has_white_bars_detects_letterbox_but_not_white_background(tmp_path):
    """FLUX.3 不用 upsampler 時實測會出現上下白帶；白色背景（主體沒有橫跨整張寬）不能誤判。"""
    _png(tmp_path / "bars.png", bars=298, color="white")  # 與實測 crop=704:684:0:298 同量級
    _png(tmp_path / "ok.png")
    # 白底、中間一塊不到整張寬的主體：裁切後寬度會縮水，不是 letterbox
    pipeline._ff(
        "-f",
        "lavfi",
        "-i",
        "color=c=white:s=704x1280",
        "-vf",
        "drawbox=x=150:y=300:w=400:h=700:color=red:t=fill",
        "-frames:v",
        1,
        tmp_path / "white_bg.png",
    )
    assert pipeline._has_white_bars(tmp_path / "bars.png", 704, 1280)
    assert not pipeline._has_white_bars(tmp_path / "ok.png", 704, 1280)
    assert not pipeline._has_white_bars(tmp_path / "white_bg.png", 704, 1280)


def test_keyframes_regenerate_with_new_seed_when_black_bars(tmp_path, monkeypatch):
    seeds = []

    class FakeComfy:
        def __init__(self):
            self.last_outputs = {}

        def prepare(self, extra_gb=0): ...
        def free(self): ...

        def run(self, wf, dest, timeout):
            seeds.append(wf["ks"]["inputs"]["seed"])
            dest.parent.mkdir(parents=True, exist_ok=True)  # 與真正的 Comfy._fetch 一致
            _png(dest, bars=58 if len(seeds) == 1 else 0)  # 只有第一次呼叫有黑邊

    monkeypatch.setattr(pipeline, "Comfy", FakeComfy)
    monkeypatch.setattr(config, "WORK", tmp_path)
    d = tmp_path / "job"
    d.mkdir()
    plan = {
        "topic": "t",
        "slug": "t",
        "style": "s",
        "character": "a round brown bear cub with a red scarf",
        "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
        "seed": 7,
        "shots": [{"id": i, "prompt": "p"} for i in range(1, 6)],
    }
    (d / "plan.json").write_text(json.dumps(plan))
    (d / "meta.json").write_text(json.dumps({"timings": {}, "models": {}, "shots": {}}))
    job = pipeline.Job(d)
    pipeline.keyframes(job)
    assert seeds[:2] == [8, 1008]  # shot 1 先用 seed+1，有黑邊後用 seed+1+1000
    assert len(seeds) == 6  # 其餘 4 鏡各 1 次
    assert job.meta["shots"]["1"]["seed"] == 1008  # meta 記錄實際使用的 seed
    assert not pipeline._has_black_bars(d / "keyframes" / "01.png", 704, 1280)


@pytest.mark.parametrize(
    ("kind", "first_node", "model"),
    [
        ("kids", "pup", "flux3-image-klein-bf16.safetensors"),
        ("trivia", "pos", "flux1-schnell-fp8.safetensors"),
    ],
)
def test_keyframes_workflow_by_kind(tmp_path, monkeypatch, kind, first_node, model):
    """兒童音樂用 FLUX.3＋upsampler（擴寫後的 caption 要進 meta 當授權證據）；小知識維持 schnell。"""
    sent = []

    class FakeComfy:
        def prepare(self, extra_gb=0): ...
        def free(self): ...

        def run(self, wf, dest, timeout):
            sent.append(wf)
            # 與真正的 ComfyUI 一致：只有帶 upsampler 的 workflow 才有 pup_text 輸出
            self.last_outputs = (
                {"pup_text": {"text": ["Full-color photograph of a bear"]}} if "pup" in wf else {}
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            _png(dest)

    monkeypatch.setattr(pipeline, "Comfy", FakeComfy)
    monkeypatch.setattr(config, "WORK", tmp_path)
    d = tmp_path / "job"
    d.mkdir()
    plan = {
        "topic": "t",
        "slug": "t",
        "kind": kind,
        "style": "s",
        "character": "a round brown bear cub with a red scarf",
        "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
        "seed": 7,
        "shots": [{"id": i, "prompt": "p"} for i in range(1, 6)],
    }
    (d / "plan.json").write_text(json.dumps(plan))
    (d / "meta.json").write_text(json.dumps({"timings": {}, "models": {}, "shots": {}}))
    job = pipeline.Job(d)
    pipeline.keyframes(job)
    assert ("pup" in sent[0]) == (kind == "kids")  # 只有 FLUX.3 的 workflow 有 upsampler 節點
    if kind == "kids":
        assert sent[0]["pup"]["inputs"]["prompt"] == job.plan.full_prompt(job.plan.shots[0])
        assert job.meta["shots"]["1"]["upsampled"] == "Full-color photograph of a bear"
    else:
        assert "upsampled" not in job.meta["shots"]["1"]
    assert model in job.meta["models"]["keyframes"]


def test_estimate_keyframes_slower_for_kids(monkeypatch):
    """兒童音樂的關鍵幀改用 FLUX.3＋upsampler（實測約 35–42 秒/張），預估要比小知識（schnell）長。"""
    from videomaker import nightly

    monkeypatch.setattr(config, "STEPS", 20)
    assert nightly.estimate("short", "kids") > nightly.estimate("short", "trivia")
    assert nightly.estimate("short") == nightly.estimate("short", "kids")  # 預設仍是兒童音樂


def test_bench_counts_only_jobs_that_finished_qc(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    for name, done in (("a", True), ("b", True), ("c", False)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "meta.json").write_text(
            json.dumps({"timings": {"clips": 100 if name == "a" else 300}})
        )
        if done:
            (tmp_path / name / "status.json").write_text("{}")
    row = pipeline.bench().splitlines()[-1]
    assert "| 2 |" in row and "200s" in row  # 只算 a、b：支數 2、clips 平均 200 秒


_OK_PLAN = {
    "topic": "t",
    "slug": "bear-brush",
    "style": "flat 2D cartoon",
    "character": "a round brown bear cub with a red scarf",
    "shots": [{"id": 1, "prompt": "the bear holds a short straight toothbrush and smiles"}],
    "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
}


def test_plan_accepts_toothbrush_but_rejects_risky_words():
    Plan.model_validate(_OK_PLAN)  # toothbrush 本身可以，teeth/tooth 單字才擋
    risky = [
        "the bear shows its teeth",  # 牙齒
        "the bear opens its mouth wide",  # 嘴巴
        "the bear grins at the mirror",
        "the bear uses dental floss",  # 細長會彎的物體
        "the bear pours water into a cup",  # 液體
        "the bear blows soap bubbles",  # 泡沫
        "the bear squeezes toothpaste onto the brush",
        "the toothbrush bends as the bear brushes",  # 彎曲
    ]
    for prompt in risky:
        with pytest.raises(ValueError, match="高風險字眼"):
            Plan.model_validate({**_OK_PLAN, "shots": [{"id": 1, "prompt": prompt}]})
    # character 也不能提牙齒（實際發生過：LLM 寫了 bright yellow teeth，FLUX 就畫出怪牙）
    with pytest.raises(ValueError, match="高風險字眼"):
        Plan.model_validate(
            {**_OK_PLAN, "character": "a cute orange bear with a blue toothbrush and bright yellow teeth"}
        )


def test_risky_rules_not_applied_to_photo_mode(tmp_path, monkeypatch):
    """照片模式的描述來自使用者：不套用 RISKY，但中文描述要在 _use_photos 就清楚報錯。"""
    plan, src, d = _photo_job(tmp_path, monkeypatch, n_photos=1)
    (src / "dog1.txt").write_text("a dog with an open mouth")
    used = pipeline._use_photos(plan, [src], d)
    Plan.model_validate_json(plan.model_dump_json())  # 存檔後重新載入（Job 每階段都會做）不能失敗
    assert used[0]["desc"] == ["a dog with an open mouth"]
    (tmp_path / "x").mkdir()
    plan2, src2, d2 = _photo_job(tmp_path / "x", monkeypatch, n_photos=1)
    (src2 / "dog1.txt").write_text("一隻黃金獵犬")
    with pytest.raises(ValueError, match="純英文"):
        pipeline._use_photos(plan2, [src2], d2)


def test_wan_negative_prompt_mentions_teeth_and_bent_objects():
    neg = fill("clip", prompt="p", image="i", seed=1, width=1, height=1, length=121)["neg"]["inputs"]["text"]
    assert "牙齿变形" in neg and "弯曲的牙刷" in neg


def test_prop_color_must_be_fixed_across_shots():
    ok = {
        **_OK_PLAN,
        "prop": "a short straight blue toothbrush",
        "shots": [
            {"id": 1, "prompt": "the bear holds the toothbrush and waves"},
            {"id": 2, "prompt": "the bear tilts its head with a short straight blue toothbrush"},
        ],
    }
    plan = Plan.model_validate(ok)
    # prop 併入每鏡固定前綴，道具顏色由程式保證一致
    assert plan.full_prompt(plan.shots[0]).startswith(
        "flat 2D cartoon, a round brown bear cub with a red scarf, holding a short straight blue toothbrush, the bear holds"
    )
    for bad_prop, shots, msg in (
        ("a short straight toothbrush", ok["shots"], "必須寫出顏色"),  # prop 沒顏色
        (
            "a short straight blue toothbrush",
            [{"id": 1, "prompt": "the bear waves a pink toothbrush"}],
            "不同",
        ),  # 某鏡換成粉紅色
        (
            "a short straight blue toothbrush",
            [{"id": 1, "prompt": "the bear holds a red and white toothbrush"}],
            "不同",
        ),
    ):
        with pytest.raises(ValueError, match=msg):
            Plan.model_validate({**ok, "prop": bad_prop, "shots": shots})
    # 場景顏色不受影響（只檢查道具名詞前面的顏色）
    Plan.model_validate(
        {**ok, "shots": [{"id": 1, "prompt": "the bear sits on a green lawn holding the blue toothbrush"}]}
    )
    # 沒有 prop（不需要道具的主題）照常通過；舊的 plan.json 沒有 prop 欄位也能載入
    assert Plan.model_validate(_OK_PLAN).prop == ""


def test_make_plan_accepts_null_prop(monkeypatch):
    from videomaker import plan as planmod

    reply = {
        **_OK_PLAN,
        "prop": None,
        "youtube": _BILINGUAL_YT,
        "shots": [{"id": i, "prompt": "the bear waves"} for i in range(1, 6)],
    }

    class R:
        def raise_for_status(self): ...
        def json(self):
            return {"choices": [{"message": {"content": json.dumps(reply)}}]}

    monkeypatch.setattr(planmod.requests, "post", lambda *a, **k: R())
    assert planmod.make_plan("t", 5, 120).prop == ""


@pytest.mark.parametrize("bpm", range(100, 141))
def test_shot_math_long_bounds(bpm):
    from videomaker.plan import FORMATS

    shot, _, total = shot_math(bpm, "long")
    assert shot <= 5.0 and FORMATS["long"]["min_total"] <= total <= FORMATS["long"]["max_total"]
    assert total < 180  # YouTube Shorts 上限 3:00，必須留餘裕


def test_build_layout_cycles_with_new_order_and_no_adjacent_repeat():
    from videomaker.plan import build_layout

    lay = build_layout(14, 42, seed=5)
    assert len(lay) == 42 and lay[:14] == list(range(1, 15))  # 第一輪照順序
    assert all(lay.count(i) == 3 for i in range(1, 15))  # 每個鏡頭剛好 3 次
    assert lay[14:28] != lay[:14] and lay[28:] != lay[:14]  # 之後每輪換順序
    assert all(a != b for a, b in itertools.pairwise(lay))  # 相鄰時段不同鏡頭
    assert lay == build_layout(14, 42, seed=5) != build_layout(14, 42, seed=6)  # 可重現，seed 不同順序不同
    assert len(build_layout(14, 35, seed=5)) == 35  # 時段數不是整輪也行（BPM 100 時 35 個時段）


def test_plan_rejects_layout_with_unknown_shot_id():
    with pytest.raises(ValueError, match="layout"):
        Plan.model_validate({**_OK_PLAN, "layout": [1, 2]})  # 只有 shot 1


def test_long_format_mux_reuses_clips_with_offsets_and_qc_uses_long_bounds(tmp_path, monkeypatch):
    """長片：4 個獨特鏡頭填 10 個時段。縮小片型參數讓測試只跑幾秒，邏輯與正式長片相同。"""
    from videomaker import plan as planmod

    monkeypatch.setattr(visual_qc, "check", lambda job: {"status": "passed"})

    monkeypatch.setitem(
        planmod.FORMATS, "long", {"target": 40, "min_total": 30, "max_total": 45, "unique": 4}
    )
    monkeypatch.setattr(config, "WORK", tmp_path)
    d = tmp_path / "2026-01-01_long"
    (d / "clips").mkdir(parents=True)
    _, n_slots, _ = shot_math(120, "long")
    assert n_slots == 10
    plan = {
        "topic": "t",
        "slug": "t",
        "style": "s",
        "character": "a round brown bear cub with a red scarf",
        "seed": 3,
        "music": {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"},
        "shots": [{"id": i, "prompt": "p"} for i in range(1, 5)],
        "layout": planmod.build_layout(4, n_slots, 3),
    }
    (d / "plan.json").write_text(json.dumps(plan))
    (d / "meta.json").write_text(json.dumps({"format": "long", "timings": {}, "models": {}, "shots": {}}))
    for i, color in enumerate(["red", "green", "blue", "yellow"], 1):
        _ff(
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=704x1280:r=24:d=5.05",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            d / "clips" / f"{i:02d}.mp4",
        )
    click_track(d / "music.wav", 120, offset=0.37, secs=60)
    (d / "beats.json").write_text(json.dumps(audio.analyze(d / "music.wav", 120, n_slots)))
    job = pipeline.Job(d)
    assert job.fmt == "long"
    pipeline.mux(job)
    pipeline.qc(job)
    status = json.loads((d / "status.json").read_text())
    assert status["status"] == "pending_review", status
    assert "duration_30_45" in status["checks"] and 39 <= status["details"]["duration"] <= 41


def test_nightly_deadline_estimate_and_topics(monkeypatch):
    from datetime import datetime

    from videomaker import nightly

    night = datetime(2026, 10, 6, 22, 0).astimezone()
    assert nightly.deadline_after(night, "07:00") == night.replace(
        day=7, hour=7
    )  # 晚上 22:00 啟動 → 隔天 07:00
    assert nightly.deadline_after(night.replace(hour=2), "07:00") == night.replace(
        hour=7
    )  # 凌晨 2 點 → 當天 07:00
    assert nightly.deadline_after(night.replace(hour=7), "07:00") == night.replace(
        day=7, hour=7
    )  # 剛好 07:00 → 算下一個
    monkeypatch.setattr(config, "STEPS", 20)
    short, long = nightly.estimate("short"), nightly.estimate("long")
    assert 30 * 60 < short < 45 * 60 and 90 * 60 < long < 130 * 60  # 實測短片 30 分鐘、長片估約 1.5–2 小時
    monkeypatch.setattr(config, "STEPS", 40)
    assert nightly.estimate("long") > 2 * 90 * 60  # 步數加倍，長片預估也跟著加倍
    # 22:00–07:00 共 9 小時：預設的每晚組合（short、long、xl 各 1 支）在 20 步下要塞得進去，且留有餘裕
    monkeypatch.setattr(config, "STEPS", 20)
    assert sum(nightly.estimate(f) for f in nightly.NIGHT) < 6 * 3600


@pytest.mark.parametrize("override", [False, True])
def test_topic_model_defaults_and_overrides(monkeypatch, override):
    import runpy

    monkeypatch.setenv("VMK_LLM_MODEL", "planning-only")
    for kind in ("KIDS", "TRIVIA"):
        monkeypatch.delenv(f"VMK_{kind}_TOPIC_MODEL", raising=False)
        if override:
            monkeypatch.setenv(f"VMK_{kind}_TOPIC_MODEL", f"custom-{kind.lower()}")
    values = runpy.run_path(config.__file__)
    assert values["KIDS_TOPIC_MODEL"] == ("custom-kids" if override else "qwen3.8:27b")
    assert values["TRIVIA_TOPIC_MODEL"] == ("custom-trivia" if override else "gemma4:31b")
    assert values["LLM_MODEL"] == "planning-only"


@pytest.mark.parametrize("kind", ["kids", "trivia"])
def test_topic_requests_use_model_for_kind(monkeypatch, kind):
    from types import SimpleNamespace

    from videomaker import nightly

    monkeypatch.setattr(config, "KIDS_TOPIC_MODEL", "kids-model", raising=False)
    monkeypatch.setattr(config, "TRIVIA_TOPIC_MODEL", "trivia-model", raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", "planning-only")
    calls = []
    topic = "小貓跳舞" if kind == "kids" else "Sea otter"

    def post(url, *, json: dict, timeout):
        calls.append(json["model"])
        body = {"choices": [{"message": {"content": '{"topics": ["' + topic + '"]}'}}]}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(nightly.requests, "post", post)
    monkeypatch.setattr(nightly.wiki, "fetch", lambda title: SimpleNamespace(title=title))
    suggest = nightly.suggest_topics if kind == "kids" else nightly.suggest_trivia_topics
    assert suggest(1, []) == [topic]
    assert calls == [f"{kind}-model"]


@pytest.mark.parametrize("override", [False, True])
def test_plan_models_follow_topic_configuration(monkeypatch, override):
    import runpy

    monkeypatch.delenv("VMK_LLM_MODEL", raising=False)
    monkeypatch.setenv("VMK_KIDS_TOPIC_MODEL", "custom-kids" if override else "qwen3.8:27b")
    monkeypatch.setenv("VMK_TRIVIA_TOPIC_MODEL", "custom-trivia" if override else "gemma4:31b")
    values = runpy.run_path(config.__file__)
    assert values["LLM_MODEL"] == ""
    assert values["plan_model"]("kids") == values["KIDS_TOPIC_MODEL"]
    assert values["plan_model"]("trivia") == values["TRIVIA_TOPIC_MODEL"]


@pytest.mark.parametrize("kind", ["kids", "trivia"])
@pytest.mark.parametrize("override", ["", "manual-planner"])
def test_topics_and_plans_share_model_and_record_actual_model(tmp_path, monkeypatch, kind, override):
    from types import SimpleNamespace

    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(config, "LLM_MODEL", override)
    monkeypatch.setattr(config, "KIDS_TOPIC_MODEL", "qwen3.8:27b")
    monkeypatch.setattr(config, "TRIVIA_TOPIC_MODEL", "gemma4:31b")
    topic = "小貓跳舞" if kind == "kids" else "Sea otter"
    reply = (
        _trivia_reply(_facts())
        if kind == "trivia"
        else {
            **_OK_PLAN,
            "youtube": _BILINGUAL_YT,
            "shots": [{"id": i, "prompt": "the bear waves"} for i in range(1, 6)],
        }
    )
    # 第一次企劃違規，重寫仍須使用同一模型，meta 也必須記錄真正的企劃模型。
    replies = iter([{"topics": [topic]}, {**reply, "style": ""}, reply])
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["model"])
        body = {"choices": [{"message": {"content": json.dumps(next(replies))}}]}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(nightly.requests, "post", post)
    monkeypatch.setattr(nightly.wiki, "fetch", lambda title: _article())
    suggest = nightly.suggest_topics if kind == "kids" else nightly.suggest_trivia_topics
    job = pipeline.new_job(suggest(1, [])[0], kind=kind)
    topic_model = "qwen3.8:27b" if kind == "kids" else "gemma4:31b"
    model = override or topic_model
    assert calls == [topic_model, model, model]
    assert job.meta["llm"] == json.loads(job.p("meta.json").read_text())["llm"] == model


def test_suggest_topics_filters_duplicates_and_retries(monkeypatch):
    from videomaker import nightly

    replies = iter(
        [
            {
                "topics": ["小熊刷牙", "小貓睡覺", "小貓睡覺", "x"]
            },  # 小熊刷牙已做過、重複、太短 → 只剩 1 個可用，不夠
            {"topics": ["小貓睡覺", "小兔跳舞", "小鳥唱歌", "小熊刷牙"]},
        ]
    )

    class R:
        def __init__(self, d):
            self.d = d

        def raise_for_status(self): ...
        def json(self):
            return {"choices": [{"message": {"content": json.dumps(self.d)}}]}

    monkeypatch.setattr(nightly.requests, "post", lambda *a, **k: R(next(replies)))
    assert nightly.suggest_topics(3, ["小熊刷牙"]) == ["小貓睡覺", "小兔跳舞", "小鳥唱歌"]
    monkeypatch.setattr(nightly.requests, "post", lambda *a, **k: R({"topics": ["小熊刷牙"]}))
    with pytest.raises(RuntimeError, match="3 次"):
        nightly.suggest_topics(2, ["小熊刷牙"])


def test_nightly_runs_in_order_skips_when_too_late_and_retries_once(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(config, "STEPS", 20)
    monkeypatch.setattr(nightly, "suggest_topics", lambda n, avoid: ["甲", "乙", "丙"][:n])
    calls = []

    class FakeJob:
        def __init__(self, topic):
            self.dir = tmp_path / topic
            self.dir.mkdir(exist_ok=True)
            (self.dir / "status.json").write_text(json.dumps({"status": "pending_review"}))

        def p(self, name):
            return self.dir / name

    def fake_run(topic, fmt="short", resume=False, kind=None):
        assert kind == "kids"  # 一律明確傳 kind，不靠預設值
        calls.append((topic, fmt, resume))
        if topic == "乙" and not resume:
            raise RuntimeError("ComfyUI 暫時掛掉")  # 第一次失敗、重試成功
        return FakeJob(topic)

    monkeypatch.setattr(nightly.pipeline, "run", fake_run)
    monkeypatch.setattr(
        nightly, "gpu_temp", lambda: None
    )  # 不依賴真實 GPU 溫度：GPU 還熱時 wait_cool 會多呼叫 sleep
    sleeps = []
    in_55_min = (datetime.now().astimezone() + timedelta(minutes=55)).strftime("%H:%M")
    res = nightly.nightly(["short", "short", "long"], in_55_min, sleep=sleeps.append)
    assert [r["status"] for r in res] == [
        "pending_review",
        "pending_review",
        "skipped",
    ]  # 長片預估 > 55 分鐘 → 略過
    assert calls == [("甲", "short", False), ("乙", "short", False), ("乙", "short", True)]  # 乙重試時 resume
    assert sleeps == [nightly.RETRY_WAIT]
    hist = [json.loads(x) for x in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert [h["topic"] for h in hist] == ["甲", "乙", "丙"] and hist[2]["status"] == "skipped"
    # 全部失敗：記錄 failed，不丟例外（排程不能被單支拖垮）
    monkeypatch.setattr(nightly.pipeline, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了")))
    res = nightly.nightly(["short"], in_55_min, sleep=lambda s: None)
    assert res[0]["status"] == "failed" and "壞了" in res[0]["error"]


def test_wait_cool_polls_until_below_threshold_and_respects_latest_start(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from videomaker import nightly

    monkeypatch.setattr(config, "COOL_TEMP", 45)
    later = datetime.now().astimezone() + timedelta(hours=1)
    temps = iter([85, 60, 45])  # 45 = 剛好達標（門檻是「大於才等」）
    monkeypatch.setattr(nightly, "gpu_temp", lambda: next(temps))
    sleeps = []
    nightly.wait_cool(later, sleeps.append)
    assert sleeps == [nightly.COOL_POLL] * 2
    # 一直降不下來：到 latest_start 就放棄等，不能無限卡住
    monkeypatch.setattr(nightly, "gpu_temp", lambda: 85)
    sleeps.clear()
    nightly.wait_cool(datetime.now().astimezone() - timedelta(seconds=1), sleeps.append)
    assert sleeps == []
    # 讀不到溫度（非 GX10）→ 不等
    monkeypatch.setattr(nightly, "gpu_temp", lambda: None)
    nightly.wait_cool(later, sleeps.append)
    assert sleeps == []
    # 整合：溫度降不下來又趕不上 deadline → 該支被略過、不跑
    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(nightly, "suggest_topics", lambda n, avoid: ["甲"])
    monkeypatch.setattr(nightly, "gpu_temp", lambda: 85)
    monkeypatch.setattr(nightly.pipeline, "run", lambda *a, **k: pytest.fail("不該開跑"))
    in_55_min = (datetime.now().astimezone() + timedelta(minutes=55)).strftime("%H:%M")
    res = nightly.nightly(["long"], in_55_min, sleep=lambda s: None)
    assert res[0]["status"] == "skipped"


def test_simplified_chinese_rejected_in_youtube_fields_but_traditional_ok():
    ok = {
        **_OK_PLAN,
        "youtube": {
            "title": "小熊刷牙｜快樂的刷牙時間",
            "description": "跟著小熊一起學習",
            "tags": ["兒童音樂", "小熊"],
        },
    }
    Plan.model_validate(ok)
    for field, bad in (
        ("title", "小熊刷牙｜快乐的刷牙时间"),
        ("description", "跟随小熊一起学习"),
        ("tags", ["儿童音乐"]),
    ):
        with pytest.raises(ValueError, match="簡體字"):
            Plan.model_validate({**ok, "youtube": {**ok["youtube"], field: bad}})
    # 兔、松、面 等繁簡同形字不能被誤判
    Plan.model_validate({**ok, "youtube": {"title": "松鼠與兔子", "description": "麵包", "tags": []}})


def test_suggest_topics_drops_simplified(monkeypatch):
    from videomaker import nightly

    class R:
        def raise_for_status(self): ...
        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"topics": ["小猫跳舞", "小貓跳舞", "风吹树叶", "小松鼠跳舞", "小兔睡覺"]}
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(nightly.requests, "post", lambda *a, **k: R())
    assert nightly.suggest_topics(3, []) == ["小貓跳舞", "小松鼠跳舞", "小兔睡覺"]


def test_cli_images_accepts_one_or_two_dirs_and_passes_a_list(monkeypatch, tmp_path):
    """CLI 層級：--images 要能收 1 或 2 個資料夾，並以 list 傳給 pipeline.run（先前只有函式層測試，CLI 其實是壞的）。"""
    from pathlib import Path

    from videomaker import __main__ as cli

    p = cli.build_parser()
    a = p.parse_args(["make", "主題", "--images", "photos/a", "photos/b", "--stage", "keyframes"])
    assert a.images == [Path("photos/a"), Path("photos/b")] and a.stage == "keyframes"
    assert p.parse_args(["make", "主題", "--images", "photos/a"]).images == [Path("photos/a")]
    assert p.parse_args(["make", "主題"]).images is None

    seen = {}

    class FakeJob:
        dir = tmp_path

    def fake_run(topic, upto, dry_run, resume, images, fmt, kind):
        seen.update(topic=topic, upto=upto, images=images, fmt=fmt, kind=kind)
        return FakeJob()

    monkeypatch.setattr(cli.pipeline, "run", fake_run)
    cli.main(["make", "主題", "--images", "x", "y", "--format", "long", "--stage", "keyframes"])
    assert seen == {
        "topic": "主題",
        "upto": "keyframes",
        "images": [Path("x"), Path("y")],
        "fmt": "long",
        "kind": "kids",
    }


class _FakeReq:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def next_chunk(self):
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return None, o


class _FakeYT:
    def __init__(self, outcomes):
        self.outcomes, self.calls = outcomes, []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.calls.append({"part": part, "body": body, "size": media_body.size()})
        return _FakeReq(self.outcomes)


def _yt_job(tmp_path, status="approved", images=None):
    d = tmp_path / "2026-01-01_t"
    d.mkdir()
    plan = {
        **_OK_PLAN,
        "youtube": {
            "title": "小熊刷牙｜" + "長" * 120,
            "description": "跟著小熊一起學習",
            "tags": ["兒童音樂", "小熊", "a b"],
        },
    }
    (d / "plan.json").write_text(json.dumps(plan))
    (d / "meta.json").write_text(json.dumps({"timings": {}, **({"images": images} if images else {})}))
    (d / "status.json").write_text(json.dumps({"status": status}))
    (d / "final.mp4").write_bytes(b"x" * 1000)
    if status == "approved":
        from videomaker import youtube

        youtube.set_review(d, "approved")  # 測試中的已核准作業也必須綁定當時的成片
    return d


def test_youtube_body_declares_not_made_for_kids_for_non_kids_content(tmp_path):
    from videomaker import youtube

    plan = Plan.model_validate_json((_yt_job(tmp_path) / "plan.json").read_text())
    for kind, kids in [("kids", True), ("trivia", True), ("kids_english_dialogue", True),
                       ("animal_sitcom", False), ("dialogue_animation", False)]:  # fmt: skip
        status = youtube.build_body(plan.model_copy(update={"kind": kind}), {})["status"]
        assert status["selfDeclaredMadeForKids"] is kids, kind  # 非兒童內容要明確送 False，不能省略
        assert status["privacyStatus"] == "private"  # 不論種類，上傳一律私人


def test_youtube_body_is_private_kids_and_trimmed(tmp_path):
    from videomaker import youtube

    d = _yt_job(tmp_path)
    plan = Plan.model_validate_json((d / "plan.json").read_text())
    b = youtube.build_body(plan, {})
    assert b["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": True,
        "containsSyntheticMedia": False,
    }
    assert len(b["snippet"]["title"]) == 100 and b["snippet"]["tags"] == ["兒童音樂", "小熊", "a b"]
    assert (
        youtube.build_body(plan, {"images": [{"shot": 1}]})["status"]["containsSyntheticMedia"] is True
    )  # 照片模式
    many = plan.model_copy(
        update={"youtube": plan.youtube.model_copy(update={"tags": [f"標籤{i:03d}" * 3 for i in range(100)]})}
    )
    assert sum(len(t) + 1 for t in youtube.build_body(many, {})["snippet"]["tags"]) <= 500


def test_youtube_upload_requires_approval_and_is_idempotent(tmp_path):
    from videomaker import youtube

    (tmp_path / "x").mkdir()
    pending = _yt_job(tmp_path / "x", status="pending_review")
    with pytest.raises(ValueError, match="必須先人工核准"):
        youtube.upload(pending, service=_FakeYT([{"id": "zzz"}]))
    youtube.set_review(pending, "approved")
    assert json.loads((pending / "status.json").read_text())["status"] == "approved"
    svc = _FakeYT([{"id": "vid123", "status": {"privacyStatus": "private"}}])
    assert youtube.upload(pending, service=svc) == "vid123"
    assert svc.calls[0]["part"] == "snippet,status" and svc.calls[0]["size"] == 1000
    saved = json.loads((pending / "meta.json").read_text())
    assert saved["youtube_id"] == "vid123" and saved["youtube_privacy"] == "private"
    assert (
        youtube.upload(pending, service=_FakeYT([])) == "vid123"
    )  # 第二次不會再呼叫 API（outcomes 是空的，呼叫就會爆）


def test_youtube_upload_retries_transient_errors_but_not_quota(tmp_path, monkeypatch):
    import httplib2
    from googleapiclient.errors import HttpError

    from videomaker import youtube

    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    err = lambda code: HttpError(httplib2.Response({"status": code}), b"{}")
    (tmp_path / "a").mkdir()
    assert (
        youtube.upload(_yt_job(tmp_path / "a"), service=_FakeYT([err(503), err(502), {"id": "ok1"}])) == "ok1"
    )
    (tmp_path / "b").mkdir()
    with pytest.raises(HttpError):  # 403 額度／權限錯誤：重試沒用，直接拋出
        youtube.upload(_yt_job(tmp_path / "b"), service=_FakeYT([err(403)]))


def test_youtube_pending_approved_lists_only_approved_not_uploaded(tmp_path, monkeypatch):
    from videomaker import youtube

    monkeypatch.setattr(config, "WORK", tmp_path)
    for name, status, vid in (
        ("a", "approved", None),
        ("b", "pending_review", None),
        ("c", "approved", "done"),
        ("d", "rejected", None),
    ):
        d = tmp_path / name
        d.mkdir()
        (d / "status.json").write_text(json.dumps({"status": status}))
        (d / "meta.json").write_text(json.dumps({"youtube_id": vid} if vid else {}))
    assert [p.name for p in youtube.pending_approved()] == ["a"]


def test_cli_upload_approved_continues_after_one_failure(monkeypatch, tmp_path, capsys):
    from videomaker import __main__ as cli

    jobs = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    monkeypatch.setattr(cli.youtube, "pending_approved", lambda: jobs)

    def fake_upload(d):
        if d.name == "b":
            raise RuntimeError("額度用完")
        return f"id-{d.name}"

    monkeypatch.setattr(cli.youtube, "upload", fake_upload)
    with pytest.raises(SystemExit) as e:
        cli.main(["upload-approved"])
    out = capsys.readouterr().out
    assert (
        e.value.code == 1 and "a id-a" in out and "c id-c" in out and "b 失敗" in out
    )  # b 失敗，a、c 仍上傳


def test_listing_shows_review_state_of_each_job(tmp_path, monkeypatch):
    from videomaker import youtube

    monkeypatch.setattr(config, "WORK", tmp_path)
    cases = {
        "a": ("pending_review", None, 20.0),
        "b": ("approved", None, 20.0),
        "c": ("approved", "vid9", 168.0),
        "d": ("rejected", None, 20.0),
        "e": ("failed", None, 20.0),
        "f": (None, None, 0),
    }
    for name, (status, vid, dur) in cases.items():
        d = tmp_path / name
        d.mkdir()
        (d / "plan.json").write_text(json.dumps({**_OK_PLAN, "topic": f"主題{name}"}))
        (d / "meta.json").write_text(
            json.dumps({"format": "long" if name == "c" else "short", **({"youtube_id": vid} if vid else {})})
        )
        if status:
            (d / "status.json").write_text(json.dumps({"status": status, "details": {"duration": dur}}))
    rows = youtube.listing()
    assert rows == [
        "a | short | 待審核 | 20s | 主題a",
        "b | short | 已核准（待上傳） | 20s | 主題b",
        "c | long | 已上傳 vid9 | 168s | 主題c",
        "d | short | 不通過 | 20s | 主題d",
        "e | short | QC 失敗 | 20s | 主題e",
        "f | short | 未完成 | - | 主題f",
    ]


_BILINGUAL_YT = {
    "title": "風鈴輕響｜Wind Chime Song for Kids",
    "description": "跟著小松鼠聽風鈴的聲音。\n輕快的音樂適合小朋友。\n\nA cheerful squirrel listens to wind chimes. Gentle music made for little kids.\n\n#KidsMusic #Shorts #NurseryRhymes",
    "tags": ["兒童音樂", "風鈴", "kids songs", "nursery rhymes", "baby songs"],
}


def test_check_bilingual_requires_chinese_and_english_everywhere():
    from videomaker.plan import Youtube, check_bilingual

    check_bilingual(Youtube(**_BILINGUAL_YT))
    for field, bad, msg in (
        ("title", "風鈴輕響－兒童音樂短片", "雙語"),  # 只有中文
        ("title", "Wind Chime Song for Kids", "雙語"),  # 只有英文
        ("title", "風鈴輕響｜" + "Wind Chime " * 10, "100"),  # 太長，英文會被截斷
        ("description", "跟著小松鼠聽風鈴的聲音。", "英文單字"),
        ("tags", ["兒童音樂", "風鈴", "kids songs"], "英文標籤"),
    ):
        with pytest.raises(ValueError, match=msg):
            check_bilingual(Youtube(**{**_BILINGUAL_YT, field: bad}))


def test_make_plan_retries_until_youtube_fields_are_bilingual(monkeypatch):
    from videomaker import plan as planmod

    zh_only = {"title": "風鈴輕響", "description": "跟著小松鼠聽風鈴。", "tags": ["兒童音樂"]}
    replies = iter([zh_only, _BILINGUAL_YT])
    sent = []

    class R:
        def __init__(self, yt):
            self.yt = yt

        def raise_for_status(self): ...
        def json(self):
            reply = {
                **_OK_PLAN,
                "youtube": self.yt,
                "shots": [{"id": i, "prompt": "the bear waves"} for i in range(1, 6)],
            }
            return {"choices": [{"message": {"content": json.dumps(reply)}}]}

    def fake_post(url, json=None, **kw):
        sent.append(json["messages"])
        return R(next(replies))

    monkeypatch.setattr(planmod.requests, "post", fake_post)
    plan = planmod.make_plan("t", 5, 120)
    assert plan.youtube.title == "風鈴輕響｜Wind Chime Song for Kids"
    assert len(sent) == 2 and "雙語" in sent[1][-1]["content"]  # 第一次中文單語被退回，並把原因告訴 LLM


class _Proc:
    def __init__(self, out="", code=0):
        self.stdout, self.returncode = out, code


def _renew_env(monkeypatch, remote, local="ok", confirm=True, copy_code=0, after="ok"):
    """替換 renew_token 用到的外部動作，記錄呼叫順序。"""
    from videomaker import youtube

    calls, remotes = [], iter([remote, after])
    monkeypatch.setattr(youtube, "remote_status", lambda: next(remotes))
    monkeypatch.setattr(youtube, "token_status", lambda: local)
    monkeypatch.setattr(youtube, "auth", lambda: calls.append("auth"))
    monkeypatch.setattr(youtube, "_ask", lambda msg: calls.append("ask") or confirm)
    monkeypatch.setattr(youtube, "_notify", lambda msg: calls.append(f"notify:{msg}"))
    monkeypatch.setattr(
        youtube,
        "_sh",
        lambda cmd, timeout=120: calls.append(cmd[0]) or _Proc(code=copy_code if cmd[0] == "scp" else 0),
    )
    return youtube, calls


def test_renew_token_does_nothing_when_gx10_token_is_ok(monkeypatch):
    youtube, calls = _renew_env(monkeypatch, "ok")
    assert youtube.renew_token() == "ok" and calls == []  # 不打擾你


def test_renew_token_unreachable_only_notifies(monkeypatch):
    youtube, calls = _renew_env(monkeypatch, "unreachable")
    assert youtube.renew_token() == "unreachable"
    assert calls == ["notify:連不上 GX10，無法檢查 YouTube token"]


def test_renew_token_declined_does_not_touch_anything(monkeypatch):
    youtube, calls = _renew_env(monkeypatch, "expired", confirm=False)
    assert youtube.renew_token() == "declined" and calls == ["ask"]  # 只問，沒有授權也沒有複製


def test_renew_token_reuses_valid_local_token_then_copies(monkeypatch):
    youtube, calls = _renew_env(monkeypatch, "expired", local="ok")
    assert youtube.renew_token() == "ok"
    assert (
        calls[:3] == ["ask", "scp", "ssh"] and "auth" not in calls
    )  # 本機 token 有效 → 不重新授權，直接複製、chmod
    assert calls[-1].startswith("notify:YouTube 授權已更新")


def test_renew_token_reauthorizes_when_local_token_also_expired(monkeypatch):
    youtube, calls = _renew_env(monkeypatch, "missing", local="expired")
    assert youtube.renew_token() == "ok"
    assert calls[:4] == ["ask", "auth", "scp", "ssh"]  # 先問 → 授權 → 複製 → chmod 600


def test_renew_token_reports_copy_failure_and_unverified_remote(monkeypatch):
    youtube, calls = _renew_env(monkeypatch, "expired", copy_code=1)
    assert youtube.renew_token() == "copy-failed" and calls[-1] == "notify:token 複製到 GX10 失敗"
    youtube, calls = _renew_env(monkeypatch, "expired", after="expired")
    assert (
        youtube.renew_token() == "expired" and "GX10 驗證仍是 expired" in calls[-1]
    )  # 複製了但遠端仍不通過，不能說成功


def test_remote_status_parses_last_line_and_rejects_garbage(monkeypatch):
    from videomaker import youtube

    monkeypatch.setattr(youtube, "_sh", lambda cmd, timeout=120: _Proc("Warning: banner line\nexpired\n"))
    assert youtube.remote_status() == "expired"
    monkeypatch.setattr(
        youtube,
        "_sh",
        lambda cmd, timeout=120: _Proc("ssh: connect to host gx10 port 22: Operation timed out", 255),
    )
    assert youtube.remote_status() == "unreachable"


def test_ask_detects_the_confirm_button(monkeypatch):
    from videomaker import youtube

    monkeypatch.setattr(
        youtube, "_sh", lambda cmd, timeout=120: _Proc("button returned:重新授權, gave up:false\n")
    )
    assert youtube._ask("x") is True
    monkeypatch.setattr(
        youtube, "_sh", lambda cmd, timeout=120: _Proc("button returned:稍後再說, gave up:false\n")
    )
    assert youtube._ask("x") is False
    monkeypatch.setattr(
        youtube, "_sh", lambda cmd, timeout=120: _Proc("", 1)
    )  # 使用者按 Esc／沒有圖形工作階段：不能當成確認
    assert youtube._ask("x") is False


def test_token_status_missing_and_expired(monkeypatch, tmp_path):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    from videomaker import youtube

    monkeypatch.setattr(youtube, "TOKEN", tmp_path / "token.json")
    assert youtube.token_status() == "missing"
    (tmp_path / "token.json").write_text("{}")

    class Expired:
        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "from_authorized_user_file", classmethod(lambda cls, *a, **k: Expired()))
    assert youtube.token_status() == "expired"

    class Fresh:
        def refresh(self, request): ...
        def to_json(self):
            return '{"refreshed": true}'

    monkeypatch.setattr(Credentials, "from_authorized_user_file", classmethod(lambda cls, *a, **k: Fresh()))
    assert youtube.token_status() == "ok" and (tmp_path / "token.json").read_text() == '{"refreshed": true}'
    assert oct((tmp_path / "token.json").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("bpm", range(100, 141))
def test_shot_math_xl_is_about_five_minutes(bpm):
    from videomaker.plan import FORMATS

    shot, n, total = shot_math(bpm, "xl")
    assert shot <= 5.0 and 270 <= total <= 330 and n >= 60
    assert FORMATS["xl"]["unique"] == 20


def test_xl_layout_reuses_each_shot_about_four_times_without_adjacent_repeats():
    import itertools

    from videomaker.plan import build_layout

    lay = build_layout(20, 75, seed=9)
    assert len(lay) == 75 and set(lay) == set(range(1, 21))
    assert all(3 <= lay.count(i) <= 4 for i in range(1, 21))  # 75 = 3×20 + 15
    assert all(a != b for a, b in itertools.pairwise(lay))


def test_photo_mode_20_shots_never_repeat_a_photo_and_scene_combination(tmp_path, monkeypatch):
    """5 分鐘版 20 個獨特鏡頭：照片組合（5 張輪流）與場景（7 個輪流）的組合不能重複，否則看起來是同一支畫面。"""
    import itertools

    plan, a, d = _photo_job(tmp_path, monkeypatch, n_photos=5)
    b = tmp_path / "dogsB"
    b.mkdir()
    for i in range(1, 6):
        _ff("-f", "lavfi", "-i", "color=c=white:s=1080x1920:d=1", "-frames:v", 1, b / f"b{i}.jpg")
    plan.shots = [plan.shots[0].model_copy(update={"id": i}) for i in range(1, 21)]
    used = pipeline._use_photos(plan, [a, b], d)
    combos = [(tuple(u["files"]), u["scene"]) for u in used]
    assert len(set(combos)) == 20
    assert len(pipeline.SCENES) == 7 and all(x != y for x, y in itertools.pairwise(u["scene"] for u in used))


@pytest.mark.parametrize("fmt,unique,slots", [("short", 5, 5), ("long", 14, 42), ("xl", 20, 75)])
def test_new_job_builds_reuse_layout_for_every_format_that_reuses_shots(
    tmp_path, monkeypatch, fmt, unique, slots
):
    """每種片型：獨特鏡頭數、時段數、layout 長度都要對得上（xl 曾經漏建 layout，合成階段要跑到最後才會爆）。"""
    monkeypatch.setattr(config, "WORK", tmp_path)
    seen = {}

    def fake_make_plan(topic, n_shots, bpm, long=False):
        seen["args"] = (n_shots, long)
        return Plan.model_validate(
            {
                **_OK_PLAN,
                "slug": "t",
                "shots": [{"id": i, "prompt": "the bear waves"} for i in range(1, n_shots + 1)],
            }
        )

    monkeypatch.setattr(pipeline, "make_plan", fake_make_plan)
    job = pipeline.new_job("主題", fmt=fmt)
    assert seen["args"] == (unique, unique < slots)  # 長片要告訴 LLM「鏡頭會被重複播放」
    assert len(job.plan.shots) == unique
    assert len(job.plan.layout) == (slots if unique < slots else 0)
    if unique < slots:
        assert set(job.plan.layout) == set(range(1, unique + 1))  # 每個獨特鏡頭都被排到
    assert job.fmt == fmt


def test_clip_workflow_suppresses_hands_and_toys_only_in_photo_mode(tmp_path):
    d = tmp_path / "j"
    d.mkdir()
    (d / "plan.json").write_text(json.dumps(_OK_PLAN))
    shot = Plan.model_validate(_OK_PLAN).shots[0]
    photo_meta = {"timings": {}, "models": {}, "shots": {}, "images": [{"shot": 1}]}
    plain = pipeline.Job.__new__(pipeline.Job)
    plain.dir, plain.plan, plain.meta, plain.size = (
        d,
        Plan.model_validate(_OK_PLAN),
        {"timings": {}},
        pipeline.SIZE,
    )
    photo = pipeline.Job.__new__(pipeline.Job)
    photo.dir, photo.plan, photo.meta, photo.size = d, plain.plan, photo_meta, pipeline.SIZE
    wf_plain, wf_photo = (
        pipeline.clip_workflow(plain, shot, "i.png", 7),
        pipeline.clip_workflow(photo, shot, "i.png", 7),
    )
    assert (
        pipeline.PHOTO_NEG not in wf_plain["neg"]["inputs"]["text"]
        and pipeline.PHOTO_SUFFIX not in wf_plain["pos"]["inputs"]["text"]
    )
    assert wf_photo["neg"]["inputs"]["text"].endswith(pipeline.PHOTO_NEG) and wf_photo["pos"]["inputs"][
        "text"
    ].endswith(pipeline.PHOTO_SUFFIX)
    assert (
        wf_photo["ks"]["inputs"]["seed"] == wf_plain["ks"]["inputs"]["seed"] == 7
    )  # 其餘設定完全相同，才能公平比較
    assert wf_photo["neg"]["inputs"]["text"].startswith(
        wf_plain["neg"]["inputs"]["text"]
    )  # 原本的負向提示詞完整保留


def test_nightly_default_plan_is_short_long_xl_everywhere():
    from videomaker import __main__ as cli
    from videomaker import nightly

    assert nightly.NIGHT == ["short", "long", "xl"]
    args = cli.build_parser().parse_args(["nightly"])
    assert args.plan is None and args.kind == "kids"  # 沒帶 --plan 時依 --kind 決定：kids 就是 NIGHT
    assert nightly.PLANS == {
        "kids": ["short", "long", "xl"],
        "trivia": ["short", "long"],
        "kids_english_dialogue": ["short", "short"],
        "animal_sitcom": ["short", "short"],
        "dialogue_animation": ["short", "short"],
    }  # 兒童音樂與小知識維持原樣；對白類每晚 2 支 short
    assert [
        f for plan in nightly.PLANS.values() for f in plan if f not in nightly.FORMATS
    ] == []  # 每個片型都存在


def test_nas_publishing_removed_and_default_stage_is_qc():
    from videomaker import __main__ as cli

    assert pipeline.STAGES[-1] == "qc" and "publish_nas" not in pipeline.FNS
    assert not hasattr(pipeline, "publish_nas")
    assert not hasattr(config, "NAS_TARGET") and not hasattr(config, "NAS_SSH")
    assert cli.build_parser().parse_args(["make", "主題"]).stage == "qc"


@pytest.fixture
def visual_env(tmp_path, monkeypatch):
    from types import SimpleNamespace

    (tmp_path / "clips").mkdir()
    (tmp_path / "clips/01.mp4").write_bytes(b"clip")
    job = SimpleNamespace(
        dir=tmp_path,
        p=lambda *parts: tmp_path.joinpath(*parts),
        plan=SimpleNamespace(shots=[SimpleNamespace(id=1)], layout=[1, 1, 1]),
        meta={},
        windows=lambda: [
            (1, 0.0, 1.0, 0.0),
            (1, 0.0, 1.0, 1.0),
            (1, 0.0, 1.0, 2.0),
        ],  # (鏡頭, 素材起, 素材終, 成片起)
    )
    env = SimpleNamespace(
        job=job, calls=[], prepared=[], scores=[0.1, 0.8], fail_at=None, digest="test-digest"
    )

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    monkeypatch.setattr(visual_qc.Comfy, "prepare", lambda self: env.prepared.append(True))
    monkeypatch.setattr(
        visual_qc.requests,
        "get",
        lambda *a, **k: Response(
            {
                "models": [{"name": visual_qc.MODEL, "digest": env.digest}],
            }
        ),
    )

    def post(url, json, timeout):
        if url.endswith("/api/generate"):
            env.calls.append("unload")
            return Response({})
        count = sum(isinstance(x, dict) for x in env.calls)
        if count == env.fail_at:
            raise visual_qc.requests.ConnectionError("Ollama 暫時離線")
        env.calls.append(json)
        return Response(
            {"answers": {key: {"noul": env.scores[count % len(env.scores)]} for key in json["questions"]}}
        )

    def ffmpeg(args, **kwargs):
        from types import SimpleNamespace

        assert "showinfo" in args[args.index("-vf") + 1]
        for number in (1, 2):
            Path(args[-1] % number).write_bytes(b"jpeg")
        return SimpleNamespace(stderr="n: 0 pts: 0 pts_time:0\nn: 1 pts: 12 pts_time:0.5")

    from pathlib import Path

    monkeypatch.setattr(visual_qc.requests, "post", post)
    monkeypatch.setattr(visual_qc.subprocess, "run", ffmpeg)
    env.response = Response
    return env


def test_visual_qc_samples_unique_clips_and_records_worst_frame(visual_env):
    env = visual_env
    result = visual_qc.check(env.job)
    assert result["status"] == "flagged" and result["checked_frames"] == 2
    assert result["model_digest"] == "test-digest"
    assert result["scores"]["child_safety"] == {
        "score": 0.8,
        "source": "clips/01.mp4",
        "time": 0.5,
        "final_time": 0.5,
    }
    assert len(env.prepared) == 1 and env.calls[-1] == "unload"
    assert all("images" in c and "keep_alive" in c for c in env.calls if isinstance(c, dict))
    saved = json.loads(env.job.p("visual_qc.json").read_text())
    assert len(saved["frames"]) == 2 and saved["questions"] == visual_qc.QUESTIONS
    assert not env.job.p("visual_qc.tmp").exists()


def test_visual_qc_resumes_interrupted_frames_and_skips_completed_results(visual_env):
    env = visual_env
    env.fail_at = 1
    assert visual_qc.check(env.job)["status"] == "error"
    saved = json.loads(env.job.p("visual_qc.json").read_text())
    assert len(saved["frames"]) == 1 and env.calls[-1] == "unload"
    env.fail_at = None
    assert visual_qc.check(env.job)["status"] == "flagged"
    assert sum(isinstance(x, dict) for x in env.calls) == 2  # 不重查第一張
    count = len(env.calls), len(env.prepared)
    assert visual_qc.check(env.job)["status"] == "flagged"
    assert (len(env.calls), len(env.prepared)) == count
    env.job.p("clips/01.mp4").write_bytes(b"regenerated clip")
    visual_qc.check(env.job)
    assert sum(isinstance(x, dict) for x in env.calls) == 4  # 素材變更必須重查
    env.digest = "updated-model"
    result = visual_qc.check(env.job)
    assert result["model_digest"] == "updated-model"
    assert sum(isinstance(x, dict) for x in env.calls) == 6  # 同名模型更新也不能混用舊分數


def test_visual_qc_keeps_saved_frames_when_failing_before_new_fingerprint(visual_env, monkeypatch):
    env = visual_env
    assert visual_qc.check(env.job)["status"] == "flagged"
    before = json.loads(env.job.p("visual_qc.json").read_text())
    online = visual_qc.requests.get

    def offline(*a, **k):
        raise visual_qc.requests.ConnectionError("Ollama 暫時離線")

    monkeypatch.setattr(visual_qc.requests, "get", offline)
    assert visual_qc.check(env.job)["status"] == "error"
    saved = json.loads(env.job.p("visual_qc.json").read_text())
    assert saved["frames"] == before["frames"] and saved["fingerprint"] == before["fingerprint"]
    assert saved["summary"]["scores"] == before["summary"]["scores"]
    monkeypatch.setattr(visual_qc.requests, "get", online)
    assert visual_qc.check(env.job)["status"] == "flagged"
    assert sum(isinstance(x, dict) for x in env.calls) == 2  # 離線期間沒丟掉進度，恢復後不重查


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, None, -0.1, 1.1, "0.2", 10**400])
def test_visual_qc_rejects_invalid_scores_without_passing(visual_env, score):
    env = visual_env
    env.scores = [score]
    result = visual_qc.check(env.job)
    assert result["status"] == "error" and "無效分數" in result["error"]
    assert result["checked_frames"] == 0 and result["scores"] == {}
    assert env.calls[-1] == "unload"


def test_visual_qc_photo_checks_and_final_fallback(visual_env):
    env = visual_env
    env.scores = [0.1]
    env.job.meta["images"] = ["own photo"]
    env.job.p("clips/01.mp4").unlink()
    env.job.p("final.mp4").write_bytes(b"final")
    result = visual_qc.check(env.job)
    assert result["status"] == "passed"
    assert "unexpected_objects" in result["scores"]
    assert result["scores"]["visual_defect"]["source"] == "final.mp4"


def test_visual_qc_memory_guard_prevents_model_request(visual_env, monkeypatch):
    env = visual_env

    def insufficient(self):
        raise RuntimeError("剩餘記憶體 20GB < 40GB，中止")

    monkeypatch.setattr(visual_qc.Comfy, "prepare", insufficient)
    result = visual_qc.check(env.job)
    assert result["status"] == "error" and "40GB" in result["error"]
    assert env.calls == []


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"answers": None},
        {"answers": {"child_safety": {"noul": 0.1}}},
        {"answers": {k: {"noul": 0.1} for k in ("child_safety", "visual_defect", "extra")}},
    ],
)
def test_visual_qc_rejects_incomplete_api_responses(visual_env, monkeypatch, body):
    env = visual_env
    monkeypatch.setattr(
        visual_qc.requests,
        "post",
        lambda url, **kw: env.response({} if url.endswith("/api/generate") else body),
    )
    result = visual_qc.check(env.job)
    assert result["status"] == "error" and result["checked_frames"] == 0


@pytest.mark.parametrize("visual_state", ["passed", "flagged", "error"])
def test_pipeline_qc_requires_visual_completion(tmp_path, monkeypatch, visual_state):
    from types import SimpleNamespace

    job = SimpleNamespace(dir=tmp_path, p=lambda *p: tmp_path.joinpath(*p), fmt="short", meta={})
    probe = {
        "streams": [{"codec_type": "video", "width": 1080, "height": 1920}, {"codec_type": "audio"}],
        "format": {"duration": "20"},
    }
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(probe)))
    monkeypatch.setattr(pipeline, "_measure", lambda *a: {"input_i": -14})
    monkeypatch.setattr(pipeline, "_ff", lambda *a: "")
    monkeypatch.setattr(visual_qc, "check", lambda j: {"status": visual_state, "error": "offline"})
    if visual_state == "error":
        with pytest.raises(RuntimeError, match="視覺 QC 未完成"):
            pipeline._qc(job)
    else:
        pipeline._qc(job)
    status = json.loads(job.p("status.json").read_text())
    assert status["status"] == ("pending_review" if visual_state == "passed" else "failed")
    assert status["checks"]["visual_qc"] is (visual_state == "passed")


@pytest.mark.parametrize(
    "state,visual,rerun",
    [
        ("pending_review", "passed", False),
        ("failed", "flagged", False),
        ("approved", "error", False),
        ("failed", "error", True),
    ],
)
def test_qc_resume_retries_only_incomplete_visual_checks(tmp_path, monkeypatch, state, visual, rerun):
    from contextlib import nullcontext
    from types import SimpleNamespace

    (tmp_path / "status.json").write_text(json.dumps({"status": state, "visual_qc": {"status": visual}}))
    job = SimpleNamespace(p=lambda *p: tmp_path.joinpath(*p), timed=lambda stage: nullcontext())
    calls = []
    monkeypatch.setattr(pipeline, "_qc", lambda j: calls.append(j))
    pipeline.qc(job)
    assert bool(calls) is rerun


def test_visual_qc_checks_only_frames_used_in_final_and_reports_final_time(visual_env):
    env = visual_env
    env.scores = [0.9]
    env.job.windows = lambda: [(1, 0.5, 1.0, 10.0)]  # 成片 10 秒處用到素材 0.5～1.0 秒
    result = visual_qc.check(env.job)
    assert result["checked_frames"] == 1  # 素材 0 秒那張沒進成片，不查
    assert result["scores"]["child_safety"] == {
        "score": 0.9,
        "source": "clips/01.mp4",
        "time": 0.5,
        "final_time": 10.0,
    }


def test_visual_qc_rechecks_when_used_segments_change(visual_env):
    env = visual_env
    visual_qc.check(env.job)
    env.job.windows = lambda: [(1, 0.0, 0.5, 0.0)]  # 重新合成後取用的區段不同
    visual_qc.check(env.job)
    assert sum(isinstance(x, dict) for x in env.calls) == 3  # 2 張 + 重查區段內的 1 張


def test_visual_qc_threshold_change_recomputes_status_without_rechecking(visual_env, monkeypatch):
    env = visual_env
    assert visual_qc.check(env.job)["status"] == "flagged"
    calls = len(env.calls)
    monkeypatch.setattr(visual_qc, "THRESHOLD", 0.9)
    result = visual_qc.check(env.job)
    assert result["status"] == "passed" and result["threshold"] == 0.9
    assert len(env.calls) == calls and len(env.prepared) == 1  # 分數沿用，不碰 GPU


def test_visual_qc_rechecks_when_sampling_settings_change(visual_env, monkeypatch):
    env = visual_env
    visual_qc.check(env.job)
    monkeypatch.setattr(visual_qc, "STATE", "A different prompt.")
    visual_qc.check(env.job)
    assert sum(isinstance(x, dict) for x in env.calls) == 4


def test_visual_qc_multi_source_resume_skips_finished_sources(visual_env, monkeypatch):
    from types import SimpleNamespace

    env = visual_env
    env.job.p("clips/02.mp4").write_bytes(b"clip two")
    env.job.plan = SimpleNamespace(shots=[SimpleNamespace(id=1), SimpleNamespace(id=2)], layout=[1, 2])
    env.job.windows = lambda: [(1, 0.0, 1.0, 0.0), (2, 0.0, 1.0, 5.0)]
    env.fail_at = 2  # 第 3 張（第二支的第一張）失敗
    assert visual_qc.check(env.job)["status"] == "error"
    saved = json.loads(env.job.p("visual_qc.json").read_text())
    assert saved["done"] == ["clips/01.mp4"]
    env.fail_at = None
    extracted, real = [], visual_qc.subprocess.run
    monkeypatch.setattr(
        visual_qc.subprocess,
        "run",
        lambda args, **k: extracted.append(args[args.index("-i") + 1]) or real(args, **k),
    )
    result = visual_qc.check(env.job)
    assert result["checked_frames"] == 4 and extracted == [str(env.job.p("clips/02.mp4"))]  # 不重抽第一支
    assert result["scores"]["child_safety"]["source"] in ("clips/01.mp4", "clips/02.mp4")
    assert {f["final_time"] for f in json.loads(env.job.p("visual_qc.json").read_text())["frames"]} == {
        0.0,
        0.5,
        5.0,
        5.5,
    }


def test_visual_qc_reports_missing_model_by_name(visual_env, monkeypatch):
    env = visual_env
    monkeypatch.setattr(visual_qc.requests, "get", lambda *a, **k: env.response({"models": []}))
    result = visual_qc.check(env.job)
    assert result["status"] == "error" and visual_qc.MODEL in result["error"] and env.calls == []


def test_visual_qc_error_keeps_ffmpeg_stderr(visual_env, monkeypatch):
    env = visual_env

    def broken(args, **kwargs):
        raise visual_qc.subprocess.CalledProcessError(1, args, stderr="moov atom not found")

    monkeypatch.setattr(visual_qc.subprocess, "run", broken)
    result = visual_qc.check(env.job)
    assert result["status"] == "error" and "moov atom not found" in result["error"]


def test_visual_qc_errors_when_ffmpeg_extracts_no_frames(visual_env, monkeypatch):
    env = visual_env
    monkeypatch.setattr(visual_qc.subprocess, "run", lambda args, **k: None)
    result = visual_qc.check(env.job)
    assert result["status"] == "error" and "無法抽取畫面" in result["error"]


def test_visual_qc_result_survives_model_unload_failure(visual_env, monkeypatch):
    env = visual_env
    post = visual_qc.requests.post

    def flaky(url, json, timeout):
        if url.endswith("/api/generate"):
            raise visual_qc.requests.ConnectionError("unload failed")
        return post(url, json=json, timeout=timeout)

    monkeypatch.setattr(visual_qc.requests, "post", flaky)
    assert visual_qc.check(env.job)["status"] == "flagged"


def _qc_job(tmp_path, monkeypatch, duration):
    from types import SimpleNamespace

    job = SimpleNamespace(dir=tmp_path, p=lambda *p: tmp_path.joinpath(*p), fmt="short", meta={})
    probe = {
        "streams": [{"codec_type": "video", "width": 1080, "height": 1920}, {"codec_type": "audio"}],
        "format": {"duration": duration},
    }
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(probe)))
    monkeypatch.setattr(pipeline, "_measure", lambda *a: {"input_i": -14})
    monkeypatch.setattr(pipeline, "_ff", lambda *a: "")
    return job


def test_pipeline_qc_skips_gpu_visual_check_when_basic_checks_fail(tmp_path, monkeypatch):
    job = _qc_job(tmp_path, monkeypatch, "99")  # 超過 short 的長度上限

    def forbidden(j):
        raise AssertionError("基本檢查失敗時不該啟動視覺檢查")

    monkeypatch.setattr(visual_qc, "check", forbidden)
    pipeline._qc(job)
    status = json.loads(job.p("status.json").read_text())
    assert status["status"] == "failed" and status["visual_qc"]["status"] == "not_checked"
    assert status["checks"]["visual_qc"] is False and "models" not in job.meta


@pytest.mark.parametrize("frames,recorded", [(3, ["clef-flash:9b@sha-1"]), (0, None)])
def test_pipeline_qc_records_visual_model_digest_only_when_model_was_used(
    tmp_path, monkeypatch, frames, recorded
):
    job = _qc_job(tmp_path, monkeypatch, "20")
    summary = {"status": "passed", "checked_frames": frames, "model_digest": "sha-1"}
    monkeypatch.setattr(visual_qc, "check", lambda j: summary)
    pipeline._qc(job)
    assert job.meta.get("models", {}).get("visual_qc") == recorded


def test_slot_windows_follow_mux_offsets_and_convert_to_seconds(tmp_path, monkeypatch):
    from types import SimpleNamespace

    (tmp_path / "beats.json").write_text(json.dumps({"cuts": [0, 2, 4, 6], "t0": 0}))
    job = pipeline.Job.__new__(pipeline.Job)
    job.dir = tmp_path
    job.plan = SimpleNamespace(layout=[1, 2, 1], shots=[])
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="5.04"))
    bounds = pipeline.audio.frame_bounds([0, 2, 4, 6], pipeline.FPS)
    assert pipeline.slot_windows(job, bounds) == [
        (1, 0, 48),
        (2, 0, 48),
        (1, 24, 48),
    ]  # 第二次用鏡頭 1 從第 24 幀起
    assert job.windows() == [(1, 0.0, 2.0, 0.0), (2, 0.0, 2.0, 2.0), (1, 1.0, 3.0, 4.0)]


# ---------- 小知識（trivia） ----------
_BODY = (
    "The sea otter is a marine mammal native to the coasts of the northern and eastern North Pacific Ocean. "
    "Adult sea otters typically weigh between 14 and 45 kilograms. Sea otters have the densest fur of any animal, "
    "with up to 150,000 hairs per square centimetre. They use rocks as tools to crack open shellfish. "
) * 8


def _article(text=_BODY, zh=""):
    from videomaker import wiki

    return wiki.Article("Sea otter", "https://en.wikipedia.org/wiki/Sea_otter", 123, text, zh)


def _facts(n=2):
    quotes = [
        "Sea otters have the densest fur of any animal",
        "They use rocks as tools to crack open shellfish",
    ]
    texts = [
        "Sea otters have the densest fur of any animal.",
        "Sea otters use rocks as tools to open shellfish.",
    ]
    return [
        {"text": texts[i % 2] + " " * 0 + ("" if i < 2 else str(i)), "quote": quotes[i % 2]} for i in range(n)
    ]


def test_wiki_fetch_parses_article_and_rejects_unusable_pages(monkeypatch):
    from videomaker import wiki

    def reply(page, variant=None):
        wiki.fetch.cache_clear()

        class R:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self): ...
            def json(self):
                return self.body

        def get(url, params=None, **kw):
            if "zh.wikipedia" in url:  # 中文維基：回台灣正體標題
                return R(
                    {"query": {"pages": {"2": {"title": params["titles"], "varianttitles": variant or {}}}}}
                )
            return R({"query": {"pages": {"1": page}}})

        monkeypatch.setattr(wiki.requests, "get", get)

    good = {
        "title": "Sea otter",
        "fullurl": "https://en.wikipedia.org/wiki/Sea_otter",
        "revisions": [{"revid": 99}],
        "extract": "x" * (wiki.MAX_CHARS + 500),
        "langlinks": [{"lang": "zh", "*": "海獭 (动物)"}],
    }
    reply(good, {"zh-tw": "海獺 (動物)"})
    article = wiki.fetch("sea otter")
    assert article.title == "Sea otter" and len(article.text) == wiki.MAX_CHARS
    assert article.zh_title == "海獺"  # 換成台灣正體、去掉消歧義括號
    reply(good, {})  # 中文維基沒給台灣正體：不給中文名稱，免得把簡體強塞給 LLM
    assert wiki.fetch("sea otter").zh_title == ""
    reply({k: v for k, v in good.items() if k != "langlinks"})
    assert wiki.fetch("sea otter").zh_title == ""
    assert article.permalink == "https://en.wikipedia.org/wiki/Sea_otter?oldid=99"
    for page, message in [
        ({"title": "Nope", "missing": ""}, "沒有條目"),
        ({**good, "pageprops": {"disambiguation": ""}}, "消歧義"),
        ({**good, "extract": "short"}, "太短"),
    ]:
        reply(page)
        with pytest.raises(ValueError, match=message):
            wiki.fetch("x")


def test_check_facts_requires_verbatim_quotes_from_the_article():
    from videomaker.plan import Fact, check_facts

    article = _article()
    good = [Fact(**f) for f in _facts()]
    check_facts(good, article, 2)
    bad = {
        "wrong count": (good[:1], "數量"),
        "invented quote": ([good[0], Fact(text=good[1].text, quote="Sea otters can fly across the whole ocean quickly")], "找不到"),
        "quote too short": ([good[0], Fact(text=good[1].text, quote="sea otter")], "至少 30"),
        "not english": ([good[0], Fact(text="海獺很可愛，牠會用石頭敲開貝殼。" * 2, quote=good[1].quote)], "純英文"),
        "too long": ([good[0], Fact(text="Sea otters " * 20, quote=good[1].quote)], "字元"),
        "number not in quote": ([good[0], Fact(text="Sea otters have 99999 hairs on their fur.", quote=good[1].quote)], "數字"),
        "duplicate": ([good[0], good[0]], "重複"),
        "claim beyond quote": ([good[0], Fact(text="Sea otters use rocks as tools and eat sea urchins.", quote=good[1].quote)], "urchins"),
        "synonym not in quote": ([good[0], Fact(text="Sea otters use rocks as tools to smash shellfish.", quote=good[1].quote)], "smash"),
    }  # fmt: skip
    for facts, message in bad.values():
        with pytest.raises(ValueError, match=message):
            check_facts(facts, article, 2)


def _trivia_reply(facts):
    return {
        **_OK_PLAN,
        "shots": [{"id": i, "prompt": "the otter waves"} for i in range(1, 6)],
        "youtube": {
            "title": "海獺小知識｜Sea Otter Facts for Kids",
            "description": "海獺有最濃密的毛。\n牠們會用石頭敲開貝殼。\n\nSea otters have thick fur.\nThey use rocks as tools to open shells.\n\n#KidsFacts #FunFacts",
            "tags": ["海獺", "小知識", "sea otter", "fun facts for kids", "kids learning"],
        },
        "facts": facts,
        "sources": [{"title": "Made up", "url": "https://evil.example"}],  # LLM 自填的來源不可採信
    }


def test_trivia_make_plan_retries_on_invented_quotes_and_writes_source_itself(monkeypatch):
    from videomaker import plan as planmod

    invented = [
        {
            "text": "Sea otters can fly very fast over the sea.",
            "quote": "Sea otters can fly very fast over the sea.",
        }
    ] * 2
    replies = iter([_trivia_reply(invented), _trivia_reply(_facts())])
    sent = []

    class R:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self): ...
        def json(self):
            return {"choices": [{"message": {"content": json.dumps(self.body)}}]}

    def fake_post(url, json=None, **kw):
        sent.append(json["messages"])
        return R(next(replies))

    monkeypatch.setattr(planmod.requests, "post", fake_post)
    plan = planmod.make_plan("Sea otter", 5, 120, article=_article(), n_facts=2)
    assert len(sent) == 2 and "找不到" in sent[1][-1]["content"]  # 第一次捏造的 quote 被擋，錯誤丟回 LLM
    assert plan.kind == "trivia" and plan.topic == "Sea otter" and len(plan.facts) == 2
    assert [(x.title, x.url) for x in plan.sources] == [
        ("Sea otter", "https://en.wikipedia.org/wiki/Sea_otter?oldid=123")
    ]
    assert "oldid=123" in plan.youtube.description and "CC BY-SA 4.0" in plan.youtube.description
    assert "evil.example" not in plan.youtube.description
    assert (
        "Wikipedia article" in sent[0][1]["content"] and "THIS PLAN IS A KIDS TRIVIA" in sent[0][0]["content"]
    )


def test_fact_slots_split_slots_evenly_and_cover_everything():
    assert pipeline.fact_slots(5, 2) == [(0, 2), (2, 5)]
    assert pipeline.fact_slots(42, 6) == [(i * 7, i * 7 + 7) for i in range(6)]
    spans = pipeline.fact_slots(75, 4)
    assert spans[0][0] == 0 and spans[-1][1] == 75 and all(a[1] == b[0] for a, b in itertools.pairwise(spans))


def _caption_job(tmp_path, facts):
    from types import SimpleNamespace

    from videomaker.plan import Fact

    return SimpleNamespace(
        p=lambda *parts: tmp_path.joinpath(*parts),
        plan=SimpleNamespace(facts=[Fact(**f) for f in facts]),
    )


def test_caption_filters_time_each_fact_to_its_slots(tmp_path, monkeypatch):
    font = tmp_path / "font.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(config, "FONT", str(font))
    job = _caption_job(tmp_path, _facts())
    bounds = [0, 48, 96, 144, 192, 240]  # 5 個時段、每個 2 秒
    chain = pipeline.caption_filters(job, bounds)
    assert chain.count("drawtext=") == 2 and chain.startswith(",")
    assert (
        "gte(t,0.000)*lt(t,4.000)" in chain and "gte(t,4.000)*lt(t,10.000)" in chain
    )  # 前 2 時段、後 3 時段
    assert "expansion=none" in chain  # 字幕裡的 % 不當成 drawtext 語法
    first = (tmp_path / "captions/01.txt").read_text()
    assert "\n" in first and all(len(line) <= pipeline.CAPTION_WIDTH for line in first.splitlines())
    assert pipeline.caption_filters(_caption_job(tmp_path, []), bounds) == ""  # 兒童音樂不加字幕
    monkeypatch.setattr(config, "FONT", str(tmp_path / "missing.ttf"))
    with pytest.raises(RuntimeError, match="字型"):
        pipeline.caption_filters(job, bounds)


def test_new_job_trivia_fetches_article_and_records_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(pipeline.wiki, "fetch", lambda title: _article())
    seen = {}

    def fake_make_plan(topic, n_shots, bpm, long=False, article=None, n_facts=0):
        seen.update(article=article, n_facts=n_facts, n_shots=n_shots)
        return Plan.model_validate(
            {
                **_OK_PLAN,
                "slug": "sea-otter",
                "shots": [{"id": i, "prompt": "the otter waves"} for i in range(1, n_shots + 1)],
            }
        )

    monkeypatch.setattr(pipeline, "make_plan", fake_make_plan)
    job = pipeline.new_job("Sea otter", fmt="long", kind="trivia")
    assert seen["article"].title == "Sea otter" and seen["n_facts"] == 6
    assert job.meta["kind"] == "trivia"
    assert job.meta["wikipedia"] == {
        "title": "Sea otter",
        "url": "https://en.wikipedia.org/wiki/Sea_otter?oldid=123",
        "license": "CC BY-SA 4.0",
    }
    with pytest.raises(ValueError, match="小知識只支援"):
        pipeline.new_job("Sea otter", fmt="xl", kind="trivia")
    with pytest.raises(ValueError, match="小知識只支援"):
        pipeline.new_job("Sea otter", images=[tmp_path], kind="trivia")


def test_suggest_trivia_topics_keeps_only_real_unused_articles(monkeypatch):
    from videomaker import nightly, wiki

    monkeypatch.setattr(
        nightly,
        "_ask_topics",
        lambda prompt, model: ["Sea otter", "Not a page", "Mercury", "Giraffe", 5, "Mario"],
    )

    def fetch(title):
        if title == "Not a page":
            raise ValueError("沒有條目")
        return wiki.Article({"Mercury": "Mercury (planet)"}.get(title, title), "u", 1, "t")

    monkeypatch.setattr(nightly.wiki, "fetch", fetch)
    # 已用過的、查無條目的、禁用字詞（Mario）、非字串都略過；用維基的正式標題（Mercury → Mercury (planet)）
    assert nightly.suggest_trivia_topics(2, ["Sea otter"]) == ["Mercury (planet)", "Giraffe"]


def test_suggest_trivia_topics_gives_up_after_three_attempts(monkeypatch):
    from videomaker import nightly

    asked = []
    monkeypatch.setattr(nightly, "_ask_topics", lambda prompt, model: asked.append(1) or ["Not a page"])
    monkeypatch.setattr(nightly.wiki, "fetch", lambda t: (_ for _ in ()).throw(ValueError("沒有條目")))
    with pytest.raises(RuntimeError, match="小知識主題"):
        nightly.suggest_trivia_topics(2, [])
    assert len(asked) == 3


def test_nightly_trivia_runs_short_and_long_and_records_kind(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(nightly, "suggest_topics", lambda n, avoid: pytest.fail("小知識不該用兒童主題"))
    monkeypatch.setattr(nightly, "suggest_trivia_topics", lambda n, avoid: ["Sea otter", "Giraffe"][:n])
    calls = []

    class FakeJob:
        def __init__(self, topic):
            self.dir = tmp_path / topic
            self.dir.mkdir()
            (self.dir / "status.json").write_text(json.dumps({"status": "pending_review"}))

        def p(self, name):
            return self.dir / name

    def fake_run(topic, fmt="short", resume=False, kind="kids"):
        calls.append((topic, fmt, kind))
        return FakeJob(topic)

    monkeypatch.setattr(nightly.pipeline, "run", fake_run)
    deadline = (datetime.now().astimezone() + timedelta(hours=20)).strftime("%H:%M")
    res = nightly.nightly(deadline=deadline, kind="trivia", sleep=lambda s: None)
    assert calls == [("Sea otter", "short", "trivia"), ("Giraffe", "long", "trivia")]
    assert [r["kind"] for r in res] == ["trivia", "trivia"]
    hist = [json.loads(x) for x in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert {h["kind"] for h in hist} == {"trivia"}


def test_trivia_make_plan_forces_the_wikipedia_chinese_name(monkeypatch):
    from videomaker import plan as planmod

    ok = _trivia_reply(_facts())
    wrong = {**ok, "youtube": {**ok["youtube"], "title": "海海狸小知識｜Sea Otter Facts for Kids"}}
    replies = iter([wrong, ok])
    sent = []

    class R:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self): ...
        def json(self):
            return {"choices": [{"message": {"content": json.dumps(self.body)}}]}

    def fake_post(url, json=None, **kw):
        sent.append(json["messages"])
        return R(next(replies))

    monkeypatch.setattr(planmod.requests, "post", fake_post)
    plan = planmod.make_plan("Sea otter", 5, 120, article=_article(zh="海獺"), n_facts=2)
    assert "use exactly this name" in sent[0][1]["content"] and "中文名稱「海獺」" in sent[1][-1]["content"]
    assert "海獺" in plan.youtube.title and "海獺" in plan.youtube.description
    # 簡體的中文名稱只當提示，不強制
    yt = planmod.Youtube(title="長頸鹿｜Giraffe", description="x", tags=[])
    planmod.check_zh_name(yt, "长颈鹿")
    planmod.check_zh_name(yt, "")


def test_check_facts_word_support_allows_inflection_title_and_short_words():
    from videomaker.plan import Fact, check_facts

    article = _article()
    quote = "They use rocks as tools to crack open shellfish"
    # 詞形變化（rocks／rock 前 5 字母相同）、條目標題的字（Sea otter）、4 字母以下的字與虛詞都不算數
    ok = Fact(text="A sea otter is using a rock as a tool to open shellfish, and it is fun.", quote=quote)
    check_facts([ok, Fact(**_facts()[0])], article, 2)
    glue = Fact(
        text="Sea otters use rocks as tools, which they use to open shellfish.", quote=quote
    )  # which 是虛詞，不比對
    check_facts([glue, Fact(**_facts()[0])], article, 2)


def test_visual_threshold_is_20_percent(visual_env):
    assert visual_qc.THRESHOLD == 0.2  # 實測 38% 的畫面（身體融合）明顯有缺陷，50% 會放行
    env = visual_env
    env.scores = [0.19]
    assert visual_qc.check(env.job)["status"] == "passed"
    env.scores = [0.22]
    env.job.p("clips/01.mp4").write_bytes(b"another clip")  # 換素材才會重查
    assert visual_qc.check(env.job)["status"] == "flagged"


def test_restate_recomputes_finished_results_only():
    scores = {"visual_defect": {"score": 0.3, "source": "clips/01.mp4", "time": 0}}
    old = {"status": "passed", "threshold": 0.5, "scores": scores}
    new = visual_qc.restate(old)
    assert (
        new["status"] == "flagged" and new["threshold"] == visual_qc.THRESHOLD and old["status"] == "passed"
    )
    assert visual_qc.restate({**old, "status": "flagged", "scores": {}})["status"] == "passed"
    for state in ("error", "running", "not_checked"):
        assert visual_qc.restate({"status": state, "scores": scores}) == {"status": state, "scores": scores}


def test_wiki_fetch_is_cached_so_one_night_reads_one_revision(monkeypatch):
    from videomaker import wiki

    wiki.fetch.cache_clear()
    calls = []

    class R:
        def raise_for_status(self): ...
        def json(self):
            page = {
                "title": "Giraffe",
                "fullurl": "u",
                "revisions": [{"revid": len(calls)}],
                "extract": "x" * 2000,
            }
            return {"query": {"pages": {"1": page}}}

    monkeypatch.setattr(wiki.requests, "get", lambda *a, **k: calls.append(1) or R())
    assert wiki.fetch("Giraffe") is wiki.fetch("Giraffe") and len(calls) == 1  # 選主題與產作業用同一個版本
    wiki.fetch.cache_clear()


def test_check_zh_name_forces_the_taiwan_name():
    from videomaker import plan as planmod

    yt = planmod.Youtube(title="海狸小知識｜Sea Otter Facts", description="海狸很可愛", tags=[])
    with pytest.raises(ValueError, match="海獺"):
        planmod.check_zh_name(yt, "海獺")


def test_unsupported_words_does_not_accept_unrelated_long_words_by_prefix():
    from videomaker.plan import unsupported_words

    quote = "Sea otters are internal fertilizers found near a carnival of kelp"
    assert unsupported_words("Sea otters are international swimmers.", quote, "Sea otter") == [
        "international",
        "swimmers",
    ]
    assert unsupported_words("Sea otters are carnivores.", quote, "Sea otter") == ["carnivores"]
    assert unsupported_words("Sea otters are found near kelp.", quote, "Sea otter") == []
    # quote 的字很短（interns，7 字母）也不能讓長字靠前綴通過
    assert unsupported_words("Sea otters are international.", "Sea otters have interns", "Sea otter") == [
        "international"
    ]


def test_trivia_resume_finds_the_job_by_canonical_wikipedia_title(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline.wiki, "fetch", lambda title: _article())  # "sea otters" → "Sea otter"
    seen = []
    job = SimpleNamespace(p=lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr(
        pipeline,
        "find_job",
        lambda topic, fmt, kind: seen.append(topic) or (job if topic == "Sea otter" else None),
    )
    monkeypatch.setattr(pipeline, "new_job", lambda *a, **k: pytest.fail("不該重開新作業"))
    assert pipeline.run("sea otters", dry_run=True, resume=True, kind="trivia") is job
    assert seen == ["sea otters", "Sea otter"]


def test_nightly_rejects_formats_the_kind_does_not_support_before_doing_anything(monkeypatch):
    from videomaker import nightly

    monkeypatch.setattr(nightly, "suggest_trivia_topics", lambda n, avoid: pytest.fail("不該先想主題"))
    with pytest.raises(ValueError, match="xl"):
        nightly.nightly(["short", "xl"], kind="trivia")
    assert nightly.PLANS["trivia"] == nightly.KINDS["trivia"]  # 只有一張表


def test_review_binds_viewed_bytes_and_upload_rejects_changed_or_legacy_approval(tmp_path):
    from videomaker import __main__ as cli
    from videomaker import youtube

    d = _yt_job(tmp_path)
    digest = youtube.video_sha256(d)
    (d / "final.mp4").write_bytes(b"new film")
    with pytest.raises(ValueError, match="成片已變更"):
        cli.main(["approve", str(d), "--sha256", digest])
    service = _FakeYT([])
    with pytest.raises(ValueError, match="重新人工核准"):
        youtube.upload(d, service)
    (d / "status.json").write_text(json.dumps({"status": "approved"}))
    with pytest.raises(ValueError, match="重新人工核准"):
        youtube.upload(d, service)
    assert service.calls == []
    cli.main(["approve", str(d), "--sha256", youtube.video_sha256(d)])
    cli.main(["reject", str(d)])
    assert "approved_sha256" not in json.loads((d / "status.json").read_text())


def test_concurrent_uploads_create_only_one_video(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from videomaker import youtube

    d = _yt_job(tmp_path)
    entered, release, duplicate = Event(), Event(), Event()

    class SlowRequest:
        def next_chunk(self):
            entered.set()
            assert release.wait(5)
            return None, {"id": "one"}

    class Service(_FakeYT):
        def insert(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) > 1:
                duplicate.set()
            return SlowRequest()

    service = Service([])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(youtube.upload, d, service)
        try:
            assert entered.wait(5)
            second = pool.submit(youtube.upload, d, service)
            assert not duplicate.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == "one"
    assert len(service.calls) == 1


def test_upload_keeps_the_reviewed_open_file_if_mux_replaces_path(tmp_path):
    from videomaker import youtube

    d = _yt_job(tmp_path)
    seen = []

    class Service(_FakeYT):
        def videos(self):
            replacement = d / "final.tmp.mp4"
            replacement.write_bytes(b"unreviewed replacement")
            replacement.replace(d / "final.mp4")
            return self

        def insert(self, **kwargs):
            media = kwargs["media_body"]
            seen.append(media.getbytes(0, media.size()))
            return _FakeReq([{"id": "reviewed-film"}])

    assert youtube.upload(d, Service([])) == "reviewed-film"
    assert seen == [b"x" * 1000]
    assert (d / "final.mp4").read_bytes() == b"unreviewed replacement"


def test_resume_filters_kind_and_format_and_orders_by_created_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)

    def job(slug, fmt, kind, created):
        d = tmp_path / slug
        d.mkdir()
        (d / "plan.json").write_text(json.dumps({**_OK_PLAN, "topic": "same", "kind": kind}))
        (d / "meta.json").write_text(json.dumps({"format": fmt, "created_at": created}))
        return d

    job("2026-10-16_same-9", "long", "kids", "2026-10-16T09:00:00+08:00")
    latest = job("2026-10-16_same-10", "long", "kids", "2026-10-16T10:00:00+08:00")
    job("2026-10-16_z", "short", "kids", "2026-10-16T11:00:00+08:00")
    job("2026-10-16_zz", "long", "trivia", "2026-10-16T12:00:00+08:00")
    assert pipeline.run("same", fmt="long", resume=True, dry_run=True).dir == latest
    monkeypatch.setattr(pipeline.wiki, "fetch", lambda title: pytest.fail("既有作業不應依賴網路"))
    assert pipeline.run("same", fmt="long", kind="trivia", resume=True, dry_run=True).plan.kind == "trivia"


@pytest.mark.parametrize("keep_previous", [False, True])
def test_mux_interruption_keeps_previous_final_and_retry_does_not_skip(tmp_path, monkeypatch, keep_previous):
    from contextlib import nullcontext
    from types import SimpleNamespace

    if keep_previous:
        (tmp_path / "final.mp4").write_bytes(b"old final")
    else:
        (tmp_path / "thumb.jpg").write_bytes(b"old thumbnail")
    (tmp_path / "status.json").write_text('{"status":"approved"}')
    (tmp_path / "beats.json").write_text('{"cuts":[0,4],"t0":0}')
    job = SimpleNamespace(
        p=lambda *parts: tmp_path.joinpath(*parts),
        plan=SimpleNamespace(facts=[]),
        timed=lambda stage: nullcontext(),
    )
    monkeypatch.setattr(pipeline, "slot_windows", lambda *a: [(1, 0, 96)])
    monkeypatch.setattr(
        pipeline,
        "_measure",
        lambda *a: dict.fromkeys(["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"], "0"),
    )
    failed = []

    def ff(*args):
        dest = args[-1]
        dest.write_bytes(b"completed")
        if dest.name == "final.tmp.mp4" and not failed:
            failed.append(True)
            raise RuntimeError("encode interrupted")

    monkeypatch.setattr(pipeline, "_ff", ff)
    with pytest.raises(RuntimeError, match="interrupted"):
        pipeline.mux(job)
    if keep_previous:
        assert (tmp_path / "final.mp4").read_bytes() == b"old final"
    else:
        assert not (tmp_path / "final.mp4").exists()
    assert not (tmp_path / "thumb.jpg").exists() and not (tmp_path / "status.json").exists()
    pipeline.mux(job)
    assert (tmp_path / "final.mp4").read_bytes() == b"completed"
    assert (tmp_path / "thumb.jpg").read_bytes() == b"completed"


def test_retry_does_not_start_when_cooling_used_up_remaining_time(monkeypatch):
    from datetime import datetime, timedelta

    from videomaker import nightly

    class Clock(datetime):
        current = datetime.fromisoformat("2026-10-16T06:00:29+08:00")

        @classmethod
        def now(cls, tz=None):
            return cls.current

    calls = []

    def fail(*a, **k):
        calls.append(k)
        raise RuntimeError("temporary failure")

    def sleep(seconds):
        Clock.current += timedelta(seconds=seconds)

    monkeypatch.setattr(nightly, "datetime", Clock)
    monkeypatch.setattr(nightly, "estimate", lambda fmt, kind="kids": 1800)
    monkeypatch.setattr(nightly, "gpu_temp", lambda: 85)
    monkeypatch.setattr(nightly.pipeline, "run", fail)
    result = nightly._run_with_retry("t", "short", datetime.fromisoformat("2026-10-16T07:00:00+08:00"), sleep)
    assert result["status"] == "failed" and len(calls) == 1


@pytest.mark.parametrize("bad", [None, "小貓跳舞", [{"topic": "小貓跳舞"}], [5]])
@pytest.mark.parametrize("kind", ["kids", "trivia"])
def test_bad_topic_types_retry_instead_of_crashing_nightly(monkeypatch, bad, kind):
    from types import SimpleNamespace

    from videomaker import nightly

    answers = iter([bad, ["小貓跳舞"] if kind == "kids" else ["Sea otter"]])
    calls = []

    def post(*a, **k):
        calls.append(True)
        body = {"choices": [{"message": {"content": json.dumps({"topics": next(answers)})}}]}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(nightly.requests, "post", post)
    monkeypatch.setattr(nightly.wiki, "fetch", lambda title: SimpleNamespace(title=title))
    suggest = nightly.suggest_topics if kind == "kids" else nightly.suggest_trivia_topics
    assert suggest(1, []) == (["小貓跳舞"] if kind == "kids" else ["Sea otter"])
    assert len(calls) == 2


# ---------- 對白類：星期路由、夜間保護機制、每日合併 ----------
def _hours_from_now(hours):
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() + timedelta(hours=hours)).strftime("%H:%M")


def test_schedule_routes_each_weekday_to_its_content_kind():
    from datetime import datetime, timedelta

    from videomaker import nightly

    monday = datetime(2026, 10, 19).astimezone()
    assert monday.weekday() == 0
    assert [nightly.kind_for(monday + timedelta(days=i)) for i in range(7)] == [
        "kids",
        "trivia",
        "kids_english_dialogue",
        "animal_sitcom",
        "dialogue_animation",
        None,  # 週末不跑
        None,
    ]
    assert set(nightly.SCHEDULE.values()) == set(nightly.PLANS)  # 每種內容都有排到一天


def test_tonight_auto_picks_the_kind_runs_the_night_then_merges_it(tmp_path, monkeypatch):
    from datetime import datetime

    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    calls = []
    monkeypatch.setattr(
        nightly,
        "nightly",
        lambda plan, dl, dry, sleep, kind: calls.append(("night", kind)) or [{"status": "ok"}],
    )
    merged = [{"type": "merge", "topic": "", "format": "short", "kind": "x", "status": "merged"}]
    monkeypatch.setattr(nightly.weekly, "merge_kind", lambda kind: calls.append(("merge", kind)) or merged)
    wednesday = datetime(2026, 10, 21, 19, 30).astimezone()
    res = nightly.tonight("auto", now=wednesday)
    assert calls == [("night", "kids_english_dialogue"), ("merge", "kids_english_dialogue")]  # 先生成、後合併
    assert [r.get("type", "night") for r in res] == ["night", "merge"]
    hist = [json.loads(x) for x in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert hist == merged  # 合併結果寫進 history
    calls.clear()
    nightly.tonight("auto", now=datetime(2026, 10, 22, 19, 30).astimezone())  # 週四動物短劇也合併
    assert calls == [("night", "animal_sitcom"), ("merge", "animal_sitcom")]
    calls.clear()
    assert (
        nightly.tonight("auto", now=datetime(2026, 10, 24, 19, 30).astimezone()) == [] and calls == []
    )  # 週六
    assert nightly.tonight("kids", dry_run=True) and ("merge", "kids") not in calls  # dry-run 不合併


def test_merge_failure_never_fails_the_night_and_is_recorded(tmp_path, monkeypatch):
    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(nightly, "nightly", lambda *a: [{"status": "pending_review"}])
    monkeypatch.setattr(
        nightly.weekly, "merge_kind", lambda kind: (_ for _ in ()).throw(RuntimeError("ffmpeg 壞了"))
    )
    res = nightly.tonight("trivia")
    assert (
        res[0]["status"] == "pending_review"
        and res[1]["status"] == "failed"
        and "ffmpeg 壞了" in res[1]["error"]
    )
    hist = [json.loads(x) for x in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert (
        hist[0]["type"] == "merge"
        and hist[0]["kind"] == "trivia"
        and "topic" in hist[0]
        and "format" in hist[0]
    )
    # merge 紀錄不會被當成生成作業：不會干擾主題去重
    assert nightly._past_topics() == []


def test_h3_nights_are_skipped_without_planning_while_the_renderer_is_disabled(tmp_path, monkeypatch):
    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(config, "H3_ENABLED", False)
    monkeypatch.setattr(nightly, "suggest_dialogue_topics", lambda *a: pytest.fail("不該想主題"))
    monkeypatch.setattr(nightly.pipeline, "run", lambda *a, **k: pytest.fail("不該跑管線"))
    res = nightly.nightly(kind="kids_english_dialogue", deadline=_hours_from_now(8))
    assert (
        [r["status"] for r in res] == ["skipped", "skipped"]  # 對白類每晚 2 支，各記一筆
        and "未啟用" in res[0]["reason"]
        and res[0]["renderer"] == "h3_fl2va"
    )
    assert json.loads((tmp_path / "history.jsonl").read_text().splitlines()[0])["status"] == "skipped"


def test_h3_night_is_skipped_without_planning_while_weights_are_not_installed(tmp_path, monkeypatch):
    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(config, "H3_ENABLED", True)
    monkeypatch.setattr(nightly.renderers, "missing_weights", lambda name: ["m.safetensors"])
    monkeypatch.setattr(nightly, "suggest_dialogue_topics", lambda *a: pytest.fail("不該想主題"))
    monkeypatch.setattr(nightly.pipeline, "run", lambda *a, **k: pytest.fail("不該跑管線"))
    res = nightly.nightly(kind="animal_sitcom", deadline=_hours_from_now(8))
    assert [r["status"] for r in res] == ["skipped", "skipped"] and "m.safetensors" in res[0]["reason"]
    assert json.loads((tmp_path / "history.jsonl").read_text().splitlines()[0])["status"] == "skipped"


def _h3_night(tmp_path, monkeypatch, run):
    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(config, "H3_ENABLED", True)
    monkeypatch.setattr(nightly.renderers, "missing_weights", lambda name: [])  # 不連真的 ComfyUI
    monkeypatch.setattr(nightly, "gpu_temp", lambda: None)
    monkeypatch.setattr(nightly, "suggest_dialogue_topics", lambda kind, n, avoid: ["甲", "乙"][:n])
    monkeypatch.setattr(nightly.pipeline, "run", run)
    return nightly


class _H3Job:
    def __init__(self, tmp_path, topic, status="pending_review"):
        self.dir = tmp_path / topic
        self.dir.mkdir(exist_ok=True)
        (self.dir / "status.json").write_text(
            json.dumps({"status": status, "checks": {"has_audio": True}, "visual_qc": {"status": "passed"}})
        )
        (self.dir / "final.mp4").write_bytes(b"x")
        shots = {str(i): {"clip_sec": 100.0, "seconds_generated": 5.0} for i in (1, 2, 3)}
        (self.dir / "meta.json").write_text(
            json.dumps(
                {
                    "renderer": "h3_ref2va",
                    "timings": {"keyframes": 0, "clips": 300.0},
                    "shots": shots,
                    "models": {"clips": ["m.safetensors"]},
                }
            )
        )

    def p(self, name):
        return self.dir / name


def test_h3_night_records_renderer_timing_qc_and_per_second_generation_cost(tmp_path, monkeypatch):
    nightly = _h3_night(tmp_path, monkeypatch, lambda topic, fmt, resume, kind: _H3Job(tmp_path, topic))
    (res,) = nightly.nightly(["short"], _hours_from_now(8), kind="animal_sitcom")
    assert res["status"] == "pending_review"  # 夜間排程只到待審核，不會自動核准或公開
    for key in ("start", "end", "elapsed", "retry_count", "output", "qc", "gpu_seconds", "models"):
        assert key in res
    assert (
        res["content_type"] == "animal_sitcom" and res["renderer"] == "h3_ref2va" and res["retry_count"] == 0
    )
    assert res["qc"] == {"status": "pending_review", "checks": {"has_audio": True}, "visual": "passed"}
    assert res["gpu_seconds"] == 300.0 and res["h3_mode"] == "ref2va" and res["shot_count"] == 3
    assert (res["seconds_generated"], res["generation_seconds"], res["gen_sec_per_video_sec"]) == (
        15.0,
        300.0,
        20.0,
    )
    assert json.loads((tmp_path / "history.jsonl").read_text())["gen_sec_per_video_sec"] == 20.0


def test_h3_estimate_starts_conservative_then_follows_recent_history(tmp_path, monkeypatch):
    from videomaker import nightly

    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(config, "H3_DEFAULT_SEC_PER_SEC", 90.0)
    cold = nightly.estimate("short", "animal_sitcom")
    assert cold == pytest.approx((50 * 90 + 360) * 1.15)  # 沒有資料：最長 50 秒 × 保守的每秒 90 秒生成成本
    rates = [10.0, 30.0, 20.0, 999.0]
    lines = [{"renderer": "h3_ref2va", "gen_sec_per_video_sec": r} for r in rates] + [
        {"renderer": "wan", "gen_sec_per_video_sec": 1.0}, {"renderer": "h3_ref2va"}]  # fmt: skip
    (tmp_path / "history.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "garbage\n")
    warm = nightly.estimate("short", "animal_sitcom")
    assert warm < cold
    assert nightly.h3_sec_per_video_sec("h3_ref2va") == sorted(rates)[len(rates) // 2]  # 中位數
    assert nightly.h3_sec_per_video_sec("h3_fl2va") == 90.0  # 沒有資料的 renderer 用預設
    assert nightly.estimate("short", "kids") == nightly.estimate("short", "kids")  # 兒童音樂估算不受影響


def test_h3_night_skips_when_the_estimate_cannot_meet_the_deadline(tmp_path, monkeypatch):
    nightly = _h3_night(tmp_path, monkeypatch, lambda *a, **k: pytest.fail("趕不上 deadline 不該開跑"))
    res = nightly.nightly(["short"], _hours_from_now(1), kind="dialogue_animation")
    assert [r["status"] for r in res] == ["skipped"]
    assert json.loads((tmp_path / "history.jsonl").read_text())["status"] == "skipped"


def test_h3_failure_waits_ten_minutes_resumes_once_then_marks_failed_and_continues(tmp_path, monkeypatch):
    calls = []

    def run(topic, fmt, resume, kind):
        calls.append((topic, resume))
        if topic == "甲":
            raise RuntimeError("H3 掛了")  # 甲兩次都失敗
        if topic == "乙" and not resume:
            raise RuntimeError("記憶體不足")  # 乙第一次失敗、續跑成功
        return _H3Job(tmp_path, topic)

    nightly = _h3_night(tmp_path, monkeypatch, run)
    sleeps = []
    res = nightly.nightly(
        ["short", "short"], _hours_from_now(20), sleep=sleeps.append, kind="kids_english_dialogue"
    )
    assert calls == [("甲", False), ("甲", True), ("乙", False), ("乙", True)]  # 失敗只換下一支，整晚不中止
    assert sleeps == [nightly.RETRY_WAIT, nightly.RETRY_WAIT]
    assert [r["status"] for r in res] == ["failed", "pending_review"]
    assert res[0]["retry_count"] == 1 and "H3 掛了" in res[0]["error"] and res[1]["retry_count"] == 1


def test_dialogue_uploads_stay_private_and_need_manual_approval(tmp_path):
    from tests.test_content import SCRIPTS
    from videomaker import youtube
    from videomaker.content import SPECS, to_plan

    plan = to_plan(SPECS["animal_sitcom"].schema.model_validate(SCRIPTS["animal_sitcom"]()), "x")
    assert youtube.build_body(plan, {})["status"]["privacyStatus"] == "private"
    d = tmp_path / "2026-10-22_sitcom"
    d.mkdir()
    (d / "plan.json").write_text(plan.model_dump_json())
    (d / "meta.json").write_text(json.dumps({"timings": {}}))
    (d / "status.json").write_text(json.dumps({"status": "pending_review"}))
    (d / "final.mp4").write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="必須先人工核准"):
        youtube.upload(d, service=_FakeYT([{"id": "zzz"}]))  # QC 通過也只是待審核，沒有自動上傳
    youtube.set_review(d, "approved")
    svc = _FakeYT([{"id": "vid1", "status": {"privacyStatus": "private"}}])
    assert (
        youtube.upload(d, service=svc) == "vid1"
        and svc.calls[0]["body"]["status"]["privacyStatus"] == "private"
    )


def _merge_job(tmp_path, source_states, status="pending_review"):
    """work/ 底下建好各來源（只有 status.json）與一支合輯；回傳合輯資料夾。"""
    names = []
    for i, state in enumerate(source_states):
        src = tmp_path / f"2026-01-01_src{i}"
        src.mkdir()
        if state:
            (src / "status.json").write_text(json.dumps({"status": state}))
        names.append(src.name)
    merged = tmp_path / "2026-01-02_weekly-short-long"
    merged.mkdir()
    (merged / "meta.json").write_text(json.dumps({"sources": names}))
    (merged / "status.json").write_text(json.dumps({"status": status}))
    (merged / "final.mp4").write_bytes(b"x" * 1000)
    return merged


def test_merge_is_approved_from_sources_only_when_every_source_is_approved(tmp_path):
    from videomaker import youtube

    ready = _merge_job(tmp_path, ["approved", "approved"])
    assert youtube.sources_approved(ready) is True
    youtube.approve_from_sources(ready)
    st = json.loads((ready / "status.json").read_text())
    assert st["status"] == "approved" and st["approved_basis"] == "sources"
    assert st["approved_sha256"] == youtube.video_sha256(ready)  # 仍綁定成片，上傳閘門照舊
    youtube.approve_from_sources(ready)  # 已核准：不動

    # 來源有一支待審核或已不在 GX10 → 不核准
    for i, states in enumerate((["approved", "pending_review"], ["approved", None])):
        (tmp_path / f"w{i}").mkdir()
        part = _merge_job(tmp_path / f"w{i}", states)
        assert youtube.sources_approved(part) is False
        with pytest.raises(ValueError, match="請人工審核"):
            youtube.approve_from_sources(part)
        assert json.loads((part / "status.json").read_text())["status"] == "pending_review"


def test_approve_from_sources_never_overrides_rejected_or_failed_merge_or_plain_jobs(tmp_path):
    from videomaker import youtube

    for i, status in enumerate(("rejected", "failed")):
        (tmp_path / f"w{i}").mkdir()
        d = _merge_job(tmp_path / f"w{i}", ["approved", "approved"], status=status)
        with pytest.raises(ValueError, match="不能依來源核准"):
            youtube.approve_from_sources(d)
        assert json.loads((d / "status.json").read_text())["status"] == status
    (tmp_path / "plain").mkdir()
    plain = _yt_job(tmp_path / "plain", status="pending_review")  # 一般作業沒有 sources
    assert youtube.sources_approved(plain) is None
    with pytest.raises(ValueError, match="不是合輯"):
        youtube.approve_from_sources(plain)


def test_cli_approve_from_sources_then_upload_goes_through_the_normal_gate(tmp_path):
    from videomaker import __main__ as cli
    from videomaker import youtube

    d = _merge_job(tmp_path, ["approved", "pending_review"])
    with pytest.raises(ValueError, match="請人工審核"):
        cli.main(["approve", str(d), "--from-sources"])
    with pytest.raises(ValueError, match="必須先人工核准"):
        youtube.upload(d, service=_FakeYT([]))
    (tmp_path / "2026-01-01_src1" / "status.json").write_text(json.dumps({"status": "approved"}))
    cli.main(["approve", str(d), "--from-sources"])
    assert json.loads((d / "status.json").read_text())["status"] == "approved"
    (d / "plan.json").write_text(
        json.dumps({**_OK_PLAN, "youtube": {"title": "合輯", "description": "d", "tags": ["a"]}})
    )
    svc = _FakeYT([{"id": "vidM", "status": {"privacyStatus": "private"}}])
    assert youtube.upload(d, service=svc) == "vidM"  # 走原本的 approved＋hash 閘門


class _FakeFB:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def request(self, method, url, **kwargs):
        if hasattr(kwargs.get("data"), "read"):
            kwargs["data"] = kwargs["data"].read()
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        status, body = outcome if isinstance(outcome, tuple) else (200, outcome)

        class Response:
            status_code = status

            def json(self):
                return body

        return Response()


@pytest.fixture
def fb_job(tmp_path, monkeypatch):
    from videomaker import facebook

    cred = tmp_path / "credentials"
    cred.mkdir()
    (cred / "facebook.json").write_text(json.dumps({"page_id": "123", "page_access_token": "test-token"}))
    monkeypatch.setattr(config, "FB_DIR", cred)
    monkeypatch.setattr(config, "FB_API_VERSION", "v25.0")
    monkeypatch.setattr(facebook, "_check_video", lambda stream: None)
    monkeypatch.setattr(facebook.time, "sleep", lambda seconds: None)
    return _yt_job(tmp_path)


_FB_START = {"video_id": "456", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/456"}
_FB_READY = {"status": {"video_status": "ready", "publishing_phase": {"publish_status": "draft"}}}


def test_facebook_upload_is_draft_only_hash_bound_and_independent_from_youtube(fb_job):
    from videomaker import facebook

    meta_path = fb_job / "meta.json"
    meta_path.write_text(json.dumps({"youtube_id": "already-on-youtube", "models": {"music": ["ACE-Step"]}}))
    client = _FakeFB([_FB_START, {"success": True}, {"success": True}, _FB_READY])
    assert facebook.upload(fb_job, client) == "456"
    assert client.calls[1][2]["data"] == (fb_job / "final.mp4").read_bytes()
    finish = client.calls[2][2]["data"]
    assert finish["video_state"] == "DRAFT" and finish["is_ai_generated"] == "true"
    assert finish["title"].startswith("小熊刷牙") and len(finish["title"]) <= 100
    assert all(c[2]["allow_redirects"] is False and "access_token" not in c[1] for c in client.calls)
    saved = json.loads(meta_path.read_text())
    assert saved["facebook_reel"]["phase"] == "draft" and saved["youtube_id"] == "already-on-youtube"
    assert saved["models"]["music"] == ["ACE-Step"] and "test-token" not in meta_path.read_text()
    assert facebook.upload(fb_job, _FakeFB([])) == "456"  # 成功重跑不再連線


@pytest.mark.parametrize("change", ["pending_review", "rejected", "failed", "missing_hash", "changed_file"])
def test_facebook_rejects_unapproved_or_changed_video_before_network(fb_job, change):
    from videomaker import facebook

    path = fb_job / "status.json"
    review = json.loads(path.read_text())
    if change == "missing_hash":
        review.pop("approved_sha256")
    elif change == "changed_file":
        (fb_job / "final.mp4").write_bytes(b"changed")
    else:
        review["status"] = change
    path.write_text(json.dumps(review))
    client = _FakeFB([])
    with pytest.raises(ValueError, match="核准"):
        facebook.upload(fb_job, client)
    assert client.calls == []


def test_facebook_timeout_resumes_polling_without_creating_or_uploading_again(fb_job):
    from videomaker import facebook

    pending = {"status": {"video_status": "processing"}}
    client = _FakeFB([_FB_START, {"success": True}, {"success": True}, *[pending] * facebook.POLL_ATTEMPTS])
    with pytest.raises(TimeoutError, match="仍在處理"):
        facebook.upload(fb_job, client)
    assert json.loads((fb_job / "meta.json").read_text())["facebook_reel"]["phase"] == "processing"
    resumed = _FakeFB([_FB_READY])
    assert facebook.upload(fb_job, resumed) == "456"
    assert [call[0] for call in resumed.calls] == ["GET"]


def test_facebook_transfer_failure_resumes_same_id_at_server_offset(fb_job):
    from videomaker import facebook

    client = _FakeFB([_FB_START, requests.Timeout("test-token must not leak")])
    with pytest.raises(RuntimeError, match="連線失敗") as error:
        facebook.upload(fb_job, client)
    assert "test-token" not in str(error.value) and error.value.__suppress_context__
    resumed = _FakeFB(
        [
            {"status": {"uploading_phase": {"status": "in_progress", "bytes_transfered": 200}}},
            {"success": True},
            {"success": True},
            _FB_READY,
        ]
    )
    assert facebook.upload(fb_job, resumed) == "456"
    assert resumed.calls[1][2]["headers"]["offset"] == "200"
    assert resumed.calls[1][2]["data"] == (fb_job / "final.mp4").read_bytes()[200:]
    assert not any(
        c[2].get("data", {}).get("upload_phase") == "start"
        for c in resumed.calls
        if isinstance(c[2].get("data"), dict)
    )


def test_facebook_unknown_start_result_blocks_duplicate_sessions(fb_job):
    from videomaker import facebook

    with pytest.raises(RuntimeError):
        facebook.upload(fb_job, _FakeFB([requests.Timeout("test-token")]))
    with pytest.raises(ValueError, match="結果不明"):
        facebook.upload(fb_job, _FakeFB([]))


@pytest.mark.parametrize(
    "status,phase",
    [
        ({"video_status": "error"}, "failed"),
        ({"video_status": "processing", "processing_phase": {"status": "error"}}, "failed"),
        ({"video_status": "ready", "publishing_phase": {"publish_status": "published"}}, "published"),
    ],
)
def test_facebook_processing_error_is_recorded_as_terminal_phase(fb_job, status, phase):
    from videomaker import facebook

    with pytest.raises(RuntimeError):
        facebook.upload(
            fb_job, _FakeFB([_FB_START, {"success": True}, {"success": True}, {"status": status}])
        )
    assert json.loads((fb_job / "meta.json").read_text())["facebook_reel"]["phase"] == phase
    again = _FakeFB([])
    with pytest.raises(RuntimeError, match="Meta"):
        facebook.upload(fb_job, again)  # 終態不盲目重試，也不連線
    assert again.calls == []


def test_facebook_upload_uses_opened_approved_file_if_path_is_replaced(fb_job, monkeypatch):
    from videomaker import facebook

    original = (fb_job / "final.mp4").read_bytes()

    def replace_path(stream):
        new = fb_job / "new.mp4"
        new.write_bytes(b"not approved")
        new.replace(fb_job / "final.mp4")

    monkeypatch.setattr(facebook, "_check_video", replace_path)
    client = _FakeFB([_FB_START, {"success": True}, {"success": True}, _FB_READY])
    facebook.upload(fb_job, client)
    assert client.calls[1][2]["data"] == original


@pytest.mark.parametrize(
    "url",
    [
        "http://rupload.facebook.com/video-upload/v25.0/456",
        "https://evil.example/video-upload/v25.0/456",
        "https://rupload.facebook.com.evil.example/video-upload/v25.0/456",
        "https://rupload.facebook.com/video-upload/v25.0/456?access_token=secret",
    ],
)
def test_facebook_rejects_upload_urls_that_could_expose_token(fb_job, url):
    from videomaker import facebook

    client = _FakeFB([{**_FB_START, "upload_url": url}])
    with pytest.raises(ValueError, match="網址無效"):
        facebook.upload(fb_job, client)
    assert len(client.calls) == 1


def test_facebook_api_errors_do_not_expose_response_text_or_tokens():
    from videomaker import facebook

    with pytest.raises(RuntimeError, match="code 190") as error:
        facebook._request(
            _FakeFB([{"error": {"code": 190, "message": "secret-token"}}]),
            "POST",
            "https://graph.facebook.com/v25.0/me",
            "secret-token",
        )
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize(
    "duration,width,height,fps,allowed",
    [
        (20, 1080, 1920, "24/1", True),
        (60, 540, 960, "30000/1001", True),
        (168, 1080, 1920, "24/1", False),
        (3, 1080, 1920, "24/1", False),
        (20, 1920, 1080, "24/1", False),
        (20, 540, 960, "22/1", False),
    ],
)
def test_facebook_checks_actual_video_before_upload(
    tmp_path, monkeypatch, duration, width, height, fps, allowed
):
    from videomaker import facebook

    class Probe:
        stdout = json.dumps(
            {
                "format": {"duration": duration},
                "streams": [{"width": width, "height": height, "avg_frame_rate": fps}],
            }
        )

    calls = []
    monkeypatch.setattr(facebook.subprocess, "run", lambda *a, **k: calls.append((a, k)) or Probe())
    path = tmp_path / "final.mp4"
    path.write_bytes(b"test")
    with path.open("rb") as stream:
        if allowed:
            facebook._check_video(stream)
        else:
            with pytest.raises(ValueError, match="4–60"):
                facebook._check_video(stream)
        assert calls[0][1]["pass_fds"] == (stream.fileno(),)


def test_facebook_auth_stores_credentials_outside_project_with_private_permissions(
    tmp_path, monkeypatch, caplog
):
    from videomaker import facebook

    monkeypatch.setattr(config, "FB_DIR", tmp_path / "cred")
    monkeypatch.setattr("builtins.input", lambda prompt: "123")
    monkeypatch.setattr(facebook.getpass, "getpass", lambda prompt: "secret-token")
    facebook.auth()
    path = tmp_path / "cred" / "facebook.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["page_id"] == "123"
    assert "secret-token" not in caplog.text
    monkeypatch.setattr(config, "FB_DIR", config.ROOT)
    with pytest.raises(ValueError, match="專案外"):
        facebook.auth()


def test_cli_facebook_upload_prints_only_video_id(fb_job, monkeypatch, capsys):
    from videomaker import __main__ as cli

    calls = []
    monkeypatch.setattr(cli.facebook, "upload", lambda path: calls.append(path) or "456")
    cli.main(["upload-facebook", str(fb_job)])
    assert calls == [fb_job] and capsys.readouterr().out == "456\n"
    monkeypatch.setattr(cli.facebook, "auth", lambda: calls.append("auth"))
    cli.main(["facebook-auth"])
    assert calls[-1] == "auth"


def test_facebook_rejects_approval_based_on_sources(fb_job):
    from videomaker import facebook

    path = fb_job / "status.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "approved_basis": "sources"}))
    client = _FakeFB([])
    with pytest.raises(ValueError, match="來源"):
        facebook.upload(fb_job, client)  # 來源核准的例外只限 YouTube，FB 必須人工看片
    assert client.calls == []


@pytest.mark.parametrize(
    "status",
    [
        {"video_status": "ready"},
        {"video_status": "ready", "publishing_phase": {"status": "complete"}},
        {"video_status": "ready", "publishing_phase": {"publish_status": "unknown"}},
    ],
)
def test_facebook_requires_positive_draft_confirmation(fb_job, status):
    from videomaker import facebook

    client = _FakeFB(
        [_FB_START, {"success": True}, {"success": True}, *[{"status": status}] * facebook.POLL_ATTEMPTS]
    )
    with pytest.raises(TimeoutError, match="確認"):
        facebook.upload(fb_job, client)
    assert json.loads((fb_job / "meta.json").read_text())["facebook_reel"]["phase"] == "processing"


@pytest.mark.parametrize(
    "outcome",
    [(400, {"error": {"code": 190}}), requests.ConnectTimeout("test-token")],
)
def test_facebook_definite_start_failure_does_not_block_retry(fb_job, outcome):
    from videomaker import facebook

    with pytest.raises(RuntimeError):
        facebook.upload(fb_job, _FakeFB([outcome]))
    assert "facebook_reel" not in json.loads((fb_job / "meta.json").read_text())
    client = _FakeFB([_FB_START, {"success": True}, {"success": True}, _FB_READY])
    assert facebook.upload(fb_job, client) == "456"  # 換好 token 直接重跑，不必手改 meta.json


def test_facebook_server_error_on_start_stays_ambiguous(fb_job):
    from videomaker import facebook

    with pytest.raises(RuntimeError):
        facebook.upload(fb_job, _FakeFB([(500, {"error": {"code": 2}})]))
    with pytest.raises(ValueError, match="結果不明"):
        facebook.upload(fb_job, _FakeFB([]))


def test_facebook_rejects_non_ascii_token_before_network():
    from videomaker import facebook

    with pytest.raises(ValueError, match="token"):
        facebook._validate_credentials({"page_id": "123", "page_access_token": "權杖"})


def test_facebook_keeps_video_id_when_upload_url_is_invalid(fb_job):
    from videomaker import facebook

    bad = {**_FB_START, "upload_url": "https://evil.example/video-upload/v25.0/456"}
    with pytest.raises(ValueError, match="網址無效"):
        facebook.upload(fb_job, _FakeFB([bad]))
    reel = json.loads((fb_job / "meta.json").read_text())["facebook_reel"]
    assert reel["video_id"] == "456" and reel["phase"] == "created"


def test_facebook_finishing_resume_polls_before_resending_finish(fb_job):
    from videomaker import facebook

    lost = requests.Timeout("finish response lost")
    with pytest.raises(RuntimeError):
        facebook.upload(fb_job, _FakeFB([_FB_START, {"success": True}, lost]))
    assert json.loads((fb_job / "meta.json").read_text())["facebook_reel"]["phase"] == "finishing"
    received = _FakeFB([{"status": {"video_status": "processing"}}, _FB_READY])
    assert facebook.upload(fb_job, received) == "456"
    assert all(c[0] == "GET" for c in received.calls)  # Meta 已收到 finish：不重送


def test_facebook_finishing_resume_resends_finish_when_meta_never_got_it(fb_job):
    from videomaker import facebook

    lost = requests.Timeout("finish never arrived")
    with pytest.raises(RuntimeError):
        facebook.upload(fb_job, _FakeFB([_FB_START, {"success": True}, lost]))
    idle = {"status": {"video_status": "uploading", "uploading_phase": {"status": "complete"}}}
    resumed = _FakeFB([idle, {"success": True}, _FB_READY])
    assert facebook.upload(fb_job, resumed) == "456"
    assert [c[0] for c in resumed.calls] == ["GET", "POST", "GET"]


def test_facebook_finished_draft_needs_no_credentials_or_plan(fb_job):
    from videomaker import facebook

    facebook.upload(fb_job, _FakeFB([_FB_START, {"success": True}, {"success": True}, _FB_READY]))
    (config.FB_DIR / "facebook.json").unlink()
    (fb_job / "plan.json").write_text("{")
    client = _FakeFB([])
    assert facebook.upload(fb_job, client) == "456" and client.calls == []


def test_facebook_polls_outside_the_upload_lock_without_clobbering_meta(fb_job):
    import fcntl

    from videomaker import facebook

    free = []

    class Probe(_FakeFB):
        def request(self, method, url, **kwargs):
            if method == "GET":
                with (fb_job / ".upload.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        free.append(True)
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    except BlockingIOError:
                        free.append(False)
                meta = json.loads((fb_job / "meta.json").read_text())
                (fb_job / "meta.json").write_text(json.dumps({**meta, "youtube_id": "uploaded-meanwhile"}))
            return super().request(method, url, **kwargs)

    client = Probe([_FB_START, {"success": True}, {"success": True}, _FB_READY])
    assert facebook.upload(fb_job, client) == "456"
    assert free == [True]  # 輪詢期間 approve／reject／YouTube 上傳不必等
    saved = json.loads((fb_job / "meta.json").read_text())
    assert saved["youtube_id"] == "uploaded-meanwhile" and saved["facebook_reel"]["phase"] == "draft"


def test_facebook_auth_failure_keeps_previous_credentials(tmp_path, monkeypatch):
    from videomaker import facebook

    monkeypatch.setattr(config, "FB_DIR", tmp_path / "cred")
    monkeypatch.setattr(facebook.getpass, "getpass", lambda prompt: "secret-token")
    monkeypatch.setattr("builtins.input", lambda prompt: "123")
    facebook.auth()
    monkeypatch.setattr("builtins.input", lambda prompt: "999")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(facebook.json, "dump", broken)
    with pytest.raises(OSError):
        facebook.auth()
    path = tmp_path / "cred" / "facebook.json"
    assert json.loads(path.read_text())["page_id"] == "123"  # 舊憑證不被截斷
    assert path.stat().st_mode & 0o777 == 0o600


def test_facebook_waits_beyond_two_and_a_half_minutes_for_slow_processing(fb_job):
    from videomaker import facebook

    slow = {"status": {"video_status": "processing"}}  # 實測：處理時間曾超過原本 150 秒的窗口
    client = _FakeFB([_FB_START, {"success": True}, {"success": True}, *[slow] * 45, _FB_READY])
    assert facebook.upload(fb_job, client) == "456"
