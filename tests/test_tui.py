"""jobs.py（名稱檢查、盤點、兩處合併、下載、刪除）與 TUI（用 Textual 無頭模式實際按鍵）的測試。"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from videomaker import config, jobs, tui

GOOD = ["2026-10-05_feng-ling-qing-xiang", "2026-10-05_xiao-xiong-shua-ya-2", "2026-10-06_a"]
DIGEST = "a" * 64
BAD = [
    "../x", "..", "work", "2026-10-05_", "2026-10-05_a b", "2026-10-05_a;rm -rf ~", "2026-10-05_A",
    "2026-10-05_a/../../etc", "2026-10-05_a\n", "2026-10-05_$(id)", "2026-10-05_a--b", "/etc/passwd", "",
]  # fmt: skip


@pytest.mark.parametrize("name", GOOD)
def test_valid_job_names(name):
    assert jobs.valid(name)


@pytest.mark.parametrize("name", BAD)
def test_invalid_job_names_rejected(name):
    assert not jobs.valid(name)


def test_destructive_and_download_calls_reject_bad_names_without_running_anything(monkeypatch):
    called = []
    monkeypatch.setattr(jobs, "_sh", lambda *a, **k: called.append(a) or None)
    monkeypatch.setattr(jobs, "_gx", lambda *a, **k: called.append(a) or None)
    for bad in BAD:
        with pytest.raises(ValueError, match="不合法"):
            jobs.delete_gx10(bad)
        with pytest.raises(ValueError, match="不合法"):
            jobs.delete_local(bad)
        with pytest.raises(ValueError, match="不合法"):
            jobs.download(bad)
    with pytest.raises(ValueError, match="不允許"):
        jobs.remote_cli("approve", "2026-10-05_a;rm -rf ~")
    with pytest.raises(ValueError, match="不允許"):
        jobs.remote_cli("shell")  # 只允許四個審核／上傳子指令
    assert called == []  # 一個遠端指令都沒有送出


class _P:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_delete_commands_target_exactly_the_named_folder(monkeypatch):
    gx, sh = [], []
    monkeypatch.setattr(jobs, "_gx", lambda remote, timeout=120: gx.append(remote) or _P())
    monkeypatch.setattr(jobs, "_sh", lambda cmd, timeout=120: sh.append(cmd) or _P())
    jobs.delete_gx10(GOOD[0])
    assert gx == [f"rm -r ~/code/videoMaker/work/{GOOD[0]}"] and sh == []
    monkeypatch.setattr(jobs, "_gx", lambda remote, timeout=120: _P(1, err="Permission denied"))
    with pytest.raises(RuntimeError, match="Permission denied"):
        jobs.delete_gx10(GOOD[0])


def test_delete_local_removes_only_named_download_and_preserves_symlink_targets(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    monkeypatch.setattr(jobs, "LOCAL", downloads)
    target = downloads / GOOD[0]
    target.mkdir(parents=True)
    (target / "final.mp4").write_bytes(b"video")
    (target / "plan.json").write_text("{}")
    outside = tmp_path / "gx10-work"
    outside.mkdir()
    (outside / "final.mp4").write_bytes(b"original")
    (target / "external").symlink_to(outside, target_is_directory=True)
    other = downloads / GOOD[1]
    other.mkdir()
    (other / "final.mp4").write_bytes(b"other")

    jobs.delete_local(GOOD[0])

    assert not target.exists()
    assert (outside / "final.mp4").read_bytes() == b"original"
    assert (other / "final.mp4").read_bytes() == b"other"


def test_delete_local_refuses_symlink_as_job_directory(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(jobs, "LOCAL", downloads)
    outside = tmp_path / "original"
    outside.mkdir()
    (outside / "final.mp4").write_bytes(b"original")
    target = downloads / GOOD[0]
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        jobs.delete_local(GOOD[0])

    assert target.is_symlink() and (outside / "final.mp4").read_bytes() == b"original"


def test_inventory_marks_active_and_unfinished_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)

    def mk(name, status=None, final=False, old=False, vid=None):
        d = tmp_path / name
        d.mkdir()
        (d / "plan.json").write_text(json.dumps({"topic": "主題" + name, "youtube": {"title": "標題"}}))
        (d / "meta.json").write_text(json.dumps({"format": "long", **({"youtube_id": vid} if vid else {})}))
        if status:
            (d / "status.json").write_text(json.dumps({"status": status, "details": {"duration": 168.0}}))
        if final:
            (d / "final.mp4").write_bytes(b"x" * 2048)
        if old:
            t = time.time() - 3600
            for f in d.rglob("*"):
                os.utime(f, (t, t))
        return d

    mk("done", "approved", final=True, vid="abc")
    mk("running")  # 沒有 status.json、剛有檔案更新 → 還在跑
    mk("stale", old=True)  # 沒有 status.json、1 小時前就沒動 → 中途失敗，不算進行中
    by = {j["name"]: j for j in jobs.inventory()}
    assert by["done"] | {} == by["done"] and by["done"]["status"] == "approved" and by["done"]["size"] == 2048
    assert (
        by["done"]["youtube_id"] == "abc"
        and by["done"]["duration"] == 168.0
        and by["done"]["active"] is False
    )
    assert by["running"]["active"] is True and by["running"]["status"] is None
    assert by["stale"]["active"] is False and by["stale"]["status"] is None


@pytest.mark.parametrize("age_minutes,active", [(20, True), (31, False)])
def test_inventory_keeps_job_active_during_a_long_clip(tmp_path, monkeypatch, age_minutes, active):
    # 單支 Wan 片段約 20 分鐘才寫檔；生成中途不能被當成沒在跑（否則 TUI 會允許刪除或審核）。
    monkeypatch.setattr(config, "WORK", tmp_path)
    d = tmp_path / "2026-10-23_clip"
    d.mkdir()
    (d / "plan.json").write_text(json.dumps({"topic": "主題", "youtube": {"title": "標題"}}))
    (d / "meta.json").write_text(json.dumps({"format": "short"}))
    t = time.time() - age_minutes * 60
    for f in [d, *d.rglob("*")]:
        os.utime(f, (t, t))
    (row,) = jobs.inventory()
    assert row["active"] is active


def test_rows_merges_gx10_and_mac_including_mac_only(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "LOCAL", tmp_path)
    mac_only = tmp_path / GOOD[2]
    mac_only.mkdir()
    (mac_only / "final.mp4").write_bytes(b"x")
    (mac_only / "plan.json").write_text(json.dumps({"topic": "只在 Mac"}))
    gx_row = {"name": GOOD[0], "topic": "風鈴", "title": "", "format": "short", "status": "approved", "duration": 20.0,
              "youtube_id": None, "size": 9, "photo": False, "active": False}  # fmt: skip
    monkeypatch.setattr(jobs, "gx10_jobs", lambda: [gx_row])
    assert not hasattr(jobs, "nas_dirs") and not hasattr(jobs, "delete_nas")  # NAS 功能已移除
    got = {r["name"]: r for r in jobs.rows()}
    assert set(got) == {GOOD[0], GOOD[2]}
    assert (got[GOOD[0]]["gx10"], got[GOOD[0]]["mac"]) == (True, False) and "nas" not in got[GOOD[0]]
    assert (got[GOOD[2]]["gx10"], got[GOOD[2]]["mac"]) == (False, True)
    assert got[GOOD[2]]["topic"] == "只在 Mac"  # Mac 端的主題取自下載下來的 plan.json


def test_download_pulls_from_gx10_only(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "LOCAL", tmp_path)
    cmds = []

    def fake_sh(cmd, timeout=120):
        cmds.append(cmd)
        Path(cmd[-1], "final.mp4").write_bytes(b"x")  # 模擬 rsync 成功
        return _P()

    monkeypatch.setattr(jobs, "_sh", fake_sh)
    assert jobs.download(GOOD[0]) == tmp_path / GOOD[0] / "final.mp4"
    assert cmds[0][-2] == f"{jobs.GX10}:code/videoMaker/work/{GOOD[0]}/"
    assert (
        "--include=final.mp4" in cmds[0] and "--exclude=*" in cmds[0]
    )  # 只拉成片與小檔，不拉 clips／keyframes
    monkeypatch.setattr(jobs, "_sh", lambda cmd, timeout=120: _P(23, err="rsync: link_stat failed"))
    with pytest.raises(RuntimeError, match="下載失敗"):
        jobs.download("2026-10-07_zzz")


def test_cli_jobs_json_prints_inventory(tmp_path, monkeypatch, capsys):
    from videomaker import __main__ as cli

    monkeypatch.setattr(jobs, "inventory", lambda: [{"name": "x", "topic": "中文"}])
    cli.main(["jobs-json"])
    assert json.loads(capsys.readouterr().out) == [{"name": "x", "topic": "中文"}]


# ---------------- TUI（Textual 無頭模式，實際按鍵） ----------------
def _row(
    name,
    status="pending_review",
    *,
    topic="主題",
    gx10=True,
    mac=False,
    vid=None,
    active=False,
    size=1048576,
):
    return {"name": name, "topic": topic, "title": "標題", "format": "short", "status": status, "duration": 20.0,
            "youtube_id": vid, "size": size, "photo": False, "active": active, "gx10": gx10, "mac": mac}  # fmt: skip


class Env:
    """替換 jobs／youtube 的所有外部動作，記錄呼叫。"""

    def __init__(self, monkeypatch, rows):
        self.rows, self.calls = rows, []
        self.viewed = {r["name"]: DIGEST for r in rows} if isinstance(rows, list) else {}
        monkeypatch.setattr(tui.youtube, "video_sha256", lambda directory: DIGEST)
        monkeypatch.setattr(
            jobs,
            "rows",
            lambda: self.rows if not isinstance(self.rows, Exception) else (_ for _ in ()).throw(self.rows),
        )
        monkeypatch.setattr(jobs, "remote_cli", lambda *a: self.calls.append(("cli", *a)) or "ok")
        monkeypatch.setattr(
            jobs, "download", lambda n: self.calls.append(("download", n)) or Path("/x/final.mp4")
        )
        monkeypatch.setattr(jobs, "delete_gx10", lambda n: self.calls.append(("del_gx10", n)))
        monkeypatch.setattr(jobs, "delete_local", lambda n: self.calls.append(("del_mac", n)))
        monkeypatch.setattr(tui.youtube, "renew_token", lambda: self.calls.append(("token",)) or "ok")
        monkeypatch.setattr(tui.subprocess, "Popen", lambda cmd: self.calls.append(("open", *cmd)))


def drive(env, *steps):
    """依序執行（按鍵 str 或 callable），每一步後等背景工作結束。回傳 app 以便檢查。"""

    async def go():
        app = tui.VM()
        app.viewed = dict(env.viewed)  # 原有審核測試假設使用者已看過片；另測尚未看片的閘門
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            for step in steps:
                if callable(step):
                    step(app)
                else:
                    await pilot.press(*step.split())
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
            return app

    return asyncio.run(go())


def test_tui_lists_rows_with_state_and_locations(monkeypatch):
    env = Env(
        monkeypatch,
        [_row(GOOD[0], vid="abc", mac=True), _row(GOOD[1], gx10=False), _row(GOOD[2], "approved")],
    )
    seen = {}
    drive(
        env,
        lambda app: seen.update(
            rows=[app.query_one(tui.DataTable).get_row_at(i) for i in range(3)],
            cols=[str(c.label) for c in app.query_one(tui.DataTable).columns.values()],
        ),
    )
    assert seen["rows"][0][1] == "已上傳" and seen["rows"][0][-3:-1] == ["●", "●"]  # GX10、Mac 都有
    assert seen["rows"][1][1] == "待審核" and seen["rows"][1][-3:-1] == ["·", "·"]  # 只驗欄位數
    assert "NAS" not in seen["cols"] and seen["cols"][-3:] == ["GX10", "Mac", "資料夾"]
    assert seen["rows"][2][1] == "已核准"
    assert seen["cols"][0] == "選" and [r[0] for r in seen["rows"]] == ["⬜"] * 3  # 勾選欄固定顯示，沒勾也在


def test_tui_approve_reject_and_guards(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0]), _row(GOOD[1], None, active=True), _row(GOOD[2], gx10=False)])
    drive(
        env, "a", "down", "a", "down", "a", "x"
    )  # 第 1 筆核准；第 2 筆還在跑 → 擋；第 3 筆 GX10 已沒有 → 擋；x 同樣擋
    assert env.calls == [("cli", "approve", GOOD[0], f"--sha256={DIGEST}")]
    env.calls.clear()
    drive(env, "x")
    assert env.calls == [("cli", "reject", GOOD[0])]


def test_tui_failed_qc_needs_confirmation_before_approve(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], "failed")])
    drive(env, "a", "n")
    assert env.calls == []
    drive(env, "a", "y")
    assert env.calls == [("cli", "approve", GOOD[0], f"--sha256={DIGEST}")]


def test_tui_download_open_and_missing_sources(monkeypatch):
    env = Env(
        monkeypatch,
        [_row(GOOD[0]), _row(GOOD[1], gx10=False, mac=True), _row(GOOD[2], gx10=False)],
    )
    drive(env, "d", "down", "d", "down", "d")
    assert env.calls == [("download", GOOD[0])]  # 第 2、3 筆 GX10 已沒有：沒有來源可下載，不呼叫
    env.calls.clear()
    drive(env, "down", "o")  # 已在 Mac：直接開啟，不重新下載
    assert (
        env.calls[0][0] == "open"
        and env.calls[0][-1].endswith(f"{GOOD[1]}/final.mp4")
        and len(env.calls) == 1
    )


def test_tui_upload_flows(monkeypatch):
    env = Env(
        monkeypatch,
        [_row(GOOD[0], "approved"), _row(GOOD[1], "pending_review"), _row(GOOD[2], "approved", vid="done")],
    )
    drive(env, "u")
    assert env.calls == [("cli", "upload", GOOD[0])]  # 已核准：不再詢問，直接上傳
    env.calls.clear()
    drive(env, "down", "u", "n")  # 未核准：會詢問，取消就什麼都不做
    assert env.calls == []
    drive(env, "down", "u", "y")
    assert env.calls == [
        ("cli", "approve", GOOD[1], f"--sha256={DIGEST}"),
        ("cli", "upload", GOOD[1]),
    ]  # 先核准再上傳，順序不能反
    env.calls.clear()
    drive(env, "down", "down", "u")
    assert env.calls == []  # 已上傳過的不會重複


def test_tui_upload_all_counts_only_approved_not_uploaded(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], "approved", vid="x"), _row(GOOD[1], "pending_review")])
    drive(env, "U")
    assert env.calls == []  # 沒有「已核准、尚未上傳」的
    env = Env(monkeypatch, [_row(GOOD[0], "approved"), _row(GOOD[1], "approved")])
    drive(env, "U", "n")
    assert env.calls == []
    drive(env, "U", "y")
    assert env.calls == [("cli", "upload-approved")]


def test_tui_delete_gx10_normal_needs_y_and_unique_copy_needs_typing_DELETE(monkeypatch):
    # 一般：Mac 有備份 → 按 y 確認
    env = Env(monkeypatch, [_row(GOOD[0], mac=True)])
    drive(env, "g", "n")
    assert env.calls == []
    drive(env, "g", "y")
    assert env.calls == [("del_gx10", GOOD[0])]
    # 唯一一份：Mac 沒有（NAS 已不再備份）→ 單按 y 不夠，必須輸入 DELETE
    env = Env(monkeypatch, [_row(GOOD[0], mac=False)])
    drive(env, "g", "y", "enter")  # 輸入框裡打了一個 y，Enter → 不符
    assert env.calls == []
    drive(env, "g", "D E L E T E", "enter")
    assert env.calls == [("del_gx10", GOOD[0])]
    drive(env, "g", "escape")  # Esc 取消
    assert env.calls == [("del_gx10", GOOD[0])]


def test_tui_delete_refuses_running_job_and_missing_copy(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], None, active=True), _row(GOOD[1], gx10=False, mac=True)])
    drive(env, "g", "down", "g")  # 進行中不能刪 GX10；GX10 本來就沒有 → 都不動作
    assert env.calls == []


def test_tui_delete_local_requires_confirmation_and_refuses_missing_or_active_copy(monkeypatch):
    env = Env(
        monkeypatch,
        [_row(GOOD[0], mac=True), _row(GOOD[1]), _row(GOOD[2], mac=True, active=True)],
    )
    drive(env, "m", "n", "m", "escape", "down", "m", "down", "m")
    assert env.calls == []
    drive(env, "m", "y")
    assert env.calls == [("del_mac", GOOD[0])]


@pytest.mark.parametrize("gx10,size", [(False, 1048576), (True, 0)])
def test_tui_delete_local_only_video_requires_typing_DELETE(monkeypatch, gx10, size):
    env = Env(monkeypatch, [_row(GOOD[0], mac=True, gx10=gx10, size=size)])
    drive(env, "m", "y", "enter")
    assert env.calls == []
    drive(env, "m", "D E L E T E", "enter")
    assert env.calls == [("del_mac", GOOD[0])]


def test_tui_delete_local_updates_locations_and_removes_mac_only_row(tmp_path, monkeypatch):
    real_rows, real_delete = jobs.rows, jobs.delete_local
    env = Env(monkeypatch, [_row(GOOD[0], mac=True), _row(GOOD[1], mac=True, gx10=False)])
    monkeypatch.setattr(jobs, "LOCAL", tmp_path)
    monkeypatch.setattr(jobs, "gx10_jobs", lambda: [env.rows[0]])
    monkeypatch.setattr(jobs, "rows", real_rows)
    monkeypatch.setattr(jobs, "delete_local", real_delete)
    for name in GOOD[:2]:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "final.mp4").write_bytes(b"video")

    app = drive(env, "down", "m", "y", "up", "m", "D E L E T E", "enter")

    assert [r["name"] for r in app.data] == [GOOD[0]]
    assert app.data[0]["gx10"] and not app.data[0]["mac"]
    assert not any(tmp_path.iterdir())
    assert env.calls == []  # 本機刪除與列表更新都不會送出遠端刪除指令


def test_tui_delete_local_failure_keeps_row_and_reports_error(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], mac=True)])

    def fail(name):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(jobs, "delete_local", fail)
    messages = []
    original_say = tui.VM.say

    def capture(app, message, error=False):
        messages.append((message, error))
        original_say(app, message, error)

    monkeypatch.setattr(tui.VM, "say", capture)
    app = drive(env, "m", "y")

    assert app.data[0]["mac"] and not env.calls
    assert any(error and "Permission denied" in message for message, error in messages)


def test_tui_space_toggles_selection_and_escape_clears(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0]), _row(GOOD[1]), _row(GOOD[2])])
    seen = {}

    def snap(app):
        table = app.query_one(tui.DataTable)
        seen["marks"] = [table.get_row_at(i)[0] for i in range(3)]
        seen["cursor"] = table.cursor_row

    app = drive(env, "space space", snap)
    assert app.selected == {GOOD[0], GOOD[1]}
    assert seen["marks"] == ["✅", "✅", "⬜"] and seen["cursor"] == 2  # 勾選後游標自動下移
    app = drive(env, "space up space")  # 再按一次就取消
    assert app.selected == set()
    app = drive(env, "space space escape")
    assert app.selected == set()


def test_tui_batch_delete_gx10_uses_one_confirmation_for_all_selected(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], mac=True), _row(GOOD[1], mac=True), _row(GOOD[2], mac=True)])
    app = drive(env, "space space g n")
    assert env.calls == [] and app.selected == {GOOD[0], GOOD[1]}  # 取消：什麼都不刪，勾選保留
    app = drive(env, "space space g y")
    assert env.calls == [("del_gx10", GOOD[0]), ("del_gx10", GOOD[1])]  # 第 3 筆沒勾選，不能被刪
    assert app.selected == set()


def test_tui_batch_delete_skips_active_and_missing_copies(monkeypatch):
    env = Env(
        monkeypatch,
        [_row(GOOD[0], mac=True), _row(GOOD[1], None, active=True), _row(GOOD[2], gx10=False, mac=True)],
    )
    drive(env, "space space space g y")
    assert env.calls == [("del_gx10", GOOD[0])]  # 進行中、GX10 本來就沒有的都略過
    env = Env(monkeypatch, [_row(GOOD[0], None, active=True)])
    drive(env, "space g y")
    assert env.calls == []  # 全部都不能刪：不會跳確認，也不會刪


def test_tui_batch_delete_with_any_unique_copy_requires_typing_DELETE(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], mac=True), _row(GOOD[1], mac=False)])
    drive(env, "space space g y enter")  # 其中一支是唯一一份：單按 y 不夠
    assert env.calls == []
    drive(env, "space space g y enter g D E L E T E enter")
    assert env.calls == [("del_gx10", GOOD[0]), ("del_gx10", GOOD[1])]


def test_tui_batch_delete_local_and_partial_failure_keeps_failed_selected(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], mac=True), _row(GOOD[1], mac=True)])
    drive(env, "space space m y")
    assert env.calls == [("del_mac", GOOD[0]), ("del_mac", GOOD[1])]

    def flaky(name):
        if name == GOOD[1]:
            raise PermissionError("Permission denied")
        env.calls.append(("del_mac", name))

    env = Env(monkeypatch, [_row(GOOD[0], mac=True), _row(GOOD[1], mac=True)])
    monkeypatch.setattr(jobs, "delete_local", flaky)
    messages = []
    original_say = tui.VM.say
    monkeypatch.setattr(
        tui.VM, "say", lambda app, m, error=False: (messages.append((m, error)), original_say(app, m, error))
    )
    app = drive(env, "space space m y")
    assert env.calls == [("del_mac", GOOD[0])]  # 一支失敗不中斷其餘
    assert app.selected == {GOOD[1]}  # 失敗的保留勾選，方便重試
    assert any(error and GOOD[1] in m and "Permission denied" in m for m, error in messages)


def test_tui_selection_drops_jobs_that_disappear_on_refresh(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0]), _row(GOOD[1])])
    app = drive(env, "space space", lambda app: setattr(env, "rows", [_row(GOOD[1])]), "r")
    assert app.selected == {GOOD[1]}


def test_tui_has_no_nas_actions():
    assert not hasattr(tui.VM, "action_delete_nas") and "n" not in {b.key for b in tui.VM.BINDINGS}


def test_tui_survives_refresh_failure_and_failed_actions(monkeypatch):
    env = Env(monkeypatch, RuntimeError("連不上 GX10"))
    app = drive(env)  # 開機時 jobs.rows() 就失敗：不能崩潰
    assert app.data == []
    env = Env(monkeypatch, [_row(GOOD[0])])
    monkeypatch.setattr(jobs, "remote_cli", lambda *a: (_ for _ in ()).throw(RuntimeError("ssh 逾時")))
    app = drive(env, "a")  # 動作失敗：顯示錯誤、照常重新整理，不崩潰
    assert len(app.data) == 1


def test_tui_token_and_detail(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], vid="abc")])
    drive(env, "T")
    assert env.calls == [("token",)]
    seen = {}
    drive(
        env,
        "enter",
        lambda app: seen.update(screen=type(app.screen).__name__),
        "escape",
        lambda app: seen.update(after=type(app.screen).__name__),
    )
    assert seen["screen"] == "Detail" and seen["after"] != "Detail"


def _screen_texts(env_rows, push):
    """推出某個視窗，回傳畫面上實際渲染出來的所有文字（Rich 標記解析之後，不是原始字串）。"""
    from textual.widgets import Input, Label, Static

    async def go():
        app = tui.VM()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.push_screen(push)
            await pilot.pause()
            texts = [w.visual.plain for w in app.screen.query(Label)] + [
                w.visual.plain for w in app.screen.query(Static)
            ]
            texts += [w.placeholder for w in app.screen.query(Input)]
            return "\n".join(texts)

    return asyncio.run(go())


def test_confirm_dialog_shows_y_n_esc_hints(monkeypatch):
    """回歸：[y] 曾被 Rich 當成樣式標籤吞掉，畫面上只剩「確認」，使用者不知道要按什麼。"""
    Env(monkeypatch, [_row(GOOD[0])])
    text = _screen_texts(None, tui.Confirm("刪除？"))
    assert "y 確認" in text and "n 或 Esc 取消" in text
    assert "[" not in text.split("刪除？")[1]  # 提示文字沒有殘留的方括號


def test_confirm_dialog_with_typing_requirement_and_detail_show_their_hints(monkeypatch):
    Env(monkeypatch, [_row(GOOD[0])])
    assert "DELETE" in _screen_texts(None, tui.Confirm("唯一一份", require="DELETE"))
    detail = _screen_texts(None, tui.Detail("內容"))
    assert "Esc 或 Enter 關閉" in detail


def test_inventory_download_and_mac_only_rows_carry_visual_results(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(jobs, "LOCAL", tmp_path)
    directory = tmp_path / GOOD[0]
    directory.mkdir()
    (directory / "plan.json").write_text(json.dumps({"topic": "小貓"}))
    (directory / "meta.json").write_text("{}")
    (directory / "status.json").write_text(json.dumps({"status": "failed"}))
    (directory / "final.mp4").write_bytes(b"video")
    visual = {"status": "running", "checked_frames": 3, "scores": {}}
    (directory / "visual_qc.json").write_text(json.dumps({"summary": visual}))
    row = jobs.inventory()[0]
    assert row["visual_qc"] == visual and row["active"]
    monkeypatch.setattr(jobs, "gx10_jobs", list)
    assert jobs.rows()[0]["visual_qc"] == visual
    calls = []
    monkeypatch.setattr(jobs, "_sh", lambda cmd, timeout: calls.append(cmd) or _P())
    jobs.download(GOOD[0])
    assert "--include=visual_qc.json" in calls[0]


def test_tui_renders_visual_status_scores_locations_and_errors(monkeypatch):
    row = _row(GOOD[0], "failed")
    row["visual_qc"] = {
        "status": "flagged",
        "model": "clef-flash:9b",
        "threshold": 0.5,
        "checked_frames": 10,
        "scores": {"visual_defect": {"score": 0.83, "source": "clips/03.mp4", "time": 2.5}},
        "error": "literal [red] text",
    }
    env = Env(monkeypatch, [row])
    seen = {}

    def capture(app):
        seen["row"] = app.query_one(tui.DataTable).get_row_at(0)
        seen["columns"] = [str(c.label) for c in app.query_one(tui.DataTable).columns.values()]

    def detail(app):
        seen["detail"] = "\n".join(w.visual.plain for w in app.screen.query(tui.Static))

    drive(env, capture, "enter", detail, "escape")
    assert "視覺 QC" in seen["columns"] and seen["row"][2] == "有疑慮 83%"
    assert all(
        t in seen["detail"] for t in ("畫面變形", "83.0%", "clips/03.mp4", "2.50 秒", "[red]", "仍需人工看片")
    )
    assert tui.visual_text(_row(GOOD[1])) == "未檢查"
    for state, label in (("error", "檢查失敗"), ("running", "檢查中"), ("passed", "未見疑慮")):
        assert tui.visual_text({"visual_qc": {"status": state}}) == label


def test_tui_blocks_review_upload_and_delete_while_visual_qc_retries(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], "failed", active=True)])
    drive(env, "a", "x", "u", "g")
    assert env.calls == []


@pytest.mark.parametrize("age,state", [(30, "running"), (jobs.ACTIVE_SECONDS + 60, "error")])
def test_visual_summary_treats_old_running_as_interrupted(tmp_path, age, state):
    path = tmp_path / "visual_qc.json"
    path.write_text(json.dumps({"summary": {"status": "running", "scores": {}}}))
    os.utime(path, (time.time() - age, time.time() - age))
    result = jobs.visual_summary(tmp_path, {})
    assert result["status"] == state and ("error" in result) is (state == "error")


def test_inventory_reports_content_kind_and_defaults_old_jobs_to_kids(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    for name, plan in ((GOOD[0], {"topic": "小貓"}), (GOOD[1], {"topic": "Sea otter", "kind": "trivia"})):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "plan.json").write_text(json.dumps(plan))
        (directory / "meta.json").write_text("{}")
    assert {r["name"]: r["kind"] for r in jobs.inventory()} == {GOOD[0]: "kids", GOOD[1]: "trivia"}
    assert jobs.KIND_ZH["kids"] == "兒童音樂" and jobs.KIND_ZH["trivia"] == "小知識"
    assert set(jobs.KIND_ZH) == {
        "kids",
        "trivia",
        "kids_english_dialogue",
        "animal_sitcom",
        "dialogue_animation",
    }


def test_visual_summary_applies_current_threshold_to_saved_results(tmp_path):
    scores = {"visual_defect": {"score": 0.38, "source": "clips/01.mp4", "time": 0.5}}
    (tmp_path / "visual_qc.json").write_text(
        json.dumps({"summary": {"status": "passed", "threshold": 0.5, "scores": scores}})
    )
    result = jobs.visual_summary(tmp_path, {})
    assert result["status"] == "flagged" and result["threshold"] == 0.2  # 舊作業的畫面疑慮也要依新門檻顯示
    only_status = {"visual_qc": {"status": "passed", "threshold": 0.5, "scores": {}}}
    assert jobs.visual_summary(tmp_path / "none", only_status)["status"] == "passed"


def test_open_syncs_remote_even_when_local_exists_and_approval_uses_viewed_hash(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0], mac=True)])
    env.viewed.clear()
    app = drive(env, "o", "a")
    assert app.viewed[GOOD[0]] == DIGEST
    assert env.calls[0] == ("download", GOOD[0])
    assert env.calls[1][0] == "open"
    assert env.calls[2] == ("cli", "approve", GOOD[0], f"--sha256={DIGEST}")


def test_tui_requires_opening_video_before_approving_or_approve_and_upload(monkeypatch):
    env = Env(monkeypatch, [_row(GOOD[0])])
    env.viewed.clear()
    drive(env, "a", "u")
    assert env.calls == []
