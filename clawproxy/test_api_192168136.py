#!/usr/bin/env python3
"""
Qwen3-TTS API Test Script
Target server: http://192.168.1.36:8880

Tests all major endpoints:
  - GET  /health
  - GET  /v1/models
  - GET  /v1/audio/voices
  - POST /v1/audio/speech  (basic TTS, all voices, formats, languages, streaming)
  - GET  /v1/audio/voice-clone/capabilities
  - POST /v1/audio/voice-clone
"""

import os
import sys
import time
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_URL   = "http://192.168.1.37:8880"
OUTPUT_DIR = Path("./tts_test_output")
TIMEOUT    = 120  # seconds per request

# Colours (disabled automatically when not a TTY)
_USE_COLOR = sys.stdout.isatty()
def _c(code, text): return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text
OK   = lambda t: _c("32", f"✓ {t}")
FAIL = lambda t: _c("31", f"✗ {t}")
INFO = lambda t: _c("36", f"  {t}")
HEAD = lambda t: _c("1;34", f"\n{'─'*60}\n  {t}\n{'─'*60}")

# ── Helpers ─────────────────────────────────────────────────────────────────────

def save_audio(data: bytes, name: str, fmt: str = "mp3") -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = OUTPUT_DIR / f"{ts}_{name}.{fmt}"
    path.write_bytes(data)
    return path


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(OK(label) + (f"  {detail}" if detail else ""))
    else:
        print(FAIL(label) + (f"  {detail}" if detail else ""))
    return condition


passed = failed = 0

def result(ok: bool):
    global passed, failed
    if ok: passed += 1
    else:  failed += 1
    return ok


# ── Test functions ───────────────────────────────────────────────────────────────

def test_health():
    print(HEAD("1. Health check"))
    try:
        t0 = time.time()
        r = requests.get(f"{BASE_URL}/health", timeout=10)
        ms = int((time.time() - t0) * 1000)
        ok = result(check("GET /health → 200", r.status_code == 200, f"({ms} ms)"))
        if r.status_code == 200:
            body = r.json()
            print(INFO(f"status : {body.get('status')}"))
            print(INFO(f"backend: {body.get('backend', 'n/a')}"))
            print(INFO(f"ready  : {body.get('ready', 'n/a')}"))
        return ok
    except Exception as e:
        result(check("GET /health", False, str(e)))
        return False


def test_models():
    print(HEAD("2. List models"))
    try:
        r = requests.get(f"{BASE_URL}/v1/models", timeout=10)
        ok = result(check("GET /v1/models → 200", r.status_code == 200))
        if r.status_code == 200:
            models = r.json().get("data", [])
            ids = [m["id"] for m in models]
            print(INFO(f"Found {len(ids)} models: {', '.join(ids[:8])}{'…' if len(ids) > 8 else ''}"))
        return ok
    except Exception as e:
        result(check("GET /v1/models", False, str(e)))
        return False


def test_voices():
    print(HEAD("3. List voices"))
    try:
        r = requests.get(f"{BASE_URL}/v1/audio/voices", timeout=10)
        ok = result(check("GET /v1/audio/voices → 200", r.status_code == 200))
        if r.status_code == 200:
            voices = r.json().get("voices", [])
            names = [v.get("voice_id") or v.get("name") for v in voices]
            print(INFO(f"Found {len(names)} voices: {', '.join(str(n) for n in names)}"))
        return ok
    except Exception as e:
        result(check("GET /v1/audio/voices", False, str(e)))
        return False


