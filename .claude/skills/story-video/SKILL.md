---
name: story-video
description: 把使用者的繁體中文劇情大綱（--outline）或完整劇情（--story）送到 GX10，產腳本給使用者看，確認後再生成對白類影片。僅在使用者輸入 /story-video <大綱或劇情> 時使用。
disable-model-invocation: true
---

你要把使用者的繁體中文大綱或劇情交給 GX10 做成對白類影片。**只負責「產腳本 → 使用者確認 → 開跑」；不核准、不上傳**（那是 TUI 的人工流程，硬規則 3）。

細節與限制見 `docs/operations.md#自己的劇情大綱`；硬規則見 `AGENTS.md`。

## 1. 先釐清（缺什麼才問，用 AskUserQuestion）

- **模式**：使用者只給方向、要 LLM 發揮 → `--outline`；已寫好誰做什麼、誰說什麼，要照稿 → `--story`。
- **種類**：預設 `animal_sitcom`。只支援對白類（`kids_english_dialogue`、`animal_sitcom`、`dialogue_animation`）；兒童音樂與小知識不支援，直接說明並停止。
- **標題**：`主題`，只用中英文字與空格，不含引號、`$`、反引號（它會進 ssh 指令；續跑也靠它找作業，前後必須完全一致）。
- `animal_sitcom` 的限制先對照稿子：角色只有 Meatball／HappySunny、全片 20–45 秒、3–6 鏡、每句短（≤ 3 詞／秒）、不寫會出現在畫面上的字。明顯違反就先請使用者改，不要送出去浪費 GPU。

## 2. 時間檢查（先做，不過就停）

```bash
ssh gx10 'date "+%u %H:%M"; flock -n /tmp/vmk-nightly.lock true && echo free || echo nightly-running'
```

- 週一到週六（%u 1–6）**18:40 到隔天 07:00 不跑任何生成**（含 `--dry-run`：企劃 LLM 也會占記憶體，夜間任務會因剩餘記憶體不足失敗）；`nightly-running` 同樣不跑。週日可以。
- 真實生成約 35 分鐘：現在加 50 分鐘會跨過週一到週六的 18:40 就不要開始，告訴使用者改時間。
- 不要降低 `VMK_MIN_FREE_GB` 硬跑；記憶體不足就回報並停止。

## 3. 送檔、產腳本（`--dry-run`）

檔名用 ASCII 小寫與連字號（`<日期>-<slug>.md`），放在專案外的 `~/vmk-stories/`，以 stdin 傳送避免引號問題：

```bash
ssh gx10 'mkdir -p ~/vmk-stories && cat > ~/vmk-stories/<檔名>.md' < <本機暫存檔>
ssh gx10 'cd ~/code/videoMaker && VMK_COMFY_URL=http://127.0.0.1:8188 VMK_LLM_URL=http://127.0.0.1:11434/v1 .venv/bin/python -m videomaker make "<標題>" --kind <種類> --story ~/vmk-stories/<檔名>.md --dry-run'
```

（`--story` 換成 `--outline` 即大綱模式。）最後一行 stdout 是作業目錄，取回 `plan.json` 整理成「鏡頭／秒數／誰說什麼／動作」表給使用者看：

```bash
ssh gx10 'cat <作業目錄>/plan.json'
```

- 失敗 `StoryCannotFit`：原樣轉述原因，請使用者改稿；**不要自己改稿重送**。
- 其他錯誤（驗證 3 次失敗、LLM 連不上）：貼出關鍵錯誤行，問怎麼辦。
- 對照原稿標出被改動的地方（合併、順序、台詞語意）；`--story` 模式有改動要特別說。

## 4. 使用者確認後才開跑

使用者不滿意就改稿重做第 3 步（同日同主題會另開 `-2` 資料夾；續跑只找最新的）。確認後以 `--resume` 開跑，**不要再帶 `--outline`／`--story`**（原文已在 `meta.json`）：

```bash
ssh gx10 'cd ~/code/videoMaker && nohup env VMK_COMFY_URL=http://127.0.0.1:8188 VMK_LLM_URL=http://127.0.0.1:11434/v1 .venv/bin/python -m videomaker make "<標題>" --kind <種類> --resume > log/story-<檔名>.log 2>&1 &'
```

回報：作業目錄、log 路徑、預計約 35 分鐘、看進度的指令 `ssh gx10 'tail -5 ~/code/videoMaker/log/story-<檔名>.log'`。之後的審核請使用者用 TUI（`videoMaker`：`d` 下載、`o` 看片、`a` 核准）。`renderers.RendererDisabled` 或缺權重就照實說明，不要繞過。

## 稿件範本（`--story`；`--outline` 只需寫方向與結局）

```markdown
場景：客廳，午後陽光。

1. Meatball 趴在沙發底下，眼睛盯著地上最後一塊餅乾，小聲說：
   「噓……最後一塊，是我的。」
2. HappySunny 從沙發後面探出頭，笑嘻嘻地說：「被我抓到了！」
3. Meatball 縮了一下，不服氣：「是我先發現的！」
   HappySunny 搖著尾巴：「好朋友就是要分享啊！」
4. 兩隻同時低頭，餅乾不見了，一台掃地機器人剛好滑過。
   Meatball：「餅乾呢？」
   HappySunny：「該不會……被吃掉了？」
```

使用者輸入：$ARGUMENTS
