"""對白類內容：腳本 schema 驗證、角色庫、renderer 封裝。全部不需要 GPU／ComfyUI／LLM（以 fake 取代）。"""

import copy
import json
import shutil
from types import SimpleNamespace

import pytest

from videomaker import characters, config, content, renderers
from videomaker.plan import Plan

YT = {
    "title": "想吃什麼？｜What Do You Want To Eat Kids English",
    "description": "跟著小熊和小兔學英文。\n每天練習一句話。\n\nLearn easy English with Benny and Rosie.\nSay it out loud and answer.\n\n#KidsEnglish #Learning #Shorts",
    "tags": ["兒童英文", "kids english", "learn english", "english for kids", "英文對話"],
}
STYLE = "flat 2D cartoon, soft pastel colors, kid-friendly"


def _line(who, text):
    return {"speaker": who, "text": text}


def english():
    shots = [
        (1, 5, "bear_01", "What do you want to eat?", "the bear cub smiles in a sunny kitchen"),
        (2, 5, "rabbit_01", "I want pizza!", "the white rabbit holds up a round pizza"),
        (3, 5, "bear_01", "I want noodles!", "the bear cub holds a bowl of noodles"),
        (4, 6, "rabbit_01", "What do YOU want to eat?", "both friends look at the camera and wave"),
    ]
    return {
        "slug": "what-to-eat",
        "style": STYLE,
        "characters": ["bear_01", "rabbit_01"],
        "title": "What do you want to eat?",
        "topic": "What do you want to eat?",
        "learning_goal": "ask and answer what you want to eat",
        "vocabulary": ["pizza", "noodles", "want"],
        "shots": [
            {"shot_id": i, "duration": d, "dialogue": [_line(who, t)], "subtitle": t, "visual_prompt": v}
            for i, d, who, t, v in shots
        ],
        "youtube": YT,
    }


def sitcom():
    def shot(i, d, chars, action, emotion, lines):
        return {
            "shot_id": i, "duration": d, "characters": chars, "action": action, "emotion": emotion,
            "camera": "medium shot, static", "dialogue": [_line(w, t) for w, t in lines],
        }  # fmt: skip

    return {
        "slug": "the-last-snack",
        "style": STYLE,
        "characters": ["husky_01", "husky_02"],
        "episode_title": "The Last Snack",
        "setting": "a cozy living room with a big sofa",
        "hook": "Only one snack is left, and two huskies want it.",
        "shots": [
            shot(1, 6, ["husky_01"], "Meatball stares at the last snack on the coffee table", "greedy", [("husky_01", "Only one snack left.")]),
            shot(2, 6, ["husky_01"], "Meatball sneaks toward the table", "hopeful", [("husky_01", "Nobody is looking.")]),
            shot(3, 7, ["husky_01", "husky_02"], "HappySunny trots in with a big smile", "cheerful", [("husky_02", "Let us share the snack!")]),
            shot(4, 6, ["husky_01"], "Meatball slumps onto the sofa", "crushed", [("husky_01", "Not again.")]),
        ],
        "youtube": YT,
    }  # fmt: skip


def story():
    def shot(i, beats, who, text, action):
        return {
            "shot_id": i, "duration": 8, "characters": ["bear_01", "rabbit_01"], "beats": beats,
            "action": action, "emotion": "curious", "camera": "slow push-in", "visual_prompt": "a sunny meadow",
            "dialogue": [_line(who, text)],
        }  # fmt: skip

    return {
        "slug": "the-lost-balloon",
        "style": STYLE,
        "characters": ["bear_01", "rabbit_01"],
        "title": "The Lost Balloon",
        "synopsis": "Benny loses his red balloon and Rosie helps him find it.",
        "setting": "a sunny meadow",
        "shots": [
            shot(1, ["hook", "setup"], "bear_01", "Oh no, my balloon flew away!", "Benny waves his empty hands"),
            shot(2, ["conflict"], "rabbit_01", "Where did it go?", "Rosie hops around looking up"),
            shot(3, ["reaction"], "bear_01", "It is stuck in the tall tree!", "Benny points at the tree"),
            shot(4, ["punchline"], "rabbit_01", "Then let us ask the owl.", "Rosie smiles and climbs onto a stump"),
        ],
        "youtube": YT,
    }  # fmt: skip


