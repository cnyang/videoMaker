"""GX10 的一次性 Nemotron 部署；--check 只檢查環境，不下載或使用 GPU。"""

import argparse
import base64
import fcntl
import json
import platform
import shutil
import signal
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

MODEL = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
REVISION = "579ffa0b659bc681b89e649fa7c2d5089399602e"
# 官方 GB10 nightly 的 ARM64 digest；不跟著 latest 改變執行環境。
IMAGE = "vllm/vllm-openai@sha256:fd33bec1578f312a01547cc6b9691a5154c3e8bcfb76d2b35acb657bfc4eee8f"
NAME = "vmk-nemotron-3-5-super-vl"
DOWNLOAD = NAME + "-download"
ROOT = Path.home() / "code/nemotron-3.5-super-vl"
API = "http://127.0.0.1:8000"
GIB = 2**30
MIN_FREE_GB = 40


def api(url, data=None, timeout=30):
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def available_gb():
    info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return int(info["MemAvailable"].split()[0]) * 1024 / GIB


def ensure_memory():
    free = available_gb()
    if free < MIN_FREE_GB:
        raise RuntimeError(f"剩餘記憶體 {free:.1f}GiB < {MIN_FREE_GB}GiB，中止")


def check_environment():
    if platform.machine() != "aarch64":
        raise RuntimeError("本部署固定使用 GX10 的 ARM64 映像")
    for command in ("docker", "nvidia-smi"):
        if not shutil.which(command):
            raise RuntimeError(f"缺少 {command}")
    docker("info", stdout=subprocess.DEVNULL)
    for name in (NAME, DOWNLOAD):
        found = subprocess.run(["docker", "inspect", name], capture_output=True, check=False)
        if found.returncode == 0:
            raise RuntimeError(f"容器 {name} 已存在，保留原狀，請先人工確認")
    if shutil.disk_usage(Path.home()).free < 110 * GIB:
        raise RuntimeError("磁碟不足 110GiB，不開始下載")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8000))
    print(f"環境檢查通過：{MODEL}@{REVISION}；文字／圖片，8K context、單一請求", flush=True)


def prepare_gpu():
    queue = api("http://127.0.0.1:8188/queue")
    if queue.get("queue_running") or queue.get("queue_pending"):
        raise RuntimeError("ComfyUI 有工作，保留原狀並中止")
    # 只卸載閒置的 ComfyUI 快取；Ollama 或其他服務仍在用 GPU 時不搶資源。
    if api("http://127.0.0.1:11434/api/ps").get("models"):
        raise RuntimeError("Ollama 仍有模型常駐，保留原狀並中止")
    api("http://127.0.0.1:8188/free", {"unload_models": True, "free_memory": True})
    time.sleep(5)
    ensure_memory()
    if available_gb() < 85:
        raise RuntimeError("載入 75GB 權重前可用記憶體不足 85GiB，中止")


def blue_png():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    pixels = (b"\0" + b"\0\0\xff" * 256) * 256
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 256, 256, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def verify():
    image = "data:image/png;base64," + base64.b64encode(blue_png()).decode()
    replies = {}
    for name, content, expected in (
        ("text", "What is 2 + 2? Reply with only the digit.", "4"),
        (
            "image",
            [
                {
                    "type": "text",
                    "text": "What is the dominant color? Reply with only one English color word.",
                },
                {"type": "image_url", "image_url": {"url": image}},
            ],
            "blue",
        ),
    ):
        data = api(
            API + "/v1/chat/completions",
            {
                "model": "nemotron-3.5-super-vl",
                "messages": [{"role": "user", "content": content}],
                "temperature": 0,
                "max_tokens": 64,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=300,
        )
        choice = data["choices"][0]
        answer = (choice["message"].get("content") or "").strip()
        if choice.get("finish_reason") != "stop" or answer.lower().strip(".!") != expected:
            raise RuntimeError(f"{name} 推論驗證失敗：{answer!r}")
        replies[name] = answer
    return replies


def install():
    ROOT.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "started": datetime.now().astimezone().isoformat(), "model": MODEL}
    report.update(revision=REVISION, image=IMAGE)
    created = []
    try:
        check_environment()
        docker("pull", IMAGE, timeout=3600)
        models = ROOT / "model"
        models.mkdir(exist_ok=True)
        download = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download({MODEL!r}, revision={REVISION!r}, local_dir='/models', max_workers=2)"
        )
        docker(
            "create", "--name", DOWNLOAD, "--user", f"{ROOT.stat().st_uid}:{ROOT.stat().st_gid}",
            "-e", "HF_HOME=/models/.hf", "-e", "HF_HUB_DISABLE_XET=1",
            "-v", f"{models}:/models", "--entrypoint", "python3", IMAGE, "-c", download,
        )  # fmt: skip
        created.append(DOWNLOAD)
        docker("start", "--attach", DOWNLOAD, timeout=6 * 3600)
        state = json.loads(docker("inspect", DOWNLOAD, capture_output=True, text=True).stdout)[0]["State"]
        if state["ExitCode"] != 0:
            raise RuntimeError(f"下載容器退出碼：{state['ExitCode']}")
        prepare_gpu()
        docker(
            "create", "--name", NAME, "--gpus", "all", "--ipc=host", "--restart", "no",
            "-p", "127.0.0.1:8000:8000", "-v", f"{models}:/models:ro",
            "--entrypoint", "vllm", IMAGE, "serve", "/models",
            "--host", "0.0.0.0", "--port", "8000", "--served-model-name", "nemotron-3.5-super-vl",
            "--trust-remote-code", "--max-num-seqs", "1", "--max-model-len", "8192",
            "--max-num-batched-tokens", "2048", "--gpu-memory-utilization", "0.65", "--enforce-eager",
            "--kv-cache-memory-bytes", "1073741824",
            "--mamba-backend", "flashinfer", "--mamba-ssm-cache-dtype", "float16",
            "--enable-mamba-cache-stochastic-rounding", "--mamba-cache-philox-rounds", "5",
            "--enable-mamba-shared-prefix-checkpoint", "--block-size", "64", "--prefix-match-unit", "64",
            "--limit-mm-per-prompt", '{"image": 1, "video": 0}', "--reasoning-parser", "nemotron_v3",
        )  # fmt: skip
        created.append(NAME)
        docker("start", NAME)
        deadline = time.monotonic() + 1800
        while True:
            ensure_memory()
            state = json.loads(docker("inspect", NAME, capture_output=True, text=True).stdout)[0]["State"]
            if not state["Running"]:
                raise RuntimeError(f"推論容器已退出：{state['ExitCode']}，請看 docker logs {NAME}")
            try:
                api(API + "/v1/models", timeout=5)
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("推論服務啟動超過 30 分鐘") from None
                time.sleep(10)
        report["replies"] = verify()
        ensure_memory()
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        # 保留權重與容器供查錯／手動啟動；驗證後釋放 GPU，不自動接入影片管線。
        for name in reversed(created):
            try:
                stopped = subprocess.run(["docker", "stop", "--time", "30", name], timeout=60, check=False)
                if stopped.returncode:
                    report.update(status="failed", stop_error=f"無法停止 {name}")
            except (OSError, subprocess.TimeoutExpired) as error:
                report.update(status="failed", stop_error=str(error))
        report["finished"] = datetime.now().astimezone().isoformat()
        target = ROOT / "install-result.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temp.replace(target)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check_environment()
    else:

        def interrupted(signum, frame):
            raise InterruptedError(f"收到訊號 {signum}")

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        with Path("/tmp/vmk-nightly.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install()
