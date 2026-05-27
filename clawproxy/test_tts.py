#!/usr/bin/env python3
"""
test_tts.py — manual integration test for the clawproxy /tts/synthesize endpoint.

Usage:
    # Start clawproxy in proxy mode (in another terminal):
    #   ./clawproxy --proxy --tts-provider openai
    #   (OPENAI_API_KEY must be set in the environment)

    python3 test_tts.py [--host 127.0.0.1] [--port 18780] [--text "Hello world"]
                        [--provider openai] [--voice alloy] [--format mp3]
                        [--out output.mp3] [--raw]

Options:
    --host      clawproxy host (default: 127.0.0.1)
    --port      clawproxy proxy port (default: 18780)
    --text      text to synthesise (default: "Hello from clawproxy TTS!")
    --provider  TTS provider to request (default: use server default)
    --voice     voice ID (default: use server default)
    --format    audio format (default: mp3)
    --out       save audio to this file (default: tts_output.<format>)
    --raw       request raw audio bytes (Accept: audio/*) instead of JSON+base64
    --info      just print /tts/info and exit
"""

import argparse
import base64
import json
import os
import sys
import urllib.request
import urllib.error


MIMO_PRESET_VOICES = [
    "mimo_default", "冰糖", "茉莉", "苏打", "白桦",
    "Mia", "Chloe", "Milo", "Dean",
]