SCRIPTS = {"kids_english_dialogue": english, "animal_sitcom": sitcom, "dialogue_animation": story}


@pytest.mark.parametrize("kind", SCRIPTS)
def test_valid_scripts_pass_and_become_plans(kind):
    script = content.SPECS[kind].schema.model_validate(SCRIPTS[kind]())
    plan = content.to_plan(script, "測試主題")
    assert plan.kind == kind and plan.topic == "測試主題" and plan.music is None
    assert [s.id for s in plan.shots] == [1, 2, 3, 4] and plan.script["shots"][0]["shot_id"] == 1
    assert plan.character == "" and plan.style == STYLE  # 角色描述在每鏡 prompt，不在整支共用欄位
    Plan.model_validate_json(plan.model_dump_json())  # plan.json 能被原本的載入流程讀回


def test_shot_prompt_contains_only_the_characters_in_that_shot():
    plan = content.to_plan(content.AnimalSitcom.model_validate(sitcom()), "x")
    assert "Meatball" in plan.shots[0].prompt and "HappySunny" not in plan.shots[0].prompt
    assert "Meatball" in plan.shots[2].prompt and "HappySunny" in plan.shots[2].prompt


def test_total_duration_is_computed_by_the_program_not_the_llm():
    data = english() | {"duration": 999}
    assert content.EnglishDialogue.model_validate(data).duration == 21.0


def _bad(kind, edit):
    data = SCRIPTS[kind]()
    edit(data)
    with pytest.raises(ValueError) as e:
        content.SPECS[kind].schema.model_validate(data)
    return str(e.value)


def test_invalid_character_id_fails_validation_instead_of_inventing_one():
    msg = _bad("kids_english_dialogue", lambda d: d.update(characters=["bear_01", "ghost_01"]))
    assert "ghost_01" in msg and "不可自創角色" in msg
    msg = _bad("animal_sitcom", lambda d: d["shots"][0].update(characters=["cat_99"]))
    assert "cat_99" in msg or "不在 characters" in msg


@pytest.mark.parametrize("cid", ["../etc", "dog_1", "DOG_01", "dog_01/../../x", ""])
def test_character_ids_cannot_escape_the_registry(cid):
    with pytest.raises(characters.UnknownCharacter):
        characters.load(cid)


def test_english_subtitle_must_match_dialogue_and_final_line_asks_the_viewer():
    assert "subtitle" in _bad("kids_english_dialogue", lambda d: d["shots"][0].update(subtitle="Hello there"))
    # 標點與大小寫不同不算不一致
    ok = english()
    ok["shots"][0]["subtitle"] = "what do you want to eat"
    content.EnglishDialogue.model_validate(ok)
    msg = _bad("kids_english_dialogue", lambda d: d["shots"][3]["dialogue"][0].update(text="I like it."))
    assert "問觀眾" in msg or "subtitle" in msg


def test_english_rules_simple_words_vocabulary_and_two_speakers():
    def long_word(d):
        d["shots"][1]["dialogue"][0]["text"] = "I want extraordinary pizza!"
        d["shots"][1]["subtitle"] = "I want extraordinary pizza!"

    assert "太難" in _bad("kids_english_dialogue", long_word)
    assert "vocabulary" in _bad(
        "kids_english_dialogue", lambda d: d.update(vocabulary=["pizza", "noodles", "banana"])
    )
    only_bear = lambda d: [s["dialogue"][0].update(speaker="bear_01") for s in d["shots"]]
    assert "兩個角色" in _bad("kids_english_dialogue", only_bear)