def test_basic_tts():
    print(HEAD("4. Basic TTS — POST /v1/audio/speech"))

    payload = {
        "model": "qwen3-tts",
        "input": "Hello! This is a basic test of the Qwen3 TTS API.",
        "voice": "Vivian",
        "response_format": "mp3",
        "speed": 1.0,
    }

    try:
        t0 = time.time()
        r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        ok = result(check("POST /v1/audio/speech (basic)", r.status_code == 200,
                          f"({ms} ms, {len(r.content)} bytes)"))
        if ok:
            p = save_audio(r.content, "basic_vivian", "mp3")
            print(INFO(f"Saved → {p}"))
        else:
            print(INFO(f"Response: {r.status_code} {r.text[:200]}"))
        return ok
    except Exception as e:
        result(check("POST /v1/audio/speech (basic)", False, str(e)))
        return False


def test_chinese_tts():
    print(HEAD("5. Chinese TTS"))

    payload = {
        "model": "tts-1-zh",
        "input": "你好！这是一个中文语音合成测试。Qwen3 TTS 支持多种语言。",
        "voice": "Vivian",
        "response_format": "wav",
        "language": "Chinese",
        "speed": 1.0,
    }

    try:
        t0 = time.time()
        r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        ok = result(check("POST /v1/audio/speech (Chinese)", r.status_code == 200,
                          f"({ms} ms, {len(r.content)} bytes)"))
        if ok:
            p = save_audio(r.content, "chinese_vivian", "wav")
            print(INFO(f"Saved → {p}"))
        else:
            print(INFO(f"Response: {r.status_code} {r.text[:200]}"))
        return ok
    except Exception as e:
        result(check("POST /v1/audio/speech (Chinese)", False, str(e)))
        return False


def test_all_openai_voices():
    print(HEAD("6. All OpenAI-mapped voices"))
    # supported by this model: alloy→vivian, echo→ryan
    voices = ["alloy", "echo"]
    text   = "Testing voice synthesis with different voice options."
    all_ok = True

    for voice in voices:
        payload = {
            "model": "tts-1",
            "input": text,
            "voice": voice,
            "response_format": "mp3",
        }
        try:
            t0 = time.time()
            r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
            ms = int((time.time() - t0) * 1000)
            ok = result(check(f"voice={voice}", r.status_code == 200,
                              f"({ms} ms, {len(r.content)} bytes)"))
            if ok:
                save_audio(r.content, f"voice_{voice}", "mp3")
            else:
                print(INFO(f"{r.status_code}: {r.text[:100]}"))
            all_ok = all_ok and ok
        except Exception as e:
            result(check(f"voice={voice}", False, str(e)))
            all_ok = False

    return all_ok


def test_qwen_voices():
    print(HEAD("7. Native Qwen voices"))
    # supported built-in speakers: vivian, ryan, aiden, dylan, eric, ono_anna, serena, sohee, uncle_fu
    qwen_voices = ["Vivian", "Ryan", "aiden", "serena"]
    text = "Hello, this is a native Qwen voice test."
    all_ok = True

    for voice in qwen_voices:
        payload = {
            "model": "qwen3-tts",
            "input": text,
            "voice": voice,
            "response_format": "mp3",
        }
        try:
            t0 = time.time()
            r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
            ms = int((time.time() - t0) * 1000)
            ok = result(check(f"voice={voice}", r.status_code == 200,
                              f"({ms} ms, {len(r.content)} bytes)"))
            if ok:
                save_audio(r.content, f"qwen_{voice.lower()}", "mp3")
            else:
                print(INFO(f"{r.status_code}: {r.text[:100]}"))
            all_ok = all_ok and ok
        except Exception as e:
            result(check(f"voice={voice}", False, str(e)))
            all_ok = False

    return all_ok


def test_audio_formats():
    print(HEAD("8. Audio output formats"))
    formats = ["mp3", "wav", "opus", "flac"]
    text    = "Testing different audio output formats."
    all_ok  = True

    for fmt in formats:
        payload = {
            "model": "tts-1",
            "input": text,
            "voice": "Vivian",
            "response_format": fmt,
        }
        try:
            t0 = time.time()
            r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
            ms = int((time.time() - t0) * 1000)
            ok = result(check(f"format={fmt}", r.status_code == 200,
                              f"({ms} ms, {len(r.content)} bytes, "
                              f"Content-Type={r.headers.get('content-type', 'n/a')})"))
            if ok:
                save_audio(r.content, f"format_{fmt}", fmt)
            else:
                print(INFO(f"{r.status_code}: {r.text[:100]}"))
            all_ok = all_ok and ok
        except Exception as e:
            result(check(f"format={fmt}", False, str(e)))
            all_ok = False

    return all_ok


