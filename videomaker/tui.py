"""互動式 TUI（在 Mac 上跑）：看 GX10、Mac 兩處的作業，下載、核准、上傳、刪除。

畫面只負責呈現與按鍵；所有遠端動作與名稱檢查在 jobs.py。耗時動作都放在背景執行緒，不會卡住畫面。
"""

import subprocess
from collections.abc import Callable
from typing import ClassVar

from rich.markup import escape
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from . import facebook, jobs, youtube

REFRESH_SECONDS = 120


def state_text(r: dict) -> str:
    if r["youtube_id"]:
        return "已上傳"
    if r["status"] == "pending_review" and r.get("sources_approved"):
        return "待審核（來源皆已核准）"  # 合輯：可按 u 直接上傳
    if r["status"]:
        return str(jobs.STATE_ZH.get(r["status"], r["status"]))
    if r["active"]:
        return "進行中"
    return "未完成" if r["gx10"] else "—"


FB_PHASE_ZH = {"draft": "草稿", "failed": "失敗", "published": "已發布"}


def facebook_text(r: dict) -> str:
    reel = r.get("facebook_reel")
    return FB_PHASE_ZH.get(reel["phase"], "待完成") if reel else "-"


def _mb(n: int) -> str:
    return f"{n / 1048576:.0f}MB" if n else "-"


def visual_text(row: dict) -> str:
    visual = row.get("visual_qc") or {}
    state = jobs.VISUAL_STATE_ZH.get(visual.get("status"), "未檢查")
    scores = visual.get("scores", {})
    peak = max((v["score"] for v in scores.values()), default=None)
    return f"{state} {peak:.0%}" if peak is not None else state


class Confirm(ModalScreen[bool]):
    """確認視窗：一般按 y 確認、n／Esc 取消；require 有值時必須輸入該字串才能確認（用在唯一一份的刪除）。"""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "取消")]

    def __init__(self, message: str, require: str | None = None) -> None:
        super().__init__()
        self.message, self.require = message, require

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message)
            if self.require:
                yield Input(placeholder=f"輸入 {self.require} 後按 Enter 確認，Esc 取消")
            else:
                yield Label("[b]y[/b] 確認　　[b]n[/b] 或 [b]Esc[/b] 取消")

    def on_key(self, event: events.Key) -> None:
        if not self.require and event.key in ("y", "n"):
            event.stop()
            self.dismiss(event.key == "y")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value == self.require)

    def action_cancel(self) -> None:
        self.dismiss(False)


class Detail(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "關閉"),
        Binding("enter", "close", "關閉"),
        Binding("q", "close", "關閉"),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(self.text)
            yield Label("[b]Esc[/b] 或 [b]Enter[/b] 關閉")

    def action_close(self) -> None:
        self.dismiss(None)