def test_speech_rate_shot_length_and_total_are_checked():
    assert "5–15 秒" in _bad("animal_sitcom", lambda d: d["shots"][0].update(duration=2))
    assert "總長" in _bad("animal_sitcom", lambda d: [s.update(duration=15) for s in d["shots"]])

    def rushed(d):
        d["shots"][0]["dialogue"] = [_line("husky_01", " ".join(["blah"] * 14))] * 2

    assert "說不完" in _bad("animal_sitcom", rushed)  # 28 詞 / 6 秒 > 3 詞/秒


def test_sitcom_needs_the_husky_leads_action_emotion_camera_and_valid_reference_view():
    only_bears = lambda d: d.update(characters=["bear_01", "rabbit_01"], shots=story()["shots"])
    assert "主角固定" in _bad("animal_sitcom", only_bears)
    assert "action、emotion、camera" in _bad("animal_sitcom", lambda d: d["shots"][1].update(camera=""))
    msg = _bad("animal_sitcom", lambda d: d["shots"][0].update(reference_assets=["husky_01/cry"]))
    assert "cry" in msg
    msg = _bad("animal_sitcom", lambda d: d["shots"][0].update(reference_assets=["husky_02/front"]))
    assert "不在本集角色" in msg  # 第 1 鏡只有 husky_01 在場


def test_default_reference_assets_use_each_characters_front_view():
    script = content.AnimalSitcom.model_validate(sitcom())
    assert script.shots[2].reference_assets == ["husky_01/front", "husky_02/front"]
    assert script.reference_assets == ["husky_01/front", "husky_02/front"]


def test_animation_needs_story_beats_in_order_and_real_action_every_shot():
    assert "beats" in _bad(
        "dialogue_animation", lambda d: d["shots"][3].update(beats=["setup"])
    )  # 倒退且缺 punchline
    assert "beats" in _bad("dialogue_animation", lambda d: d["shots"][0].update(beats=["hook"]))  # 缺 setup
    assert "站著說話" in _bad("dialogue_animation", lambda d: d["shots"][1].update(action=""))

    def solo(d):
        d["characters"] = ["bear_01"]
        for sh in d["shots"]:
            sh["characters"] = ["bear_01"]
            sh["dialogue"][0]["speaker"] = "bear_01"

    assert "至少要兩個角色" in _bad("dialogue_animation", solo)


def test_visual_text_and_banned_brands_are_rejected():
    assert "文字類物件" in _bad(
        "dialogue_animation", lambda d: d["shots"][0].update(visual_prompt="a poster with words")
    )
    assert "禁用" in _bad(
        "animal_sitcom", lambda d: d["shots"][0]["dialogue"][0].update(text="Hello Mickey!")
    )


# ---------- make_script：驗證失敗重寫 3 次 ----------
class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls, self.unloaded = list(replies), [], 0

    def post(self, url, json=None, timeout=None):
        if url.endswith("/api/generate"):
            self.unloaded += 1
            return SimpleNamespace(raise_for_status=lambda: None)
        self.calls.append(copy.deepcopy(json))  # make_script 會原地追加 messages，記錄當下的快照
        reply = self.replies.pop(0)
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": reply}}]}
        )


def test_make_script_feeds_errors_back_and_retries_then_succeeds(monkeypatch):
    bad = english()
    bad["characters"] = ["ghost_01"]
    llm = FakeLLM([json.dumps(bad), "not json", json.dumps(english())])
    monkeypatch.setattr(content.requests, "post", llm.post)
    plan = content.make_script("kids_english_dialogue", "想吃什麼")
    assert plan.kind == "kids_english_dialogue" and len(llm.calls) == 3
    assert "ghost_01" in llm.calls[1]["messages"][-1]["content"]  # 驗證錯誤原文丟回給 LLM
    assert llm.unloaded == 1  # 企劃完成就卸載 LLM
    assert "bear_01" in llm.calls[0]["messages"][0]["content"]  # 提示詞列出可用角色


