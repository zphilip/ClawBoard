#!/usr/bin/env python3
"""
TTS Speed Benchmark: F5-TTS vs Qwen3-TTS
=========================================
Synthesises the same texts with both providers and prints a side-by-side
timing table.

F5-TTS  : POST /voice-clone/synthesize_speech  (async polling)
Qwen3   : POST /v1/audio/speech                (sync, OpenAI-compatible)

Usage:
    python test_tts_benchmark.py [options]

Options:
    --f5-url    URL   F5-TTS server   (default: http://apicn.aiworm.cn:8010)
    --q3-url    URL   Qwen3-TTS server (default: http://apicn.aiworm.cn:8011)
    --f5-key    KEY   F5-TTS Bearer token  (or env F5_TTS_API_KEY)
    --q3-key    KEY   Qwen3 Bearer token   (or env QWEN3_TTS_API_KEY)
    --f5-voice  NAME  F5-TTS voice name    (default: demo_speaker0)
    --q3-voice  NAME  Qwen3-TTS voice      (default: Vivian)
    --q3-model  NAME  Qwen3-TTS model      (default: qwen3-tts)
    --timeout   SECS  Per-request timeout  (default: 900)
    --output    DIR   Directory to save audio files (default: ./tts_benchmark_out)
    --no-save         Do not save audio to disk
"""

import argparse
import os
import sys
import time
import json
import requests
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ── Colours ────────────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()
def _c(code, t): return f"\033[{code}m{t}\033[0m" if _TTY else t
def green(t):  return _c("32", t)
def red(t):    return _c("31", t)
def cyan(t):   return _c("36", t)
def yellow(t): return _c("33", t)
def bold(t):   return _c("1",  t)
def grey(t):   return _c("90", t)

# ── Test texts ─────────────────────────────────────────────────────────────────
# Each entry: (label, text)
TEST_CASES = [
    ("short_en",
     "Hello, how are you today?"),

    ("short_zh",
     "你好，今天天气不错。"),

    ("medium_en",
     "The quick brown fox jumps over the lazy dog. "
     "Pack my box with five dozen liquor jugs. "
     "How vexingly quick daft zebras jump!"),

    ("medium_zh",
     "今天是2026年5月23日，天气晴朗，适合出行。"
     "根据最新报道，国内各地正在积极推进乡村振兴战略，"
     "农业农村部发布了新一轮政策支持措施。"),

    ("long_zh",
     "**今日新闻汇总 (2026年5月23日 周六)**\n\n"
     "📰 **今日辟谣**\n"
     "- 流传说法\u300c可提前提取养老保险个人账户的钱\u300d系谣言，请勿相信小广告\n\n"
     "🏛️ **国内动态**\n"
     "- 2026年国际生物多样性日宣传活动在上海崇明举行，呼吁保护生态环境\n"
     "- 河南着力扩内需促消费，2025年社会消费品零售总额2.9万亿元、同比增长5.6%，居全国前列\n\n"
     "📊 **财经新闻**\n"
     "- 均胜电子：1.55亿股H股招股，多领域发展势头好\n"
     "- 2025胡润百富榜最新发布\n"
     "- 北交所重要公告：晶升股份股东拟询价转让股份\n\n"
     "📅 **今日待关注**\n"
     "- 新疆火炬 15:00-17:30 参加投资者网上集体接待日活动\n\n"
     "如需股市行情或其他具体新闻，请告诉我。"),

    ("long_en",
     "Today's technology landscape is evolving faster than ever. "
     "Artificial intelligence systems are being deployed across healthcare, "
     "finance, manufacturing, and education. "
     "Large language models can now understand and generate nuanced text in "
     "dozens of languages, summarise long documents, write code, and assist "
     "with complex reasoning tasks. "
     "Voice synthesis has reached near-human quality, enabling applications "
     "from accessibility tools to interactive voice assistants. "
     "The challenge for engineers is not just capability but latency — users "
     "expect responses in seconds, not minutes."),
]

# ── Result dataclass ────────────────────────────────────────────────────────────
@dataclass
class Result:
    provider:    str
    label:       str
    chars:       int
    elapsed_s:   float = 0.0
    bytes_out:   int   = 0
    error:       str   = ""
    audio_path:  str   = ""

    @property
    def ok(self): return self.error == ""

    @property
    def chars_per_sec(self):
        if self.elapsed_s > 0 and self.ok:
            return self.chars / self.elapsed_s
        return 0.0

# ── F5-TTS client ───────────────────────────────────────────────────────────────