def test_speed_variants():
    print(HEAD("9. Speech speed variants"))
    speeds = [0.5, 0.75, 1.0, 1.5, 2.0]
    text   = "Testing speech speed control."
    all_ok = True

    for speed in speeds:
        payload = {
            "model": "tts-1",
            "input": text,
            "voice": "Vivian",
            "response_format": "mp3",
            "speed": speed,
        }
        try:
            t0 = time.time()
            r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
            ms = int((time.time() - t0) * 1000)
            ok = result(check(f"speed={speed}", r.status_code == 200,
                              f"({ms} ms, {len(r.content)} bytes)"))
            if ok:
                save_audio(r.content, f"speed_{str(speed).replace('.','_')}", "mp3")
            all_ok = all_ok and ok
        except Exception as e:
            result(check(f"speed={speed}", False, str(e)))
            all_ok = False

    return all_ok


def test_instruct_style():
    print(HEAD("10. Instruct / emotion control"))

    cases = [
        ("excited",  "Wow, this is absolutely amazing news!",  "Speak with great excitement and enthusiasm."),
        ("calm",     "Please remain calm and take a deep breath.", "Speak softly and calmly."),
        ("whisper",  "This is a secret, don't tell anyone.",   "Speak in a quiet whisper."),
    ]

    all_ok = True
    for name, text, instruct in cases:
        payload = {
            "model": "qwen3-tts",
            "input": text,
            "voice": "Vivian",
            "response_format": "mp3",
            "instruct": instruct,
        }
        try:
            t0 = time.time()
            r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
            ms = int((time.time() - t0) * 1000)
            ok = result(check(f"instruct={name}", r.status_code == 200,
                              f"({ms} ms, {len(r.content)} bytes)"))
            if ok:
                save_audio(r.content, f"instruct_{name}", "mp3")
            else:
                print(INFO(f"{r.status_code}: {r.text[:100]}"))
            all_ok = all_ok and ok
        except Exception as e:
            result(check(f"instruct={name}", False, str(e)))
            all_ok = False

    return all_ok


def test_streaming():
    print(HEAD("11. Streaming TTS"))

    payload = {
        "model": "tts-1",
        "input": "This is a streaming test. The audio should arrive in chunks as it is generated.",
        "voice": "Vivian",
        "response_format": "mp3",
        "stream": True,
    }

    try:
        t0 = time.time()
        chunks = []
        with requests.post(f"{BASE_URL}/v1/audio/speech", json=payload,
                           stream=True, timeout=TIMEOUT) as r:
            ok = result(check("POST /v1/audio/speech (stream=True) → 200",
                              r.status_code == 200))
            if ok:
                for chunk in r.iter_content(chunk_size=4096):
                    if chunk:
                        chunks.append(chunk)
                ms = int((time.time() - t0) * 1000)
                total = sum(len(c) for c in chunks)
                print(INFO(f"Received {len(chunks)} chunks, {total} bytes total ({ms} ms)"))
                p = save_audio(b"".join(chunks), "streaming", "mp3")
                print(INFO(f"Saved → {p}"))
            else:
                print(INFO(f"Response: {r.status_code} {r.text[:200]}"))
        return ok
    except Exception as e:
        result(check("POST /v1/audio/speech (streaming)", False, str(e)))
        return False