def test_make_script_gives_up_after_three_invalid_replies(monkeypatch):
    llm = FakeLLM(["{}", "{}", "{}"])
    monkeypatch.setattr(content.requests, "post", llm.post)
    with pytest.raises(RuntimeError, match="重試 3 次"):
        content.make_script("animal_sitcom", "x")
    assert len(llm.calls) == 3 and llm.unloaded == 1


def test_make_script_requires_bilingual_youtube_fields(monkeypatch):
    bad = english()
    bad["youtube"] = {"title": "只有中文", "description": "只有中文。", "tags": ["兒童"]}
    llm = FakeLLM([json.dumps(bad), json.dumps(english())])
    monkeypatch.setattr(content.requests, "post", llm.post)
    content.make_script("kids_english_dialogue", "想吃什麼")
    assert "youtube.title" in llm.calls[1]["messages"][-1]["content"]


# ---------- Plan 相容性 ----------
def test_plan_music_is_still_required_for_music_kinds_and_old_plans_load():
    plan = {
        "topic": "t", "slug": "t", "style": "flat", "character": "a round brown bear cub with a red scarf",
        "shots": [{"id": 1, "prompt": "waving"}],
    }  # fmt: skip
    with pytest.raises(ValueError, match="music"):
        Plan.model_validate(plan)
    music = {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"}
    old = Plan.model_validate(plan | {"music": music})
    assert old.kind == "kids" and old.script == {} and old.music.bpm == 120


def test_dialogue_plans_skip_music_kid_rules_but_music_kinds_keep_them():
    # 兒童音樂規則禁止 mouth/water 等字眼；對白類的角色會開口說話，不套用
    script = sitcom()
    script["shots"][0]["action"] = "Meatball talks with a happy open mouth beside a water bowl"
    content.to_plan(content.AnimalSitcom.model_validate(script), "x")
    music = {"caption": "x", "bpm": 120, "time_signature": "4/4", "key": "C major"}
    with pytest.raises(ValueError, match="高風險"):
        Plan(topic="t", slug="t", style="flat", character="a round brown bear cub with a red scarf", music=music,
             shots=[{"id": 1, "prompt": "bear near water"}])  # fmt: skip


# ---------- 角色庫 ----------
def test_shipped_characters_load_and_have_english_only_prompts():
    assert {"dog_01", "dog_02", "bear_01", "rabbit_01", "husky_01", "husky_02"} <= set(characters.ids())
    for cid in characters.ids():
        c = characters.load(cid)
        assert c.id == cid and c.appearance.isascii() and c.voice.isascii() and "front" in c.references


def test_character_build_generates_only_missing_references_and_never_overwrites(tmp_path, monkeypatch):
    shutil.copytree(
        config.CHARACTERS / "bear_01", tmp_path / "bear_01", ignore=shutil.ignore_patterns("*.png")
    )
    monkeypatch.setattr(config, "CHARACTERS", tmp_path)
    (tmp_path / "bear_01" / "front.png").write_bytes(b"keep")  # 已有的參考圖（人工挑過）不能被覆蓋
    runs = []

    class FakeComfy:
        def prepare(self):
            pass

        def free(self):
            pass

        def upload(self, path, name):
            return f"up_{path.name}"

        def run(self, wf, dest, timeout=0):
            runs.append((dest.name, wf))
            dest.write_bytes(b"new")

    made = characters.build(comfy=FakeComfy())
    assert [r[0] for r in runs] == ["happy.png"] and [p.name for p in made] == ["happy.png"]
    assert (tmp_path / "bear_01" / "front.png").read_bytes() == b"keep"
    assert characters.build(comfy=FakeComfy()) == []  # 第二次什麼都不產
    wf = runs[0][1]  # 非基準視角：從 front 編輯而來，只餵一張圖
    assert wf["img1"]["inputs"]["image"] == "up_front.png" and "img2" not in wf
    assert "image2" not in wf["pos"]["inputs"] and "image2" not in wf["neg"]["inputs"]
    assert "same character" in wf["pos"]["inputs"]["prompt"] and "joyful" in wf["pos"]["inputs"]["prompt"]


def test_character_build_makes_the_base_view_first_by_text_to_image_and_salt_changes_the_seed(
    tmp_path, monkeypatch
):
    shutil.copytree(
        config.CHARACTERS / "bear_01", tmp_path / "bear_01", ignore=shutil.ignore_patterns("*.png")
    )
    monkeypatch.setattr(config, "CHARACTERS", tmp_path)
    seen = []

    class FakeComfy:
        def prepare(self):
            pass

        def free(self):
            pass

        def upload(self, path, name):
            assert path.exists()  # 編輯用的 front 必須已經產生
            return name

        def run(self, wf, dest, timeout=0):
            seen.append(
                (
                    dest.name,
                    "img1" in wf,
                    [n["inputs"].get("seed") for n in wf.values() if "seed" in n["inputs"]],
                )
            )
            dest.write_bytes(b"new")

    characters.build(comfy=FakeComfy())
    characters.build(["bear_01"], force=True, comfy=FakeComfy(), salt=1)
    first, second = seen[:2], seen[2:]
    assert [(n, edit) for n, edit, _ in first] == [
        ("front.png", False),
        ("happy.png", True),
    ]  # front 先、文字生圖
    assert all(a[2] != b[2] for a, b in zip(first, second, strict=True))  # 換 salt 換 seed
    char = characters.load("bear_01")
    assert (
        characters._seed(char, "front", 0)
        == characters._seed(char, "front", 0)
        != characters._seed(char, "front", 2)
    )


# ---------- renderer ----------
def test_frames_snap_up_to_the_17k_plus_5_grid():
    assert [renderers.frames(s) for s in (5, 5.17, 6, 10, 15)] == [124, 124, 158, 243, 362]
    assert all((renderers.frames(s) - 5) % 17 == 0 for s in (5, 6.3, 7.7, 12.1, 15))


def test_h3_is_enabled_by_default_can_be_switched_off_and_wan_is_always_available(monkeypatch):
    assert config.H3_ENABLED  # 2026-10-22 使用者放行：預設啟用
    monkeypatch.setattr(config, "H3_ENABLED", False)
    assert (
        renderers.enabled("wan") and not renderers.enabled("h3_fl2va") and not renderers.enabled("h3_ref2va")
    )
    with pytest.raises(renderers.RendererDisabled, match="VMK_H3_ENABLED"):
        renderers.require("h3_ref2va")
    monkeypatch.setattr(config, "H3_ENABLED", True)
    renderers.require("h3_fl2va")
    with pytest.raises(ValueError, match="沒有音訊"):
        renderers.get("wan")  # Wan 沒有音訊，不能做對白類


def test_missing_weights_lists_only_this_renderers_files_and_tolerates_comfy_being_down(monkeypatch):
    info = {
        "UNETLoader": {"input": {"required": {"unet_name": [["other.safetensors"]]}}},
    }
    monkeypatch.setattr(
        renderers,
        "missing_models",
        lambda i: [("clip_h3_fl2va", "a"), ("clip_h3_ref2va", "b"), ("keyframe", "c")],
    )
    monkeypatch.setattr(renderers.requests, "get", lambda *a, **k: SimpleNamespace(json=lambda: info))
    assert renderers.missing_weights("h3_fl2va") == ["a"] and renderers.missing_weights("wan") == []

    def down(*a, **k):
        raise renderers.requests.ConnectionError("down")

    monkeypatch.setattr(renderers.requests, "get", down)
    assert renderers.missing_weights("h3_fl2va") == []  # 連不上 ComfyUI：交給後面的階段報錯


def test_default_renderers_and_env_override(monkeypatch):
    assert [content.renderer_for(k) for k in ("kids", "trivia")] == ["wan", "wan"]
    assert content.renderer_for("kids_english_dialogue") == "h3_fl2va"
    assert content.renderer_for("animal_sitcom") == "h3_ref2va"
    assert content.renderer_for("dialogue_animation") == "h3_fl2va"
    monkeypatch.setenv("VMK_RENDERER_ANIMAL_SITCOM", "h3_fl2va")
    assert content.renderer_for("animal_sitcom") == "h3_fl2va"


def test_h3_prompt_lists_each_line_with_voice_and_ref2va_maps_pictures():
    plan = content.to_plan(content.AnimalSitcom.model_validate(sitcom()), "x")
    shot = content.ScriptShot.model_validate(plan.script["shots"][2])
    ref = renderers.RENDERERS["h3_ref2va"].prompt(plan.script, shot)
    assert "<Picture 1> (front view)" in ref and "<Picture 2> (front view)" in ref
    assert (
        'HappySunny (a bright, bubbly, high-energy young female voice, sweet and cheerful like a young girl) says clearly: "Let us share the snack!"'
        in ref
    )
    assert "no background music" in ref
    fl = renderers.RENDERERS["h3_fl2va"].prompt(plan.script, shot)
    assert (
        "<Picture" not in fl and "Meatball" in fl and "HappySunny" in fl
    )  # fl2va 靠關鍵幀，提示詞直接描述外觀


class FakeComfy:
    def __init__(self):
        self.uploads, self.workflows = [], []

    def upload(self, path, name):
        self.uploads.append(name)
        return name

    def run(self, wf, dest, timeout=0):
        self.workflows.append(wf)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"clip")