def f5tts_synth(text: str, base_url: str, api_key: str,
                voice: str, timeout: int) -> tuple[bytes, float]:
    """Synthesise text with F5-TTS (async polling).  Returns (audio_bytes, elapsed_s)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Normalise voice name (bare name → resources/name.wav)
    ref_audio = voice if ("/" in voice or voice.endswith(".wav")) \
                else f"resources/{voice}.wav"

    payload = {
        "ref_audio_orig":    ref_audio,
        "gen_text":          text,
        "ref_text":          "",
        "model":             "F5TTS_v1_Base",
        "remove_silence":    False,
        "seed":              -1,
        "cross_fade_duration": 0.15,
        "nfe_step":          32,
        "speed":             1.0,
        "need_credit":       False,
    }

    t0 = time.time()

    # Submit
    r = requests.post(
        f"{base_url}/voice-clone/synthesize_speech?need_credit=false",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    task_id = r.json()["task_id"]

    # Poll status
    while True:
        sr = requests.get(
            f"{base_url}/voice-clone/status/{task_id}",
            headers=headers,
            timeout=30,
        )
        sr.raise_for_status()
        body = sr.json()
        task = body.get("task", {})
        status = task.get("status", "")
        if status == "succeeded":
            break
        if status in ("failed", "cancelled", "timeout"):
            raise RuntimeError(f"task {status}: {body.get('message', '')}")
        elapsed = time.time() - t0
        if elapsed > timeout:
            raise TimeoutError(f"polling exceeded {timeout}s")
        time.sleep(2)

    # Download
    dr = requests.get(
        f"{base_url}/voice-clone/result/{task_id}",
        headers={k: v for k, v in headers.items() if k != "Content-Type"},
        timeout=timeout,
    )
    dr.raise_for_status()
    return dr.content, time.time() - t0


def qwen3_synth(text: str, base_url: str, api_key: str,
                voice: str, model: str, timeout: int) -> tuple[bytes, float]:
    """Synthesise text with Qwen3-TTS (synchronous).  Returns (audio_bytes, elapsed_s)."""
    headers = {
        "Content-Type": "application/json",
        "Accept":        "*/*",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model":           model,
        "input":           text,
        "voice":           voice,
        "response_format": "mp3",
        "speed":           1.0,
    }

    t0 = time.time()
    r = requests.post(
        f"{base_url}/v1/audio/speech",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.content, time.time() - t0


# ── Benchmark runner ────────────────────────────────────────────────────────────

def run_one(provider: str, label: str, text: str, args) -> Result:
    chars = len(text)
    res = Result(provider=provider, label=label, chars=chars)
    try:
        if provider == "f5tts":
            audio, elapsed = f5tts_synth(
                text, args.f5_url, args.f5_key, args.f5_voice, args.timeout)
        else:
            audio, elapsed = qwen3_synth(
                text, args.q3_url, args.q3_key, args.q3_voice, args.q3_model, args.timeout)

        res.elapsed_s = elapsed
        res.bytes_out = len(audio)

        if not args.no_save and audio:
            out_dir = Path(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            ext = "wav" if provider == "f5tts" else "mp3"
            path = out_dir / f"{provider}_{label}.{ext}"
            path.write_bytes(audio)
            res.audio_path = str(path)

    except Exception as e:
        res.error = str(e)

    return res


# ── Table printer ───────────────────────────────────────────────────────────────

def print_table(results: list[Result]):
    # Group by label
    by_label: dict[str, dict[str, Result]] = {}
    for r in results:
        by_label.setdefault(r.label, {})[r.provider] = r

    col_w = [22, 6, 14, 14, 14, 14, 24]
    header = ["Test case", "Chars",
              "F5-TTS time", "F5-TTS KB", "Qwen3 time", "Qwen3 KB",
              "Winner (time)"]
    sep = "  ".join("─" * w for w in col_w)

    def fmt_cell(r: Optional[Result]) -> tuple[str, str]:
        """(time_str, kb_str)"""
        if r is None:
            return grey("(skipped)"), grey("—")
        if not r.ok:
            return red(f"ERROR"), red(r.error[:20])
        return f"{r.elapsed_s:>7.1f}s", f"{r.bytes_out/1024:>7.1f}"

    print()
    print(bold("TTS Speed Benchmark"))
    print(sep)
    print("  ".join(bold(h.ljust(w)) for h, w in zip(header, col_w)))
    print(sep)

    total: dict[str, float] = {"f5tts": 0.0, "qwen3tts": 0.0}
    wins:  dict[str, int]   = {"f5tts": 0,   "qwen3tts": 0, "tie": 0}

    for label, pmap in by_label.items():
        f5  = pmap.get("f5tts")
        q3  = pmap.get("qwen3tts")
        ft, fk = fmt_cell(f5)
        qt, qk = fmt_cell(q3)

        # Determine winner
        if f5 and f5.ok and q3 and q3.ok:
            diff = abs(f5.elapsed_s - q3.elapsed_s)
            if diff < 0.5:
                winner = yellow("tie")
                wins["tie"] += 1
            elif f5.elapsed_s < q3.elapsed_s:
                winner = green(f"F5-TTS  ({f5.elapsed_s:.1f}s vs {q3.elapsed_s:.1f}s)")
                wins["f5tts"] += 1
                total["f5tts"] += f5.elapsed_s
                total["qwen3tts"] += q3.elapsed_s
            else:
                winner = cyan(f"Qwen3   ({q3.elapsed_s:.1f}s vs {f5.elapsed_s:.1f}s)")
                wins["qwen3tts"] += 1
                total["f5tts"] += f5.elapsed_s
                total["qwen3tts"] += q3.elapsed_s
        elif f5 and f5.ok:
            winner = green("F5-TTS (Qwen3 failed)")
            wins["f5tts"] += 1
        elif q3 and q3.ok:
            winner = cyan("Qwen3 (F5 failed)")
            wins["qwen3tts"] += 1
        else:
            winner = red("both failed")

        chars = (f5 or q3).chars if (f5 or q3) else 0
        row = [label, str(chars), ft, fk, qt, qk, winner]
        print("  ".join(str(v).ljust(w) for v, w in zip(row, col_w)))

    print(sep)

    # Summary row
    print(bold(f"  Wins → F5-TTS: {wins['f5tts']}  Qwen3: {wins['qwen3tts']}  Tie: {wins['tie']}"))
    print()

    # Per-provider error summary
    for r in results:
        if not r.ok:
            print(red(f"  [{r.provider}] {r.label}: {r.error}"))

    # Saved files
    saved = [r.audio_path for r in results if r.audio_path]
    if saved:
        print()
        print(bold("Saved audio files:"))
        for p in saved:
            print(f"  {p}")


# ── Main ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Benchmark F5-TTS vs Qwen3-TTS synthesis speed")
    p.add_argument("--f5-url",    default="http://apicn.aiworm.cn:8010",
                   help="F5-TTS server base URL")
    p.add_argument("--q3-url",    default="http://apicn.aiworm.cn:8011",
                   help="Qwen3-TTS server base URL")
    p.add_argument("--f5-key",    default=os.environ.get("F5_TTS_API_KEY", ""),
                   help="F5-TTS Bearer token")
    p.add_argument("--q3-key",    default=os.environ.get("QWEN3_TTS_API_KEY", ""),
                   help="Qwen3-TTS Bearer token")
    p.add_argument("--f5-voice",  default="demo_speaker0",
                   help="F5-TTS voice (ref audio name)")
    p.add_argument("--q3-voice",  default="Vivian",
                   help="Qwen3-TTS voice")
    p.add_argument("--q3-model",  default="qwen3-tts",
                   help="Qwen3-TTS model name")
    p.add_argument("--timeout",   default=900, type=int,
                   help="Per-request timeout in seconds (default: 900)")
    p.add_argument("--output",    default="./tts_benchmark_out",
                   help="Directory to save audio output")
    p.add_argument("--no-save",   action="store_true",
                   help="Do not save audio files")
    p.add_argument("--cases",     default="",
                   help="Comma-separated list of test case labels to run "
                        "(default: all).  Available: " +
                        ", ".join(l for l, _ in TEST_CASES))
    p.add_argument("--skip-f5",   action="store_true", help="Skip F5-TTS")
    p.add_argument("--skip-q3",   action="store_true", help="Skip Qwen3-TTS")
    return p.parse_args()


def main():
    args = parse_args()

    # Filter test cases
    if args.cases:
        wanted = {s.strip() for s in args.cases.split(",")}
        cases = [(l, t) for l, t in TEST_CASES if l in wanted]
        if not cases:
            print(red(f"No matching test cases for: {args.cases}"))
            sys.exit(1)
    else:
        cases = TEST_CASES

    providers = []
    if not args.skip_f5:
        providers.append("f5tts")
    if not args.skip_q3:
        providers.append("qwen3tts")

    if not providers:
        print(red("Both providers skipped — nothing to do."))
        sys.exit(1)

    total_runs = len(cases) * len(providers)
    print(bold(f"\nRunning {total_runs} synthesis calls "
               f"({len(cases)} texts × {len(providers)} providers)"))
    if not args.skip_f5:
        print(f"  F5-TTS  : {args.f5_url}  voice={args.f5_voice}")
    if not args.skip_q3:
        print(f"  Qwen3   : {args.q3_url}  voice={args.q3_voice}  model={args.q3_model}")
    print(f"  timeout : {args.timeout}s per call\n")

    results: list[Result] = []
    run_n = 0

    for label, text in cases:
        for provider in providers:
            run_n += 1
            chars = len(text)
            preview = text[:60].replace("\n", " ")
            if len(text) > 60:
                preview += "…"
            print(f"[{run_n}/{total_runs}] {bold(provider)} / {label}  "
                  f"({chars} chars)  {grey(repr(preview))}")
            sys.stdout.flush()

            r = run_one(provider, label, text, args)
            results.append(r)

            if r.ok:
                cps = f"{r.chars_per_sec:.1f} chars/s"
                print(f"         → {green('OK')}  {r.elapsed_s:.1f}s  "
                      f"{r.bytes_out//1024} KB  [{cps}]"
                      + (f"  → {r.audio_path}" if r.audio_path else ""))
            else:
                print(f"         → {red('FAIL')}  {r.error}")

    print_table(results)


if __name__ == "__main__":
    main()
