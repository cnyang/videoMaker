#!/usr/bin/env bash
# 限速下載 models.txt 中符合 <pattern> 的模型到 ComfyUI models 目錄（可續傳、sha256 驗證）。
# 不用 comfy-get-model.sh：它底層的 hf download 多連線且無法限速，會佔滿同區網頻寬。
# 用法：scripts/download.sh <pattern> [限速，預設 2M（curl 單位，約 2MiB/s）]
set -euo pipefail
PATTERN=${1:?用法: $0 <pattern> [rate]}
RATE=${2:-2M}
MODELS=${COMFY_MODELS_DIR:-$HOME/code/comfyui-docker/models}
cd "$(dirname "$0")/.."
LINES=$(grep -v '^#' models.txt | grep -E "$PATTERN" || true)
[ -n "$LINES" ] || { echo "models.txt 沒有符合 '$PATTERN' 的項目"; exit 1; }
while read -r target type; do
  repo=$(cut -d/ -f1-2 <<<"$target")
  path=${target#"$repo"/}
  dest="$MODELS/$type/$(basename "$path")"
  [ -s "$dest" ] && { echo "已存在，略過：$dest"; continue; }
  # 預期 sha256 取自 HF 的 LFS metadata
  want=$(python3 - "$repo" "$path" <<'PY'
import json, sys, urllib.request
repo, path = sys.argv[1:]
url = f"https://huggingface.co/api/models/{repo}/tree/main/{path.rpartition('/')[0]}".rstrip("/")
print(next(f for f in json.load(urllib.request.urlopen(url)) if f["path"] == path)["lfs"]["oid"])
PY
)
  mkdir -p "$MODELS/$type"
  echo "[$(date '+%F %T')] 下載 $path → $dest（限速 $RATE，sha256 $want）"
  # 先寫 .part：ComfyUI 不會把未完成的檔案列進選單；-C - 可續傳
  # HTTP/2 長連線偶爾被 CDN 以 CANCEL (92) 切斷，curl 內建 --retry 不會重試這類錯誤，
  # 所以自己迴圈；-C - 會從 .part 現有大小接著下載
  ok=0
  for try in 1 2 3 4 5 6 7 8; do
    if curl -fsSL --limit-rate "$RATE" -C - -o "$dest.part" "https://huggingface.co/$repo/resolve/main/$path" </dev/null; then ok=1; break; fi
    echo "[$(date '+%F %T')] curl 失敗（第 $try 次），10 秒後續傳"; sleep 10
  done
  [ "$ok" = 1 ] || { echo "❌ 下載失敗，保留 $dest.part 供續傳"; exit 1; }
  if echo "$want  $dest.part" | sha256sum -c -; then
    mv "$dest.part" "$dest" && echo "[$(date '+%F %T')] ✅ 完成 $dest"
  else
    mv "$dest.part" "$dest.bad" && echo "❌ sha256 不符，已改名 $dest.bad" && exit 1
  fi
done <<<"$LINES"