def _job(tmp_path, kind="animal_sitcom"):
    plan = content.to_plan(content.SPECS[kind].schema.model_validate(SCRIPTS[kind]()), "x")
    d = tmp_path / "2026-10-22_job"
    (d / "keyframes").mkdir(parents=True)
    for s in plan.shots:
        (d / "keyframes" / f"{s.id:02d}.png").write_bytes(b"png")
    return SimpleNamespace(dir=d, plan=plan, meta={"models": {}, "shots": {}}, p=lambda *a: d.joinpath(*a))


def test_h3_fl2va_render_uploads_the_keyframe_and_fills_length_seed_and_prompt(tmp_path):
    job, c = _job(tmp_path, "kids_english_dialogue"), FakeComfy()
    info = renderers.RENDERERS["h3_fl2va"].render(job, c, job.plan.shots[0])
    wf = c.workflows[0]
    assert c.uploads == ["2026-10-22_job_01.png"] and wf["img"]["inputs"]["image"] == "2026-10-22_job_01.png"
    assert wf["h3"]["inputs"]["length"] == 124 and wf["noise"]["inputs"]["noise_seed"] == job.plan.seed + 1
    assert "What do you want to eat?" in wf["h3"]["inputs"]["prompt"]
    assert (
        wf["h3"]["inputs"]["width"] == config.H3_SIZE[0]
        and wf["fit"]["inputs"]["height"] == config.H3_SIZE[1]
    )
    assert (
        info["renderer"] == "h3_fl2va"
        and info["frames"] == 124
        and info["seconds_generated"] == pytest.approx(5.167, abs=0.01)
    )
    assert (job.dir / "clips" / "01.mp4").exists()