def test_multilingual():
    print(HEAD("12. Multi-language TTS"))

    cases = [
        ("Japanese",  "こんにちは、これは日本語のテストです。",       "ja"),
        ("Korean",    "안녕하세요, 이것은 한국어 테스트입니다.",       "ko"),
        ("French",    "Bonjour, ceci est un test en français.",    "fr"),
        ("Spanish",   "Hola, esto es una prueba en español.",       "es"),
        ("German",    "Hallo, das ist ein Test auf Deutsch.",       "de"),
    ]

    all_ok = True
    for lang, text, code in cases:
        payload = {
            "model": f"tts-1-{code}",
            "input": text,
            "voice": "Vivian",
            "response_format": "mp3",
            "language": lang,
        }
        try:
            t0 = time.time()
            r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
            ms = int((time.time() - t0) * 1000)
            ok = result(check(f"language={lang}", r.status_code == 200,
                              f"({ms} ms, {len(r.content)} bytes)"))
            if ok:
                save_audio(r.content, f"lang_{code}", "mp3")
            else:
                print(INFO(f"{r.status_code}: {r.text[:100]}"))
            all_ok = all_ok and ok
        except Exception as e:
            result(check(f"language={lang}", False, str(e)))
            all_ok = False

    return all_ok


def test_voice_clone_capabilities():
    print(HEAD("13. Voice-clone capabilities"))
    try:
        r = requests.get(f"{BASE_URL}/v1/audio/voice-clone/capabilities", timeout=10)
        ok = result(check("GET /v1/audio/voice-clone/capabilities → 200",
                          r.status_code == 200))
        if ok:
            data = r.json()
            print(INFO(f"supported      : {data.get('supported')}"))
            print(INFO(f"max_audio_len  : {data.get('max_audio_length_seconds', 'n/a')} s"))
            print(INFO(f"supported_fmts : {data.get('supported_audio_formats', 'n/a')}"))
        return ok
    except Exception as e:
        result(check("GET /v1/audio/voice-clone/capabilities", False, str(e)))
        return False


def test_voice_clone(ref_audio: str | None):
    print(HEAD("14. Voice cloning — POST /v1/audio/voice-clone"))

    if not ref_audio:
        print(INFO("Skipped — no --ref-audio provided. "
                   "Pass --ref-audio /path/to/file.wav to enable this test."))
        return True

    ref_path = Path(ref_audio)
    if not ref_path.exists():
        result(check("Voice clone (file exists)", False, f"{ref_audio} not found"))
        return False

    try:
        with open(ref_path, "rb") as f:
            t0 = time.time()
            r = requests.post(
                f"{BASE_URL}/v1/audio/voice-clone",
                files={"reference_audio": (ref_path.name, f, "audio/wav")},
                data={
                    "input": "This is a voice cloning test using a reference audio file.",
                    "model": "qwen3-tts",
                    "response_format": "mp3",
                    "speed": "1.0",
                },
                timeout=TIMEOUT,
            )
        ms = int((time.time() - t0) * 1000)
        ok = result(check("POST /v1/audio/voice-clone", r.status_code == 200,
                          f"({ms} ms, {len(r.content)} bytes)"))
        if ok:
            p = save_audio(r.content, "voice_clone", "mp3")
            print(INFO(f"Saved → {p}"))
        else:
            print(INFO(f"Response: {r.status_code} {r.text[:300]}"))
        return ok
    except Exception as e:
        result(check("POST /v1/audio/voice-clone", False, str(e)))
        return False


