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

from . import jobs, youtube

REFRESH_SECONDS = 120


def state_text(r: dict) -> str:
    if r["youtube_id"]:
        return "已上傳"
    if r["status"]:
        return str(jobs.STATE_ZH.get(r["status"], r["status"]))
    if r["active"]:
        return "進行中"
    return "未完成" if r["gx10"] else "—"


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
        Binding("d", "download", "下載到Mac"),
        Binding("o", "open_local", "開啟"),
        Binding("a", "approve", "核准"),
        Binding("x", "reject", "不通過"),
        Binding("u", "upload", "上傳"),
        Binding("U", "upload_all", "上傳全部已核准"),
        Binding("g", "delete_gx10", "刪GX10"),
        Binding("T", "token", "token"),
        Binding("q", "quit", "離開"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.data: list[dict] = []

    # ----- 畫面 -----
    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static("載入中…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(
            "狀態", "視覺 QC", "主題", "片型", "長度", "大小", "YouTube", "GX10", "Mac", "資料夾"
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
        table = self.query_one(DataTable)
        cur = self.current()
        keep = cur["name"] if cur else None
        self.data = rows
        table.clear()
        for r in rows:
            table.add_row(
                state_text(r), visual_text(r), r["topic"], r["format"], f"{r['duration']:.0f}s" if r["duration"] else "-", _mb(r["size"]),
                r["youtube_id"] or "-", *("●" if r[k] else "·" for k in ("gx10", "mac")), r["name"], key=r["name"],
            )  # fmt: skip
        if keep and any(r["name"] == keep for r in rows):
            table.move_cursor(row=table.get_row_index(keep))
        self.query_one("#status", Static).update(
            f"{len(rows)} 筆　●=該處有　·=沒有（GX10 ／ Mac）　Enter=詳情"
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
            f"片型：{r['format']}　長度：{r['duration'] or '-'} 秒　大小：{_mb(r['size'])}　照片模式：{'是' if r['photo'] else '否'}",
            f"狀態：{state_text(r)}　　位置：GX10 {'有' if r['gx10'] else '無'} ／ Mac {'有' if r['mac'] else '無'}",
            f"Mac：{mac}" if r["mac"] else "Mac：尚未下載（按 d）",
            f"YouTube：https://studio.youtube.com/video/{r['youtube_id']}/edit"
            if r["youtube_id"]
            else "YouTube：尚未上傳",
        ]
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
        if r["mac"]:
            subprocess.Popen(["open", str(path)])
            return self.say(f"已開啟 {path}")
        if not r["gx10"]:
            return self.say("這支沒有任何來源可以下載", True)
        self.run_task(
            f"下載並開啟 {r['topic']}",
            lambda: (subprocess.Popen(["open", str(jobs.download(r["name"]))]), "")[1],
        )

    def _review(self, verb: str) -> None:
        r = self.current()
        if not r:
            return
        if not r["gx10"]:
            return self.say("GX10 上已沒有這支，無法審核", True)
        if r["active"] or r["status"] is None:
            return self.say("這支還沒完成（沒有 QC 結果），不能審核", True)
        label = "核准" if verb == "approve" else "標記不通過"
        go = lambda: self.run_task(f"{label} {r['topic']}", lambda: jobs.remote_cli(verb, r["name"]))
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

        def up() -> str:
            if r["status"] != "approved":
                jobs.remote_cli("approve", r["name"])
            return jobs.remote_cli("upload", r["name"])

        go = lambda: self.run_task(f"上傳 {r['topic']}（私人）", up)
        if r["status"] == "approved":
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

    def action_delete_gx10(self) -> None:
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


def run() -> None:
    VM().run()