def test_h3_ref2va_render_attaches_only_the_shots_references(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHARACTERS", tmp_path / "chars")
    shutil.copytree(
        characters.config.ROOT / "characters", tmp_path / "chars", ignore=shutil.ignore_patterns("*.png")
    )
    for cid in ("husky_01", "husky_02"):
        (tmp_path / "chars" / cid / "front.png").write_bytes(b"png")
    job, c = _job(tmp_path), FakeComfy()
    r = renderers.RENDERERS["h3_ref2va"]
    assert r.missing_refs(job) == []
    r.render(job, c, job.plan.shots[0])  # 第 1 鏡只有 husky_01
    wf = c.workflows[0]
    assert len(c.uploads) == 1 and "ref1" not in wf and "ref2" not in wf and "ref0" in wf
    assert not any(k.startswith("ref_images.ref_image_1") for n in wf.values() for k in n["inputs"])
    c.uploads.clear()
    r.render(job, c, job.plan.shots[2])  # 第 3 鏡兩個角色
    wf = c.workflows[1]
    assert len(c.uploads) == 2 and "ref1" in wf and "ref2" not in wf
    assert wf["h3"]["inputs"]["ref_images.ref_image_1"] == ["ref1", 0]


def test_ref2va_reports_missing_reference_images_before_any_gpu_work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHARACTERS", tmp_path / "chars")
    shutil.copytree(
        characters.config.ROOT / "characters", tmp_path / "chars", ignore=shutil.ignore_patterns("*.png")
    )
    job = _job(tmp_path)
    assert renderers.RENDERERS["h3_ref2va"].missing_refs(job) == ["husky_01/front", "husky_02/front"]
    assert renderers.RENDERERS["h3_fl2va"].missing_refs(job) == []  # fl2va 不用參考圖


def test_workflow_map_h3_entries_are_gated_and_consistent(monkeypatch):
    """H3 workflow 的節點參照與欄位對應齊全；關閉 H3 時 missing_models 不檢查，啟用時才檢查。"""
    import yaml

    from videomaker import comfy

    spec = yaml.safe_load((config.ROOT / "workflow_map.yaml").read_text())
    assert spec["clip_h3_fl2va"]["gate"] == spec["clip_h3_ref2va"]["gate"] == "h3"
    info = {
        k: {"input": {"required": {f: [["x.safetensors"]]}}}
        for k, f in comfy.LOADERS.items()
    }  # fmt: skip
    monkeypatch.setattr(config, "H3_ENABLED", False)
    assert not any(n.startswith("clip_h3") for n, _ in comfy.missing_models(info))
    monkeypatch.setattr(config, "H3_ENABLED", True)
    assert {n for n, _ in comfy.missing_models(info) if n.startswith("clip_h3")} == {
        "clip_h3_fl2va",
        "clip_h3_ref2va",
    }  # 權重沒裝好：check 會列出


def test_cli_accepts_new_kinds_and_lists_character_reference_status(tmp_path, monkeypatch, capsys):
    from videomaker import __main__ as cli

    ap = cli.build_parser()
    assert ap.parse_args(["nightly", "--kind", "auto"]).kind == "auto"
    assert ap.parse_args(["nightly"]).kind == "kids"  # 預設維持舊行為
    assert ap.parse_args(["make", "x", "--kind", "animal_sitcom"]).kind == "animal_sitcom"
    with pytest.raises(SystemExit):
        ap.parse_args(["make", "x", "--kind", "unknown"])
    shutil.copytree(
        config.CHARACTERS / "bear_01", tmp_path / "bear_01", ignore=shutil.ignore_patterns("*.png")
    )
    monkeypatch.setattr(config, "CHARACTERS", tmp_path)
    cli.main(["characters", "list"])
    assert "缺參考圖：front、happy" in capsys.readouterr().out
    for name in ("front.png", "happy.png"):
        (tmp_path / "bear_01" / name).write_bytes(b"png")
    cli.main(["characters", "list"])
    assert "bear_01 參考圖齊全" in capsys.readouterr().out


def test_reference_assets_default_to_front_or_first_view_and_are_capped_at_three(tmp_path, monkeypatch):
    shutil.copytree(config.CHARACTERS, tmp_path / "chars", ignore=shutil.ignore_patterns("*.png"))
    ref = tmp_path / "chars" / "bear_01" / "character.json"
    data = json.loads(ref.read_text())
    data["references"] = {"happy": "happy.png"}  # 沒有 front
    ref.write_text(json.dumps(data))
    monkeypatch.setattr(config, "CHARACTERS", tmp_path / "chars")
    script = content.EnglishDialogue.model_validate(english())
    assert script.shots[0].reference_assets == ["bear_01/happy"]  # 取第一個視角，不會變成空清單
    msg = _bad(
        "animal_sitcom",
        lambda d: d["shots"][2].update(
            reference_assets=["husky_01/front", "husky_01/sad", "husky_01/happy", "husky_02/front"]
        ),
    )
    assert "最多 3 張" in msg
    with pytest.raises(ValueError):  # 角色至少要有一個參考圖視角
        characters.Character.model_validate({**data, "references": {}})


def test_make_script_treats_a_malformed_llm_response_as_one_attempt(monkeypatch):
    good = FakeLLM([json.dumps(english())])
    sent = []

    def post(url, json=None, timeout=None):
        if url.endswith("/api/generate"):
            return good.post(url, json=json)
        sent.append(1)
        if len(sent) == 1:  # 第一次：200 但 choices 為空
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"choices": []})
        return good.post(url, json=json)

    monkeypatch.setattr(content.requests, "post", post)
    plan = content.make_script("kids_english_dialogue", "想吃什麼")
    assert plan.kind == "kids_english_dialogue" and len(sent) == 2  # 壞回應算一次嘗試，不讓整支失敗


