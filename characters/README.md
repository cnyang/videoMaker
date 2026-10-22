# 固定角色庫

每個資料夾 = 一個角色：`character.json`（外觀、個性、配件、聲音、參考圖清單）＋ 參考圖 PNG。
對白類內容（兒童英文對話、動物短劇、有對白的動畫）只能引用這裡已有的角色 ID，找不到就是驗證失敗。

- 新增角色：建 `characters/<英文>_<兩位數字>/character.json`（`id` 與資料夾名相同，`appearance`／`voice` 純英文）。
- 產生參考圖（一次性、手動、佔用 GPU，不在夜間時段跑）：`videoMaker characters build [角色 ID …] [--force] [--salt N]`；已存在的圖會跳過，`--salt` 換一組 seed。`front` 文字生圖、其他視角由 `front` 編輯而來（維持同一隻角色）。
- **參考圖要人工看過**再放行：不滿意就手動換圖（檔名要與 `references` 一致），或 `--force --salt N` 重生；`--force` 會讓既有影片的角色外觀與新圖不同，請謹慎。
- 參考圖是持久資產：夜間排程不會重新生成；`h3_ref2va` 缺圖會在開跑前中止並提示執行上面的指令。
