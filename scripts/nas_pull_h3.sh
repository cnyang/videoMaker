#!/usr/bin/env bash
# 從 NAS 拉 H3 權重到 ComfyUI models 目錄（2026-10-22 使用者放行）：限速、先寫 .part、拉完用 HF LFS 的 sha256 驗證才改名
# 用法（在 GX10）：scripts/nas_pull_h3.sh [限速 KB/s，預設 6000]。NAS 沒有 int8_convrot 的 video VAE，那個用 scripts/download.sh minimax_h3_video_vae_int8
set -uo pipefail
MODELS=$HOME/code/comfyui-docker/models
NAS=cnyang@10.100.100.100:/volume1/comfyUI-model
RATE=${1:-6000}   # KB/s
for rel in diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors \
           diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors \
           text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors \
           vae/minimax_h3_audio_vae_fp32.safetensors; do
  dest=$MODELS/$rel
  [ -s "$dest" ] && { echo "已存在，略過：$dest"; continue; }
  want=$(python3 - "$rel" <<'PY'
import json, sys, urllib.request
rel = sys.argv[1]
url = f"https://huggingface.co/api/models/Comfy-Org/MiniMax-H3/tree/main/{rel.rpartition('/')[0]}"
print(next(f for f in json.load(urllib.request.urlopen(url)) if f["path"] == rel)["lfs"]["oid"])
PY
)
  echo "[$(date '+%F %T')] 拉 $rel（限速 ${RATE}KB/s，HF sha256 $want）"
  until rsync -e "ssh -i $HOME/.ssh/gx10_to_nas -p 2222 -o BatchMode=yes" --partial --bwlimit="$RATE" "$NAS/$rel" "$dest.part"; do
    echo "[$(date '+%F %T')] rsync 失敗，10 秒後續傳"; sleep 10
  done
  if echo "$want  $dest.part" | sha256sum -c -; then
    mv "$dest.part" "$dest" && echo "[$(date '+%F %T')] ✅ 完成 $dest"
  else
    mv "$dest.part" "$dest.bad"; echo "❌ sha256 與 HF 不符，改名 $dest.bad"
  fi
done
echo "[$(date '+%F %T')] NAS 拉取結束"