def test_ref2va_refuses_a_shot_without_references(tmp_path):
    job, c = _job(tmp_path), FakeComfy()
    job.plan.script["shots"][0]["reference_assets"] = []
    with pytest.raises(RuntimeError, match="1–3 張"):
        renderers.RENDERERS["h3_ref2va"].render(job, c, job.plan.shots[0])
    assert c.workflows == []  # 沒有送出 workflow


def test_character_build_frees_comfyui_even_when_a_generation_fails(tmp_path, monkeypatch):
    shutil.copytree(
        config.CHARACTERS / "bear_01", tmp_path / "bear_01", ignore=shutil.ignore_patterns("*.png")
    )
    monkeypatch.setattr(config, "CHARACTERS", tmp_path)
    events = []

    class Boom:
        def upload(self, path, name):
            return name

        def prepare(self):
            events.append("prepare")

        def free(self):
            events.append("free")

        def run(self, wf, dest, timeout=0):
            dest.write_bytes(b"ok") if not events.count("run") else None
            events.append("run")
            if events.count("run") == 2:
                raise RuntimeError("逾時")

    with pytest.raises(RuntimeError, match="逾時"):
        characters.build(comfy=Boom())
    assert events[-1] == "free" and (tmp_path / "bear_01" / "front.png").exists()  # 已產生的保留，並且有卸載


