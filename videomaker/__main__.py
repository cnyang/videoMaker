"""CLI 入口：所有子指令在 build_parser／main 分派（用法見 docs/operations.md）。"""

import argparse
import json
import logging
from pathlib import Path

import requests

from . import characters, comfy, config, facebook, jobs, labeling, nightly, pipeline, weekly, youtube
from .pipeline import STAGES
from .plan import DIALOGUE_KINDS


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="videomaker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make", help="主題 → final.mp4")
    mk.add_argument("topic")
    mk.add_argument("--stage", choices=STAGES, default="qc", help="跑到這個階段為止（已完成的階段會跳過）")
    mk.add_argument(
        "--format",
        choices=["short", "long", "xl"],
        default="short",
        help="short ≈ 20 秒；long ≈ 2:48（Shorts 上限內）；xl ≈ 5:00（超過 3 分鐘，不再是 Shorts）。長片的鏡頭會重複使用",
    )
    mk.add_argument(
        "--kind",
        choices=["kids", "trivia", *DIALOGUE_KINDS],
        default="kids",
        help="kids = 兒童音樂；trivia = 小知識（topic 填英文維基百科條目名稱，片型只支援 short／long）；"
        "kids_english_dialogue／animal_sitcom／dialogue_animation = 對白類（topic 填繁體中文主題，只支援 short，需啟用 H3）",
    )
    mk.add_argument("--dry-run", action="store_true", help="只產 plan.json，不呼叫 ComfyUI")
    mk.add_argument(
        "--images",
        type=Path,
        nargs="+",
        metavar="DIR",
        help="照片模式：1 個資料夾 = 照片當每鏡關鍵幀；2 個資料夾 = 兩隻狗同框（每鏡各取一張用 Qwen-Image-Edit 合成）。"
        "可附同名 .txt 一行英文描述",
    )
    mk.add_argument(
        "--resume", action="store_true", help="續跑同主題、同 --kind／--format 建立時間最新的作業"
    )
    nt = sub.add_parser(
        "nightly", help="夜間排程：LLM 想主題，依序產生（預設 short、long、xl 各 1 支），deadline 前結束"
    )
    nt.add_argument(
        "--kind",
        choices=["auto", "kids", "trivia", *DIALOGUE_KINDS],
        default="kids",
        help="今晚產哪一種；auto = 依星期（週一到週六各產 2 支 short：一/六兒童音樂、二/四小知識、三/五兒童英文對話，"
        "每晚再加動物短劇；週日不生成，只合併本週成片）。手動指定種類時，生成結束後會接著合併同一種內容（不用 GPU，失敗不影響成片）",
    )
    nt.add_argument(
        "--plan",
        help="片型順序，逗號分隔（short／long／xl）；預設依 --kind：kids 三種、trivia 只有 short、long",
    )
    nt.add_argument("--deadline", default="07:00", help="必須結束的時間 HH:MM")
    nt.add_argument("--dry-run", action="store_true", help="只想主題並預估，不產生影片")
    sub.add_parser(
        "weekly",
        help="手動合併：每種內容各按片型，把近期夜間成片串成一支（不用 GPU；夜間排程結束後會自動做一次）",
    )
    sub.add_parser("auth", help="YouTube OAuth 授權（在有瀏覽器的機器上跑一次）")
    sub.add_parser("facebook-auth", help="互動設定 Facebook Page ID 與 token（存專案外，不回顯 token）")
    for name, h in (
        ("approve", "人工核准一支成片（之後才能上傳）"),
        ("reject", "標記一支成片為不通過"),
        ("upload", "上傳一支已核准的成片（私人）"),
        ("upload-facebook", "上傳一支已核准的短片到 Facebook 粉絲專頁（草稿，不發布）"),
    ):
        review = sub.add_parser(name, help=h)
        review.add_argument("job", help="作業資料夾名（work/ 底下）或完整路徑")
        if name == "approve":
            review.add_argument("--sha256", help="審核時觀看的成片 hash；與目前成片不同就拒絕核准")
            review.add_argument(
                "--from-sources", action="store_true", help="合輯專用：來源都已人工核准才核准，否則拒絕"
            )
    sub.add_parser("token-status", help="檢查本機 YouTube token 是否還有效（ok / expired / missing）")
    sub.add_parser(
        "renew-token", help="（在 Mac 跑）檢查 GX10 的 token，過期就跳視窗確認、重新授權並自動複製"
    )
    sub.add_parser("tui", help="互動式介面（在 Mac 上跑）：看三處作業、下載、核准、上傳、刪除")
    sub.add_parser("jobs-json", help="（給 TUI 用，在 GX10 上跑）以 JSON 輸出所有作業的盤點")
    sub.add_parser("list", help="列出所有作業的狀態（待審核、已核准、已上傳…）")
    sub.add_parser("upload-approved", help="上傳所有已核准、尚未上傳的成片（私人）")
    lb = sub.add_parser(
        "visual-labels", help="（在 GX10 跑）抽出待人工標記的視覺 QC 畫面與盲標頁面，用來校準疑慮門檻"
    )
    lb.add_argument("out", type=Path, help="輸出資料夾（frames/、index.html、manifest.json）")
    lb.add_argument("--top", type=int, default=40, help="分數最高的畫面張數")
    lb.add_argument("--random", type=int, default=40, help="其餘畫面隨機抽幾張（檢查低分有沒有漏判）")
    ch = sub.add_parser(
        "characters", help="固定角色庫：list 列出角色與參考圖狀態；build 一次性產生缺少的參考圖（佔用 GPU）"
    )
    ch.add_argument("action", choices=["list", "build"])
    ch.add_argument("ids", nargs="*", help="build 時只處理這些角色 ID（預設全部）")
    ch.add_argument(
        "--force", action="store_true", help="build 時覆蓋已存在的參考圖（會破壞既有角色外觀，請謹慎）"
    )
    ch.add_argument(
        "--salt", type=int, default=0, help="build 時換一組 seed（0 = 預設；要換圖時用，如 1、2）"
    )
    sub.add_parser("bench", help="各階段耗時基準")
    sub.add_parser("check", help="檢查 workflow 用到的模型 ComfyUI 是否看得到")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "auth":
        youtube.auth()
    elif a.cmd == "facebook-auth":
        facebook.auth()
    elif a.cmd == "approve" and a.from_sources:
        youtube.approve_from_sources(youtube.resolve(a.job))
    elif a.cmd in ("approve", "reject"):
        youtube.set_review(
            youtube.resolve(a.job),
            "approved" if a.cmd == "approve" else "rejected",
            expected_sha256=a.sha256 if a.cmd == "approve" else None,
        )
    elif a.cmd == "upload":
        print(youtube.upload(youtube.resolve(a.job)))
    elif a.cmd == "upload-facebook":
        print(facebook.upload(youtube.resolve(a.job)))
    elif a.cmd == "token-status":
        status = youtube.token_status()
        print(status)
        raise SystemExit(0 if status == "ok" else 2)
    elif a.cmd == "renew-token":
        print(youtube.renew_token())
    elif a.cmd == "tui":
        from .tui import run  # 延遲載入：GX10 沒裝 Textual，夜間 cron 不能因此壞掉

        run()
    elif a.cmd == "jobs-json":
        print(json.dumps(jobs.inventory(), ensure_ascii=False))
    elif a.cmd == "list":
        print("\n".join(youtube.listing()))
    elif a.cmd == "upload-approved":
        failed = 0
        for d in youtube.pending_approved():
            try:
                print(d.name, youtube.upload(d))
            except Exception as e:  # noqa: BLE001 一支失敗不擋其他支；沒上傳成功的下次重跑（上傳是冪等的）
                failed += 1
                print(d.name, "失敗：", repr(e)[:300])
        raise SystemExit(1 if failed else 0)
    elif a.cmd == "nightly":
        for r in nightly.tonight(a.kind, a.plan.split(",") if a.plan else None, a.deadline, a.dry_run):
            print(json.dumps(r, ensure_ascii=False))
    elif a.cmd == "weekly":
        for p in weekly.merge_all():
            print(p)
    elif a.cmd == "visual-labels":
        print(f"{labeling.export(a.out, a.top, a.random)} 張 → {a.out}/index.html")
    elif a.cmd == "characters":
        if a.action == "build":
            for path in characters.build(a.ids or None, a.force, salt=a.salt):
                print(path)
        else:
            for cid in characters.ids():
                missing = characters.load(cid).missing_refs()
                print(cid, "缺參考圖：" + "、".join(missing) if missing else "參考圖齊全")
    elif a.cmd == "bench":
        print(pipeline.bench())
    elif a.cmd == "check":
        miss = comfy.missing_models(requests.get(f"{config.COMFY_URL}/object_info", timeout=30).json())
        for wf, f in miss:
            print(f"缺：{f}（{wf}）")
        raise SystemExit(1 if miss else print("模型齊全"))
    else:
        job = pipeline.run(a.topic, a.stage, a.dry_run, a.resume, a.images, a.format, a.kind)
        print(job.dir)


if __name__ == "__main__":
    main()