def synthesize_one(base_url: str, text: str, provider: str, voice: str,
                   fmt: str, raw: bool, out: str) -> bool:
    """POST to /tts/synthesize, save audio, return True on success."""
    url = base_url + "/tts/synthesize"
    body: dict = {"text": text, "format": fmt}
    if provider:
        body["provider"] = provider
    if voice:
        body["voice"] = voice

    body_bytes = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if raw:
        headers["Accept"] = "audio/*"

    print(f"POST {url}")
    print(f"  body: {json.dumps(body)}")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            content_type = resp.headers.get("Content-Type", "")
            print(f"  status:  {resp.status}")
            if raw:
                audio_bytes = resp.read()
                out_file = out or f"tts_output.{fmt}"
                with open(out_file, "wb") as f:
                    f.write(audio_bytes)
                print(f"  saved:   {out_file} ({len(audio_bytes):,} bytes)")
            else:
                data = json.loads(resp.read())
                print(f"  provider: {data.get('provider')}")
                print(f"  voice:    {data.get('voice')}")
                print(f"  format:   {data.get('format')}")
                audio_b64 = data.get("audio_b64", "")
                audio_bytes = base64.b64decode(audio_b64)
                out_format = data.get("format", fmt)
                out_file = out or f"tts_output.{out_format}"
                with open(out_file, "wb") as f:
                    f.write(audio_bytes)
                print(f"  saved:    {out_file} ({len(audio_bytes):,} bytes)")
        return True
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.reason}")
        try:
            body_err = json.loads(e.read())
            msg = body_err.get("error") or body_err.get("message") or body_err
            print(f"  error:   {msg}")
            if e.code in (401, 403):
                print("  HINT: API key is missing or invalid — check MIMO_API_KEY / --tts-mimo-key")
        except Exception:
            pass
        return False
    except urllib.error.URLError as e:
        print(f"  ERROR: cannot connect — is clawproxy running? detail: {e}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Test the clawproxy TTS endpoint")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18780)
    ap.add_argument("--text", default="Hello from clawproxy TTS!")
    ap.add_argument("--provider", default="")
    ap.add_argument("--voice", default="")
    ap.add_argument("--format", default="mp3")
    ap.add_argument("--out", default="")
    ap.add_argument("--raw", action="store_true", help="Request raw audio (no JSON wrapper)")
    ap.add_argument("--info", action="store_true", help="Print /tts/info and exit")
    # MiMo shortcuts
    ap.add_argument("--mimo", action="store_true",
                    help="Quick MiMo TTS test: synthesise --text with mimotts provider")
    ap.add_argument("--mimo-voice", default="mimo_default",
                    help="Voice for --mimo / --mimo-batch (default: mimo_default)")
    ap.add_argument("--mimo-model", default="",
                    help="Override MiMo model via voice param for voicedesign/voiceclone modes")
    ap.add_argument("--mimo-batch", action="store_true",
                    help="Test all MiMo preset voices in sequence")
    args = ap.parse_args()

    base_url = f"http://{args.host}:{args.port}"

    # ── /tts/info ────────────────────────────────────────────────────────────
    if args.info:
        url = base_url + "/tts/info"
        print(f"GET {url}")
        try:
            with urllib.request.urlopen(url) as resp:
                data = json.loads(resp.read())
                print(json.dumps(data, indent=2))
                mimo = data.get("providers", {}).get("mimotts", {})
                if mimo:
                    configured = mimo.get("configured", False)
                    print(f"\nMiMo configured: {configured}")
                    if not configured:
                        print("  HINT: set MIMO_API_KEY env var or use --tts-mimo-key")
        except urllib.error.URLError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        return

    # ── MiMo batch: test all preset voices ───────────────────────────────────
    if args.mimo_batch:
        text = args.text if args.text != "Hello from clawproxy TTS!" else "你好，这是MiMo语音合成测试。Hello from MiMo TTS!"
        print(f"=== MiMo batch test — {len(MIMO_PRESET_VOICES)} voices ===")
        passed, failed = 0, 0
        for voice in MIMO_PRESET_VOICES:
            print(f"\n--- voice: {voice} ---")
            out_file = f"mimo_{voice.replace(' ', '_')}.wav"
            ok = synthesize_one(base_url, text, "mimotts", voice, "wav", False, out_file)
            if ok:
                passed += 1
            else:
                failed += 1
        print(f"\n=== batch done: {passed} passed, {failed} failed ===")
        sys.exit(0 if failed == 0 else 1)

    # ── MiMo quick test ───────────────────────────────────────────────────────
    if args.mimo:
        text = args.text if args.text != "Hello from clawproxy TTS!" else "你好，这是MiMo语音合成测试。Hello from MiMo TTS!"
        voice = args.mimo_voice
        print(f"=== MiMo TTS quick test (voice={voice}) ===")
        ok = synthesize_one(base_url, text, "mimotts", voice, "wav", False,
                            args.out or f"mimo_output_{voice}.wav")
        sys.exit(0 if ok else 1)

    # ── /tts/synthesize (generic) ────────────────────────────────────────────
    url = base_url + "/tts/synthesize"
    body: dict = {"text": args.text, "format": args.format}
    if args.provider:
        body["provider"] = args.provider
    if args.voice:
        body["voice"] = args.voice

    body_bytes = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if args.raw:
        headers["Accept"] = "audio/*"

    print(f"POST {url}")
    print(f"  body:    {json.dumps(body)}")
    print(f"  mode:    {'raw audio' if args.raw else 'JSON+base64'}")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            content_type = resp.headers.get("Content-Type", "")
            print(f"  status:  {resp.status}")
            print(f"  content: {content_type}")

            if args.raw:
                # Raw audio — save directly
                audio_bytes = resp.read()
                out_file = args.out or f"tts_output.{args.format}"
                with open(out_file, "wb") as f:
                    f.write(audio_bytes)
                print(f"  saved:   {out_file} ({len(audio_bytes):,} bytes)")
                print(f"  headers:")
                for h in ("X-Tts-Provider", "X-Tts-Voice", "X-Tts-Format", "X-Tts-Text"):
                    v = resp.headers.get(h, "")
                    if v:
                        print(f"    {h}: {v}")
            else:
                # JSON response — decode base64 and save
                data = json.loads(resp.read())
                print(f"  provider: {data.get('provider')}")
                print(f"  voice:    {data.get('voice')}")
                print(f"  format:   {data.get('format')}")
                print(f"  text:     {data.get('text')}")

                audio_b64 = data.get("audio_b64", "")
                audio_bytes = base64.b64decode(audio_b64)
                out_format = data.get("format", args.format)
                out_file = args.out or f"tts_output.{out_format}"
                with open(out_file, "wb") as f:
                    f.write(audio_bytes)
                print(f"  saved:    {out_file} ({len(audio_bytes):,} bytes)")

    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.reason}")
        try:
            body_err = json.loads(e.read())
            msg = body_err.get("error") or body_err.get("message") or body_err
            print(f"  error:   {msg}")
            if e.code in (401, 403):
                print("  HINT: API key is missing or invalid")
        except Exception:
            pass
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: cannot connect to {url} — is clawproxy running with --proxy?")
        print(f"  detail: {e}")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