class VM(App):
    TITLE = "videoMaker"
    CSS = """
    #dialog { width: 78%; max-width: 100; height: auto; max-height: 85%; padding: 1 2; border: thick $warning; background: $surface; margin: 2 0; align-horizontal: center; }
    ModalScreen { align: center middle; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "refresh", "重新整理"),
        Binding("space", "toggle_select", "勾選"),
        Binding("escape", "clear_selection", "清除勾選", show=False),
        Binding("d", "download", "下載到Mac"),
        Binding("o", "open_local", "開啟"),
        Binding("a", "approve", "核准"),
        Binding("x", "reject", "不通過"),
        Binding("u", "upload", "上傳"),
        Binding("U", "upload_all", "上傳全部已核准"),
        Binding("f", "upload_facebook", "FB草稿"),
        Binding("g", "delete_gx10", "刪GX10"),
        Binding("m", "delete_local", "刪Mac"),
        Binding("T", "token", "token"),
        Binding("q", "quit", "離開"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.data: list[dict] = []
        self.viewed: dict[str, str] = {}  # 核准只能使用這次開啟播放的版本，不能拿 GX10 的新成片冒充
        self.selected: set[str] = set()  # 勾選的作業名稱；目前只給批次刪除（g／m）使用

    # ----- 畫面 -----
    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static("載入中…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(
            "選", "狀態", "視覺 QC", "主題", "片型", "長度", "大小", "YouTube", "FB", "GX10", "Mac", "資料夾"
        )
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter 會被表格攔截（選取列），所以用這個事件開詳情，而不是 App 層的快捷鍵。"""
        self.action_detail()

    def say(self, msg: str, error: bool = False) -> None:
        self.query_one("#status", Static).update(msg)
        self.notify(msg, severity="error" if error else "information", timeout=10 if error else 5)

    def fill(self, rows: list[dict]) -> None:
        cur = self.current()
        self.data = rows
        self.selected &= {r["name"] for r in rows}  # 已經不存在的作業不保留勾選
        self.redraw(cur["name"] if cur else None)

    def redraw(self, keep: str | None = None) -> None:
        """依 self.data 與 self.selected 重畫表格，游標停回 keep。"""
        table = self.query_one(DataTable)
        table.clear()
        for r in self.data:
            table.add_row(
                "✅" if r["name"] in self.selected else "⬜", state_text(r), visual_text(r), r["topic"], r["format"], f"{r['duration']:.0f}s" if r["duration"] else "-", _mb(r["size"]),
                r["youtube_id"] or "-", facebook_text(r), *("●" if r[k] else "·" for k in ("gx10", "mac")), r["name"], key=r["name"],
            )  # fmt: skip
        if keep and any(r["name"] == keep for r in self.data):
            table.move_cursor(row=table.get_row_index(keep))
        picked = f"　已勾選 {len(self.selected)} 支（g／m 批次刪除，Esc 清除）" if self.selected else ""
        self.query_one("#status", Static).update(
            f"{len(self.data)} 筆　●=該處有　·=沒有（GX10 ／ Mac）　Enter=詳情　空白=勾選{picked}"
        )

    def current(self) -> dict | None:
        table = self.query_one(DataTable)
        if not self.data or table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((r for r in self.data if r["name"] == key), None)

    # ----- 背景工作 -----
    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh(self) -> None:
        try:
            rows = jobs.rows()
        except Exception as e:  # noqa: BLE001 連線失敗等任何錯誤都只顯示訊息，不讓 TUI 掛掉
            self.call_from_thread(self.say, f"重新整理失敗：{e}", True)
            return
        self.call_from_thread(self.fill, rows)

    @work(thread=True, group="task")
    def run_task(self, label: str, fn: Callable[[], object]) -> None:
        self.call_from_thread(self.say, f"{label}…")
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 任何失敗都要顯示給使用者，並照常重新整理
            self.call_from_thread(self.say, f"✗ {label}失敗：{e}", True)
        else:
            self.call_from_thread(self.say, f"✓ {label}完成" + (f"：{result}" if result else ""))
        self.call_from_thread(self.action_refresh)

    def ask(self, message: str, then: Callable[[], None], require: str | None = None) -> None:
        self.push_screen(Confirm(message, require), lambda ok: then() if ok else self.say("已取消"))

    # ----- 動作 -----
    def action_detail(self) -> None:
        r = self.current()
        if not r:
            return
        mac = jobs.LOCAL / r["name"] / "final.mp4"
        lines = [
            f"[b]{r['topic']}[/b]　{r['name']}",
            f"YouTube 標題：{r['title'] or '-'}",
            f"內容：{jobs.KIND_ZH.get(r.get('kind', 'kids'), '-')}　片型：{r['format']}　長度：{r['duration'] or '-'} 秒　大小：{_mb(r['size'])}　照片模式：{'是' if r['photo'] else '否'}",
            f"狀態：{state_text(r)}　　位置：GX10 {'有' if r['gx10'] else '無'} ／ Mac {'有' if r['mac'] else '無'}",
            f"Mac：{mac}" if r["mac"] else "Mac：尚未下載（按 d）",
            f"YouTube：https://studio.youtube.com/video/{r['youtube_id']}/edit"
            if r["youtube_id"]
            else "YouTube：尚未上傳",
        ]
        reel = r.get("facebook_reel")
        if reel:
            lines.append(
                f"FB：{escape(reel.get('phase', '-'))}　影片 ID：{escape(reel.get('video_id', '-'))}（發布請到 Meta 操作）"
            )
        visual = r.get("visual_qc") or {}
        lines += ["", f"[b]視覺 QC：{visual_text(r)}[/b]"]
        if visual:
            lines.append(
                f"模型：{escape(visual.get('model', '-'))}　已抽查 {visual.get('checked_frames', 0)} 張"
            )
            lines.append(f"疑慮門檻：{visual.get('threshold', 0.5):.0%}；以下為各項最高分數")
            for key, value in visual.get("scores", {}).items():
                label = jobs.VISUAL_LABELS.get(key, key)
                where = f"{escape(value['source'])} 約 {value['time']:.2f} 秒"
                if "final_time" in value:  # 舊作業沒有這欄；長片的鏡頭會重複出現，這是第一次出現的成片時間
                    where += f"（成片約 {value['final_time']:.2f} 秒）"
                lines.append(f"{label}：{value['score']:.1%}　{where}")
            if visual.get("error"):
                lines.append(f"錯誤：{escape(visual['error'])}")
        else:
            lines.append("這支尚未做視覺檢查。")
        lines.append("抽樣可能漏掉短暫異常，分數不是安全保證，仍需人工看片。")
        self.push_screen(Detail("\n".join(lines)))

    def action_download(self) -> None:
        r = self.current()
        if not r:
            return
        if not r["gx10"]:
            return self.say("GX10 已沒有這支，無法下載", True)
        if not r["size"]:
            return self.say("這支還沒有成片（final.mp4）", True)
        self.run_task(f"下載 {r['topic']}", lambda: str(jobs.download(r["name"])))

    def action_open_local(self) -> None:
        r = self.current()
        if not r:
            return
        path = jobs.LOCAL / r["name"] / "final.mp4"
        if r["mac"] and not r["gx10"]:
            subprocess.Popen(["open", str(path)])
            return self.say(f"已開啟 {path}")
        if not r["gx10"]:
            return self.say("這支沒有任何來源可以下載", True)

        def open_latest():
            path = jobs.download(r["name"])
            digest = youtube.video_sha256(path.parent)
            subprocess.Popen(["open", str(path)])
            self.call_from_thread(self.viewed.__setitem__, r["name"], digest)

        self.run_task(f"同步並開啟 {r['topic']}", open_latest)

    def _review(self, verb: str) -> None:
        r = self.current()
        if not r:
            return
        if not r["gx10"]:
            return self.say("GX10 上已沒有這支，無法審核", True)
        if r["active"] or r["status"] is None:
            return self.say("這支還沒完成（沒有 QC 結果），不能審核", True)
        label = "核准" if verb == "approve" else "標記不通過"
        digest = self.viewed.get(r["name"])
        if verb == "approve" and not digest:
            return self.say("請先按 o 開啟最新成片，看片後再核准", True)
        args = (verb, r["name"], f"--sha256={digest}") if verb == "approve" else (verb, r["name"])
        go = lambda: self.run_task(f"{label} {r['topic']}", lambda: jobs.remote_cli(*args))
        if r["status"] == "failed":
            return self.ask(f"這支 QC 沒通過，確定要{label}嗎？", go)
        go()

    def action_approve(self) -> None:
        self._review("approve")

    def action_reject(self) -> None:
        self._review("reject")

    def action_upload(self) -> None:
        r = self.current()
        if not r:
            return
        if r["active"]:
            return self.say("這支正在處理中，請等 QC 完成再上傳", True)
        if r["youtube_id"]:
            return self.say("這支已經上傳過了", True)
        if not r["gx10"] or not r["size"]:
            return self.say("GX10 上沒有這支的成片，無法上傳（上傳在 GX10 執行）", True)
        if r["status"] is None:
            return self.say("這支還沒完成，不能上傳", True)
        digest = self.viewed.get(r["name"])
        # 合輯的來源都已人工核准 → 不必再看片，由 GX10 端重驗來源後核准（--from-sources）
        from_sources = r["status"] == "pending_review" and bool(r.get("sources_approved"))
        if r["status"] != "approved" and not digest and not from_sources:
            return self.say("請先按 o 開啟最新成片，看片後再核准上傳", True)

        def up() -> str:
            if r["status"] != "approved":
                jobs.remote_cli(
                    "approve", r["name"], "--from-sources" if from_sources else f"--sha256={digest}"
                )
            return jobs.remote_cli("upload", r["name"])

        go = lambda: self.run_task(f"上傳 {r['topic']}（私人）", up)
        if r["status"] == "approved" or from_sources:
            go()
        else:
            self.ask(f"這支{state_text(r)}，尚未核准。要「先核准再上傳（私人）」嗎？", go)

    def action_upload_all(self) -> None:
        n = sum(
            1
            for r in self.data
            if r["status"] == "approved" and not r["youtube_id"] and r["gx10"] and not r["active"]
        )
        if not n:
            return self.say("沒有「已核准、尚未上傳」的作業")
        self.ask(
            f"上傳 {n} 支已核准的影片（全部私人）？",
            lambda: self.run_task("上傳全部已核准", lambda: jobs.remote_cli("upload-approved")),
        )

    def action_upload_facebook(self) -> None:
        r = self.current()
        if not r:
            return
        if r["active"]:
            return self.say("這支正在處理中，請等 QC 完成再上傳", True)
        if not r["gx10"] or not r["size"]:
            return self.say("GX10 上沒有這支的成片，無法上傳 FB 草稿", True)
        if r["duration"] and not facebook.MIN_SECONDS <= r["duration"] <= facebook.MAX_SECONDS:
            return self.say(
                f"FB Reels 只收 {facebook.MIN_SECONDS}–{facebook.MAX_SECONDS} 秒的短片，這支不符", True
            )
        if r["status"] != "approved":
            return self.say("請先按 o 看片、按 a 人工核准，再上傳 FB 草稿", True)
        if (r.get("facebook_reel") or {}).get("phase") == "draft":
            return self.say("這支已上傳 FB 草稿；請到 Meta 檢查或發布")
        self.run_task(
            f"上傳 FB 草稿 {r['topic']}（不發布）", lambda: jobs.remote_cli("upload-facebook", r["name"])
        )

    def action_toggle_select(self) -> None:
        r = self.current()
        if not r:
            return
        self.selected ^= {r["name"]}
        self.redraw(r["name"])
        self.query_one(DataTable).action_cursor_down()  # 連按空白可以一路往下勾

    def action_clear_selection(self) -> None:
        if self.selected:
            self.selected.clear()
            cur = self.current()
            self.redraw(cur["name"] if cur else None)

    def _delete_selected(self, side: str) -> None:
        """批次刪除勾選的作業（單一確認）；進行中、該處沒有存檔的略過。有任何一支是唯一一份就要輸入 DELETE。"""
        gx = side == "gx10"
        where = "GX10" if gx else "Mac"
        have = "gx10" if gx else "mac"
        picked = [r for r in self.data if r["name"] in self.selected]
        todo = [r for r in picked if r[have] and not r["active"]]
        if not todo:
            return self.say(f"勾選的作業在 {where} 都不能刪（沒有存檔，或可能還在執行中）", True)
        # 唯一一份的判斷與單筆刪除相同：刪 GX10 看 Mac 有沒有；刪 Mac 看 GX10 有沒有成片
        only = [r for r in todo if (not r["mac"] if gx else not (r["gx10"] and r["size"]))]
        shown = [
            f"  {escape(r['name'])}（{escape(r['topic'])}）{' [red]唯一一份[/red]' if r in only else ''}"
            for r in todo[:10]
        ]
        if len(todo) > 10:
            shown.append(f"  …另 {len(todo) - 10} 支")
        lines = [f"永久刪除 {where} 上這 {len(todo)} 支作業的整個資料夾？", *shown]
        if len(picked) > len(todo):
            lines.append(f"（另 {len(picked) - len(todo)} 支沒有 {where} 存檔或可能還在執行中，已略過）")
        lines.append(
            "（已上傳的 YouTube 影片不受影響）" if gx else "（GX10 與 YouTube 不受影響，不經垃圾桶）"
        )
        if only:
            lines.append(f"[b][red]其中 {len(only)} 支是唯一一份，刪了就回不來。[/red][/b]")

        def run() -> str:
            failed = []
            for r in todo:
                try:
                    (jobs.delete_gx10 if gx else jobs.delete_local)(r["name"])
                except Exception as e:  # noqa: BLE001 單支失敗不中斷其餘，最後一起回報
                    failed.append(f"{r['name']}：{e}")
                else:
                    # 失敗的保留勾選，方便重試；用 lambda 是因為 call_from_thread 取不到內建方法 set.discard 的簽名
                    self.call_from_thread(lambda name=r["name"]: self.selected.discard(name))
            if failed:
                raise RuntimeError(
                    f"成功 {len(todo) - len(failed)} 支、失敗 {len(failed)} 支\n" + "\n".join(failed)
                )
            return f"{len(todo)} 支"

        self.ask(
            "\n".join(lines),
            lambda: self.run_task(f"批次刪除 {where} 存檔", run),
            require="DELETE" if only else None,
        )

    def action_delete_gx10(self) -> None:
        if self.selected:
            return self._delete_selected("gx10")
        r = self.current()
        if not r:
            return
        if not r["gx10"]:
            return self.say("GX10 上已沒有這支", True)
        if r["active"]:
            return self.say("這支可能還在執行中，先不要刪", True)
        unique = not r["mac"]  # 沒有 NAS 備份了：Mac 沒下載就是唯一一份
        warn = "\n[b][red]這是唯一一份！Mac 沒有下載，刪了就回不來。[/red][/b]" if unique else ""
        extra = "\n（已上傳的 YouTube 影片不受影響）" if r["youtube_id"] else ""
        self.ask(
            f"刪除 GX10 上的整個作業資料夾？\n{r['name']}（{r['topic']}，成片 {_mb(r['size'])}）{extra}{warn}",
            lambda: self.run_task(f"刪除 GX10 存檔 {r['topic']}", lambda: jobs.delete_gx10(r["name"])),
            require="DELETE" if unique else None,
        )

    def action_token(self) -> None:
        self.run_task("檢查／更新 YouTube token", youtube.renew_token)

    def action_delete_local(self) -> None:
        if self.selected:
            return self._delete_selected("mac")
        r = self.current()
        if not r:
            return
        if not r["mac"]:
            return self.say("Mac 上沒有這支的下載檔", True)
        if r["active"]:
            return self.say("這支可能還在執行中，先不要刪", True)
        unique = not (r["gx10"] and r["size"])
        warn = "\n[b][red]這是唯一一份！GX10 沒有成片，刪了就回不來。[/red][/b]" if unique else ""
        self.ask(
            f"永久刪除 Mac 上下載的整個作業資料夾（不經垃圾桶）？\n{escape(str(jobs.LOCAL / r['name']))}\n（GX10 與 YouTube 不受影響）{warn}",
            lambda: self.run_task(f"刪除 Mac 存檔 {r['topic']}", lambda: jobs.delete_local(r["name"])),
            require="DELETE" if unique else None,
        )


def run() -> None:
    VM().run()
