#!/usr/bin/env python3
"""
TTS Speed Benchmark: F5-TTS vs Qwen3-TTS vs Qwen3-TTS-2
========================================================
Synthesises the same texts with all configured providers and prints a
side-by-side timing table.

F5-TTS      : POST /voice-clone/synthesize_speech  (async polling)
Qwen3-TTS   : POST /v1/audio/speech                (sync, OpenAI-compatible)
Qwen3-TTS-2 : POST /v1/audio/speech                (sync, OpenAI-compatible)

Usage:
    python test_tts_benchmark.py [options]

Options:
    --f5-url    URL   F5-TTS server   (default: http://apicn.aiworm.cn:8010)
    --q3-url      URL   Qwen3-TTS server (default: http://apicn.aiworm.cn:8011)
    --q3-2-url    URL   Qwen3-TTS-2 server (default: http://apicn.aiworm.cn:8012)
    --f5-key    KEY   F5-TTS Bearer token  (or env F5_TTS_API_KEY)
    --q3-key      KEY   Qwen3 Bearer token   (or env QWEN3_TTS_API_KEY)
    --q3-2-key    KEY   Qwen3-TTS-2 Bearer token (or env QWEN3_TTS_2_API_KEY)
    --f5-voice  NAME  F5-TTS voice name    (default: demo_speaker0)
    --q3-voice    NAME  Qwen3-TTS voice      (default: Vivian)
    --q3-2-voice  NAME  Qwen3-TTS-2 voice    (default: Vivian)
    --q3-model    NAME  Qwen3-TTS model      (default: qwen3-tts)
    --q3-2-model  NAME  Qwen3-TTS-2 model    (default: qwen3-tts)
    --timeout   SECS  Per-request timeout  (default: 900)
    --output    DIR   Directory to save audio files (default: ./tts_benchmark_out)
    --no-save         Do not save audio to disk
"""

import argparse
import os
import re
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

# ── Config loader ───────────────────────────────────────────────────────────────

def _read_toml_value(path: Path, section: str, key: str) -> str:
    """Minimal TOML reader: finds key inside [section] without a full TOML parser."""
    try:
        text = path.read_text()
    except OSError:
        return ""
    in_section = False
    section_re = re.compile(r'^\s*\[([^\]]+)\]')
    kv_re      = re.compile(r'^\s*' + re.escape(key) + r'\s*=\s*["\']?([^"\'\n#]+)["\']?')
    for line in text.splitlines():
        m = section_re.match(line)
        if m:
            in_section = m.group(1).strip() == section
            continue
        if in_section:
            m = kv_re.match(line)
            if m:
                return m.group(1).strip()
    return ""