def test_animal_sitcom_cast_is_exactly_the_two_husky_leads():
    def solo(d):  # 只剩 Meatball：第 3 鏡也改成他自己說話
        d["characters"] = ["husky_01"]
        d["shots"][2].update(characters=["husky_01"], dialogue=[_line("husky_01", "Let us share the snack!")])

    assert "主角固定" in _bad("animal_sitcom", solo)
    assert "主角固定" in _bad(
        "animal_sitcom", lambda d: d.update(characters=["husky_01", "husky_02", "dog_01"])
    )


def test_catalog_shows_leads_only_to_animal_sitcom():
    assert "husky_01" in content.catalog("animal_sitcom") and "bear_01" not in content.catalog(
        "animal_sitcom"
    )
    for kind in ("kids_english_dialogue", "dialogue_animation"):
        assert "husky" not in content.catalog(kind) and "bear_01" in content.catalog(kind)
    assert "photorealistic" in content.system_prompt("animal_sitcom")
    assert "flat 2D cartoon" in content.system_prompt("dialogue_animation")


def test_animal_sitcom_prompt_has_craft_rules():
    # 寫作手藝規則只給動物短劇；其他對白類不套用
    assert "Craft rules" in content.system_prompt("animal_sitcom")
    assert "Craft rules" not in content.system_prompt("kids_english_dialogue")
