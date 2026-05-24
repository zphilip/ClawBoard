#!/usr/bin/env python3
"""
Qwen3-TTS GGUF Docker/API smoke test.

What it checks:
1) Docker compose service is running (optionally starts it)
2) /health, /v1/models, /v1/audio/voices
3) /v1/audio/speech returns valid audio bytes

Exit code:
0 => all checks passed
1 => one or more checks failed
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LONG_CHINESE_TEXT = (
    "各位听众，大家好。今天我们进行一段较长文本的语音合成压力测试，"
    "用于验证系统在连续中文朗读场景下的稳定性、清晰度与节奏控制能力。"
    "在现代人工智能应用中，文本转语音不仅用于阅读新闻、播报信息，"
    "还广泛应用于客服系统、车载交互、教育内容和多媒体制作。"
    "为了接近真实使用环境，这段文本包含多种句式、停顿与语义转折。"
    "首先，我们希望模型能够准确处理标点符号所代表的停顿时长；"
    "其次，我们关注长句中语调是否自然，是否会出现断词或重音异常；"
    "再次，我们需要确认在连续生成超过一分钟音频时，系统依然保持稳定输出，"
    "不会出现静音、爆音或明显失真。"
    "如果你正在收听这段测试音频，请重点留意以下几个方面："
    "其一，发音是否标准清晰；其二，语速是否均匀；其三，情感是否平稳自然。"
    "最后，感谢你参与本次测试，我们将根据结果继续优化模型参数与推理链路，"
    "以便在真实生产环境中提供更高质量、更低延迟的中文语音合成体验。"
)


def ok(msg: str) -> None:
    print(f"[PASS] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def run_cmd(args: list[str], cwd: Path) -> tuple[int, str, str]:
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def http_json(method: str, url: str, body: dict[str, Any] | None = None, timeout: int = 60) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url=url, data=data, method=method, headers=headers)

    with urlopen(req, timeout=timeout) as resp:
        status = resp.status
        payload = resp.read().decode("utf-8")
    parsed = json.loads(payload)
    return status, parsed


def http_bytes(method: str, url: str, body: dict[str, Any], timeout: int = 600) -> tuple[int, bytes, str]:
    req = Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as resp:
        status = resp.status
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read()
    return status, data, content_type


def looks_like_audio(fmt: str, data: bytes) -> bool:
    if not data:
        return False
    fmt = fmt.lower()
    if fmt == "wav":
        return data.startswith(b"RIFF") and b"WAVE" in data[:16]
    if fmt == "mp3":
        return data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
    if fmt == "flac":
        return data.startswith(b"fLaC")
    if fmt == "pcm":
        return len(data) > 256
    return len(data) > 256


def wait_for_health(base_url: str, timeout_sec: int) -> bool:
    deadline = time.time() + timeout_sec
    last_err = ""
    while time.time() < deadline:
        try:
            status, payload = http_json("GET", f"{base_url}/health", timeout=15)
            if status == 200 and payload.get("status") in {"ok", "degraded"}:
                return True
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(2)
    if last_err:
        info(f"Last health-check error: {last_err}")
    return False


def check_required_files(project_root: Path) -> bool:
    model_dir = project_root / "models"
    required = [
        "qwen3_tts_talker.q5_k.gguf",
        "qwen3_tts_predictor.q8_0.gguf",
        "qwen3_tts_decoder.fp16.onnx",
        "qwen3_tts_codec_encoder.fp16.onnx",
        "tokenizer.json",
        "embeddings/text_embedding_projected.npy",
    ]
    missing = [name for name in required if not (model_dir / name).exists()]
    if missing:
        fail(f"Missing model artifacts in models/: {', '.join(missing)}")
        return False
    ok("Required model artifacts exist under models/")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen3-TTS GGUF Docker/API smoke test")
    parser.add_argument("--project-root", default=".", help="Path to service-Qwen3-TTS-GGUF project")
    parser.add_argument("--service", default="qwen3-tts-gguf", help="Docker compose service name")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8882)
    parser.add_argument("--voice", default="serena")
    parser.add_argument("--text", default="Hello, this is a Qwen3-TTS docker smoke test.")
    parser.add_argument(
        "--use-long-chinese-text",
        action="store_true",
        help="Use built-in long Chinese paragraph for synthesis test",
    )
    parser.add_argument("--format", default="wav", choices=["wav", "mp3", "flac", "pcm"])
    parser.add_argument("--max-steps", type=int, default=None, help="Optional override for API max_steps")
    parser.add_argument("--output", default="/tmp/qwen3_tts_smoke.wav", help="Where to store synthesized audio")
    parser.add_argument("--start-service", action="store_true", help="Run docker compose up -d before checks")
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--skip-speech", action="store_true", help="Skip /v1/audio/speech call")
    args = parser.parse_args()

    if args.use_long_chinese_text:
        args.text = LONG_CHINESE_TEXT
        info("Using built-in long Chinese synthesis text")

    root = Path(args.project_root).resolve()
    compose_file = root / "docker-compose.yml"
    if not compose_file.exists():
        fail(f"docker-compose.yml not found under {root}")
        return 1

    base_url = f"http://{args.host}:{args.port}"
    all_ok = True

    if not check_required_files(root):
        all_ok = False

    if args.start_service:
        info("Starting docker compose service")
        code, out, err = run_cmd(["docker", "compose", "up", "-d", args.service], cwd=root)
        if code != 0:
            fail(f"docker compose up failed: {err or out}")
            return 1
        ok(f"docker compose service started: {args.service}")

    code, out, err = run_cmd(["docker", "compose", "ps", args.service], cwd=root)
    if code != 0:
        fail(f"docker compose ps failed: {err or out}")
        return 1
    if "Up" not in out:
        fail(f"Service not running: {args.service}")
        info(out)
        return 1
    ok(f"Service is running: {args.service}")

    if not wait_for_health(base_url, args.wait_seconds):
        fail(f"Health endpoint did not become ready: {base_url}/health")
        return 1

    try:
        status, payload = http_json("GET", f"{base_url}/health")
        if status == 200 and payload.get("models_ok") is True:
            ok("/health returned 200 and models_ok=true")
        else:
            fail(f"Unexpected /health payload: {payload}")
            all_ok = False
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        fail(f"/health request failed: {e}")
        return 1

    try:
        status, payload = http_json("GET", f"{base_url}/v1/models")
        if status == 200 and isinstance(payload.get("data"), list):
            ok("/v1/models returned model list")
        else:
            fail(f"Unexpected /v1/models payload: {payload}")
            all_ok = False
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        fail(f"/v1/models request failed: {e}")
        all_ok = False

    try:
        status, payload = http_json("GET", f"{base_url}/v1/audio/voices")
        voices = payload.get("voices", []) if isinstance(payload, dict) else []
        if status == 200 and isinstance(voices, list) and len(voices) > 0:
            ok(f"/v1/audio/voices returned {len(voices)} voices")
        else:
            fail(f"Unexpected /v1/audio/voices payload: {payload}")
            all_ok = False
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        fail(f"/v1/audio/voices request failed: {e}")
        all_ok = False

    if not args.skip_speech:
        body = {
            "model": "qwen3-tts",
            "input": args.text,
            "voice": args.voice,
            "response_format": args.format,
        }
        if args.max_steps is not None:
            body["max_steps"] = args.max_steps

        try:
            status, data, content_type = http_bytes("POST", f"{base_url}/v1/audio/speech", body)
            if status != 200:
                fail(f"/v1/audio/speech returned status {status}")
                all_ok = False
            elif not looks_like_audio(args.format, data):
                fail(
                    "/v1/audio/speech did not return valid audio bytes "
                    f"(content-type={content_type}, size={len(data)})"
                )
                try:
                    info(f"Response preview: {data[:256].decode('utf-8', errors='replace')}")
                except Exception:  # noqa: BLE001
                    pass
                all_ok = False
            else:
                out_path = Path(args.output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(data)
                ok(f"/v1/audio/speech returned valid {args.format} audio ({len(data)} bytes)")
                info(f"Saved sample audio to {out_path}")
        except HTTPError as e:
            body_txt = e.read().decode("utf-8", errors="replace")
            fail(f"/v1/audio/speech HTTPError {e.code}: {body_txt}")
            all_ok = False
        except (URLError, TimeoutError) as e:
            fail(f"/v1/audio/speech request failed: {e}")
            all_ok = False

    if not all_ok:
        info("Recent service logs (last 60 lines):")
        code, out, err = run_cmd(["docker", "compose", "logs", "--tail=60", args.service], cwd=root)
        if code == 0:
            print(out)
        else:
            print(err or out)
        return 1

    ok("All smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