def test_long_text():
    print(HEAD("15. Long text synthesis"))

    text = (
        "Qwen3-TTS is a state-of-the-art text-to-speech model developed by Alibaba. "
        "It supports multiple languages including English, Chinese, Japanese, Korean, "
        "French, Spanish, German, and more. The model can generate natural-sounding "
        "speech with various voice styles and emotions. It is designed for production "
        "use and can handle long texts efficiently. This test verifies that the API "
        "can process longer input without errors or truncation."
    )

    payload = {
        "model": "tts-1",
        "input": text,
        "voice": "Vivian",
        "response_format": "mp3",
    }

    try:
        t0 = time.time()
        r = requests.post(f"{BASE_URL}/v1/audio/speech", json=payload, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        ok = result(check(f"Long text ({len(text)} chars)", r.status_code == 200,
                          f"({ms} ms, {len(r.content)} bytes)"))
        if ok:
            p = save_audio(r.content, "long_text", "mp3")
            print(INFO(f"Saved → {p}"))
        else:
            print(INFO(f"Response: {r.status_code} {r.text[:200]}"))
        return ok
    except Exception as e:
        result(check("Long text", False, str(e)))
        return False


def test_openai_sdk_compat():
    """Optional: test via openai Python SDK if installed."""
    print(HEAD("16. OpenAI SDK compatibility (optional)"))
    try:
        from openai import OpenAI
    except ImportError:
        print(INFO("openai package not installed — skipping (pip install openai)"))
        return True

    try:
        client = OpenAI(api_key="not-needed", base_url=f"{BASE_URL}/v1")
        t0 = time.time()
        response = client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input="Hello from the OpenAI SDK compatibility test!",
        )
        ms = int((time.time() - t0) * 1000)
        audio_bytes = response.read()
        ok = result(check("OpenAI SDK → audio bytes received",
                          len(audio_bytes) > 0, f"({ms} ms, {len(audio_bytes)} bytes)"))
        if ok:
            p = save_audio(audio_bytes, "openai_sdk", "mp3")
            print(INFO(f"Saved → {p}"))
        return ok
    except Exception as e:
        result(check("OpenAI SDK compatibility", False, str(e)))
        return False


# ── Entry point ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Qwen3-TTS API test suite for http://192.168.1.36:8880"
    )
    parser.add_argument("--ref-audio", metavar="FILE",
                        help="WAV file for voice-clone test (optional)")
    parser.add_argument("--skip", nargs="*", default=[],
                        metavar="N",
                        help="Skip test numbers, e.g. --skip 6 7 12")
    parser.add_argument("--only", nargs="*", default=[],
                        metavar="N",
                        help="Run only these test numbers, e.g. --only 1 4 11")
    args = parser.parse_args()

    skip = set(args.skip)
    only = set(args.only)

    all_tests = [
        ("1",  test_health),
        ("2",  test_models),
        ("3",  test_voices),
        ("4",  test_basic_tts),
        ("5",  test_chinese_tts),
        ("6",  test_all_openai_voices),
        ("7",  test_qwen_voices),
        ("8",  test_audio_formats),
        ("9",  test_speed_variants),
        ("10", test_instruct_style),
        ("11", test_streaming),
        ("12", test_multilingual),
        ("13", test_voice_clone_capabilities),
        ("14", lambda: test_voice_clone(args.ref_audio)),
        ("15", test_long_text),
        ("16", test_openai_sdk_compat),
    ]

    print(_c("1;37", f"\nQwen3-TTS API Test Suite"))
    print(_c("1;37",  f"Server : {BASE_URL}"))
    print(_c("1;37",  f"Output : {OUTPUT_DIR.resolve()}"))
    print(_c("1;37",  f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))

    wall_start = time.time()

    for num, fn in all_tests:
        if only and num not in only:
            continue
        if num in skip:
            print(f"\n  [skip] Test {num}")
            continue
        fn()

    wall = int(time.time() - wall_start)
    total = passed + failed

    print(_c("1;34", f"\n{'═'*60}"))
    print(_c("1;37", f"  Results: {total} tests — ") +
          _c("32", f"{passed} passed") + "  " +
          (_c("31", f"{failed} FAILED") if failed else _c("32", "0 failed")))
    print(_c("1;37", f"  Total time: {wall}s"))
    print(_c("1;37", f"  Audio saved to: {OUTPUT_DIR.resolve()}"))
    print(_c("1;34", f"{'═'*60}\n"))

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