def load_clawproxy_config() -> dict:
    """Return known TTS key/url values from ~/.clawproxy/config.toml."""
    cfg_path = Path(os.environ.get("CLAWPROXY_CONFIG",
                                   Path.home() / ".clawproxy" / "config.toml"))

    # Allow either [tts.qwen3tts2] or [tts.qwen3-tts-2] naming styles.
    q3_2_key = _read_toml_value(cfg_path, "tts.qwen3tts2", "api_key")
    if not q3_2_key:
        q3_2_key = _read_toml_value(cfg_path, "tts.qwen3-tts-2", "api_key")
    q3_2_url = _read_toml_value(cfg_path, "tts.qwen3tts2", "base_url")
    if not q3_2_url:
        q3_2_url = _read_toml_value(cfg_path, "tts.qwen3-tts-2", "base_url")

    return {
        "f5tts_key":  _read_toml_value(cfg_path, "tts.f5tts",   "api_key"),
        "f5tts_url":  _read_toml_value(cfg_path, "tts.f5tts",   "base_url"),
        "qwen3_key":  _read_toml_value(cfg_path, "tts.qwen3tts", "api_key"),
        "qwen3_url":  _read_toml_value(cfg_path, "tts.qwen3tts", "base_url"),
        "qwen3_2_key": q3_2_key,
        "qwen3_2_url": q3_2_url,
    }

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
        elif provider == "qwen3tts":
            audio, elapsed = qwen3_synth(
                text, args.q3_url, args.q3_key, args.q3_voice, args.q3_model, args.timeout)
        elif provider == "qwen3-tts-2":
            audio, elapsed = qwen3_synth(
                text, args.q3_2_url, args.q3_2_key, args.q3_2_voice, args.q3_2_model, args.timeout)
        else:
            raise ValueError(f"unknown provider: {provider}")

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

    col_w = [22, 6, 10, 10, 10, 24]
    header = ["Test case", "Chars", "F5(s)", "Q3(s)", "Q3-2(s)", "Winner (time)"]
    sep = "  ".join("─" * w for w in col_w)

    def fmt_time(r: Optional[Result]) -> str:
        if r is None:
            return grey("(skip)")
        if not r.ok:
            return red("ERR")
        return f"{r.elapsed_s:>6.1f}s"

    print()
    print(bold("TTS Speed Benchmark"))
    print(sep)
    print("  ".join(bold(h.ljust(w)) for h, w in zip(header, col_w)))
    print(sep)

    wins: dict[str, int] = {"f5tts": 0, "qwen3tts": 0, "qwen3-tts-2": 0, "tie": 0}

    for label, pmap in by_label.items():
        f5 = pmap.get("f5tts")
        q3 = pmap.get("qwen3tts")
        q3_2 = pmap.get("qwen3-tts-2")
        ft = fmt_time(f5)
        qt = fmt_time(q3)
        q2t = fmt_time(q3_2)

        ok_times: list[tuple[str, float]] = []
        for name, item in (("f5tts", f5), ("qwen3tts", q3), ("qwen3-tts-2", q3_2)):
            if item and item.ok:
                ok_times.append((name, item.elapsed_s))

        if len(ok_times) == 0:
            winner = red("all failed")
        elif len(ok_times) == 1:
            only_name = ok_times[0][0]
            if only_name == "f5tts":
                winner = green("F5-TTS (only pass)")
            elif only_name == "qwen3tts":
                winner = cyan("Qwen3 (only pass)")
            else:
                winner = yellow("Qwen3-2 (only pass)")
            wins[only_name] += 1
        else:
            ok_times.sort(key=lambda x: x[1])
            fastest_name, fastest_time = ok_times[0]
            second_time = ok_times[1][1]
            if abs(fastest_time - second_time) < 0.5:
                winner = yellow("tie")
                wins["tie"] += 1
            elif fastest_name == "f5tts":
                winner = green(f"F5-TTS ({fastest_time:.1f}s)")
                wins["f5tts"] += 1
            elif fastest_name == "qwen3tts":
                winner = cyan(f"Qwen3 ({fastest_time:.1f}s)")
                wins["qwen3tts"] += 1
            else:
                winner = yellow(f"Qwen3-2 ({fastest_time:.1f}s)")
                wins["qwen3-tts-2"] += 1

        chars = (f5 or q3).chars if (f5 or q3) else 0
        row = [label, str(chars), ft, qt, q2t, winner]
        print("  ".join(str(v).ljust(w) for v, w in zip(row, col_w)))

    print(sep)

    # Summary row
    print(bold(
        f"  Wins → F5-TTS: {wins['f5tts']}  "
        f"Qwen3: {wins['qwen3tts']}  "
        f"Qwen3-2: {wins['qwen3-tts-2']}  "
        f"Tie: {wins['tie']}"
    ))
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
    # Pre-load ~/.clawproxy/config.toml so its values become the defaults.
    fc = load_clawproxy_config()

    p = argparse.ArgumentParser(
        description="Benchmark F5-TTS vs Qwen3-TTS synthesis speed")
    p.add_argument("--f5-url",    default=fc.get("f5tts_url") or "http://apicn.aiworm.cn:8010",
                   help="F5-TTS server base URL")
    p.add_argument("--q3-url",    default=fc.get("qwen3_url") or "http://apicn.aiworm.cn:8011",
                   help="Qwen3-TTS server base URL")
    p.add_argument("--q3-2-url",  default=fc.get("qwen3_2_url") or "http://apicn.aiworm.cn:8012",
                   help="Qwen3-TTS-2 server base URL")
    p.add_argument("--f5-key",
                   default=os.environ.get("F5_TTS_API_KEY") or fc.get("f5tts_key", ""),
                   help="F5-TTS Bearer token (auto-read from ~/.clawproxy/config.toml or F5_TTS_API_KEY)")
    p.add_argument("--q3-key",
                   default=os.environ.get("QWEN3_TTS_API_KEY") or fc.get("qwen3_key", ""),
                   help="Qwen3-TTS Bearer token (auto-read from ~/.clawproxy/config.toml or QWEN3_TTS_API_KEY)")
    p.add_argument("--q3-2-key",
                   default=os.environ.get("QWEN3_TTS_2_API_KEY") or fc.get("qwen3_2_key", ""),
                   help="Qwen3-TTS-2 Bearer token (auto-read from ~/.clawproxy/config.toml or QWEN3_TTS_2_API_KEY)")
    p.add_argument("--f5-voice",  default="demo_speaker0",
                   help="F5-TTS voice (ref audio name)")
    p.add_argument("--q3-voice",  default="Vivian",
                   help="Qwen3-TTS voice")
    p.add_argument("--q3-2-voice", default="Vivian",
                   help="Qwen3-TTS-2 voice")
    p.add_argument("--q3-model",  default="qwen3-tts",
                   help="Qwen3-TTS model name")
    p.add_argument("--q3-2-model", default="qwen3-tts",
                   help="Qwen3-TTS-2 model name")
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
    p.add_argument("--skip-q3-2", action="store_true", help="Skip Qwen3-TTS-2")
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
    if not args.skip_q3_2:
        providers.append("qwen3-tts-2")

    if not providers:
        print(red("Both providers skipped — nothing to do."))
        sys.exit(1)

    total_runs = len(cases) * len(providers)
    print(bold(f"\nRunning {total_runs} synthesis calls "
               f"({len(cases)} texts × {len(providers)} providers)"))
    if not args.skip_f5:
        auth_note = grey("(no auth)") if not args.f5_key else green("(auth OK)")
        print(f"  F5-TTS  : {args.f5_url}  voice={args.f5_voice}  {auth_note}")
    if not args.skip_q3:
        auth_note = grey("(no auth)") if not args.q3_key else green("(auth OK)")
        print(f"  Qwen3   : {args.q3_url}  voice={args.q3_voice}  model={args.q3_model}  {auth_note}")
    if not args.skip_q3_2:
        auth_note = grey("(no auth)") if not args.q3_2_key else green("(auth OK)")
        print(
            f"  Qwen3-2 : {args.q3_2_url}  "
            f"voice={args.q3_2_voice}  model={args.q3_2_model}  {auth_note}"
        )
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
