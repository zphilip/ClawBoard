#!/usr/bin/env python3
"""
Probe an OpenAI-compatible gateway the same way the extension talks to it.

WHY: the transports in `src/transports/` only exercise what VS Code hands them.
When a new gateway (OpenCode Zen/Go, a private proxy, ...) needs verifying, this
standalone script reproduces the real wire request - special headers, extra body
fields, SSE streaming - so a failure is provable outside the extension host.

WHAT IT CHECKS (per provider):
  1. GET  <base>/models              -> status + model ids
  2. POST <base>/chat/completions    -> non-streaming completion
  3. POST <base>/chat/completions    -> SSE streaming (TTFT, content, usage)
  4. (opencode only, opt-in)         -> same call WITHOUT the x-opencode-* headers,
                                        proving whether the gateway requires them

REASONING MODELS: a small `--max-tokens` is spent entirely on `reasoning_content`,
leaving `content: null` and `finish_reason: "length"`. That is not a gateway bug,
so an empty truncated reply is retried once at RETRY_MAX_TOKENS (opt out with
`--no-retry`).

USAGE
  # OpenCode Zen (special headers are added automatically)
  OPENCODE_API_KEY=sk-... python scripts/probe-openai-compat.py

  # OpenCode Go
  OPENCODE_GO_API_KEY=sk-... python scripts/probe-openai-compat.py --provider opencode-go

  # Any provider described in the opencode config shape
  OPENAI_API_KEY=... python scripts/probe-openai-compat.py --config my-provider.json

  # Inline overrides
  OPENCODE_API_KEY=sk-... python scripts/probe-openai-compat.py \
      --model glm-5 --header X-Trace=abc --body '{"custom_routing":"fast"}'

CONTRACT
  - Python 3.9+, standard library only (no requests/httpx install step).
  - The API key is read from the environment only; it is never printed and is
    redacted in `--dry-run` output.
  - Exit code 0 = every executed check passed, 1 = at least one failed.
  - No file writes, no network calls other than the probed base URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request

# Kept in sync with src/config.ts (OPEN_CODE_CLIENT / FALLBACK_USER_AGENT).
OPEN_CODE_CLIENT = "vscode-copilot-chat"
USER_AGENT = "everything-copilot-chat/probe VSCode"

DEFAULT_PROMPT = "Reply with exactly one word: pong"
DEFAULT_MAX_TOKENS = 32
# Enough headroom for a reasoning model to finish thinking AND emit content.
RETRY_MAX_TOKENS = 512


# ── Provider profiles ────────────────────────────────────────────────────────


@dataclass
class ProviderProfile:
    """One gateway: where it lives, how it authenticates, and what it needs on the wire."""

    name: str
    base_url: str
    api_key_env: str
    default_model: str
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    # How the key is attached: "bearer" (OpenAI style), "x-api-key" (Anthropic),
    # "x-goog-api-key" (Google), or "" for gateways that only use custom headers.
    auth_scheme: str = "bearer"
    models_path: str = "/models"
    chat_path: str = "/chat/completions"
    # OpenCode gateways read x-opencode-session for sticky routing / cache affinity.
    uses_opencode_headers: bool = False


DEFAULT_PROFILES: dict[str, ProviderProfile] = {
    "opencode": ProviderProfile(
        name="opencode",
        base_url="https://opencode.ai/zen/v1",
        api_key_env="OPENCODE_API_KEY",
        default_model="deepseek-v4-flash-free",
        headers={"x-opencode-client": OPEN_CODE_CLIENT, "User-Agent": USER_AGENT},
        uses_opencode_headers=True,
    ),
    "opencode-go": ProviderProfile(
        name="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        default_model="deepseek-v4-flash",
        headers={"x-opencode-client": OPEN_CODE_CLIENT, "User-Agent": USER_AGENT},
        uses_opencode_headers=True,
    ),
    "generic": ProviderProfile(
        name="generic",
        base_url="",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        headers={"User-Agent": USER_AGENT},
    ),
}


def load_config_profiles(path: str) -> dict[str, ProviderProfile]:
    """
    Load profiles from the opencode config shape:

        { "provider": { "<name>": { "options": {
              "baseURL": "...", "headers": {...}, "body": {...},
              "apiKeyEnv": "MY_KEY", "authScheme": "bearer",
              "modelsPath": "/models", "chatPath": "/chat/completions",
              "defaultModel": "..." } } } }

    `apiKeyEnv` / `authScheme` / `*Path` / `defaultModel` are extensions of that
    shape; everything else mirrors it exactly.
    """
    # utf-8-sig so a config saved by Notepad / PowerShell 5.1 (BOM) still parses.
    with open(path, "r", encoding="utf-8-sig") as handle:
        raw = json.load(handle)

    profiles: dict[str, ProviderProfile] = {}
    for name, entry in (raw.get("provider") or {}).items():
        options = (entry or {}).get("options") or {}
        base_url = options.get("baseURL") or options.get("baseUrl") or ""
        if not base_url:
            raise SystemExit(f"provider {name!r} in {path} has no options.baseURL")
        profiles[name] = ProviderProfile(
            name=name,
            base_url=base_url,
            api_key_env=options.get("apiKeyEnv", "OPENAI_API_KEY"),
            default_model=options.get("defaultModel", "gpt-4o-mini"),
            headers=dict(options.get("headers") or {}),
            body=dict(options.get("body") or {}),
            auth_scheme=options.get("authScheme", "bearer"),
            models_path=options.get("modelsPath", "/models"),
            chat_path=options.get("chatPath", "/chat/completions"),
            uses_opencode_headers=bool(options.get("opencodeHeaders", False)),
        )
    if not profiles:
        raise SystemExit(f"{path} contains no provider entries")
    return profiles


# ── Helpers ──────────────────────────────────────────────────────────────────


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge `extra` over `base` recursively; caller-supplied values always win."""
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def auth_headers(profile: ProviderProfile, api_key: str | None) -> dict[str, str]:
    if not api_key or not profile.auth_scheme:
        return {}
    if profile.auth_scheme == "x-api-key":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if profile.auth_scheme == "x-goog-api-key":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def build_headers(profile: ProviderProfile, api_key: str | None, session_id: str, request_id: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if profile.uses_opencode_headers:
        headers["x-opencode-session"] = session_id
        headers["x-opencode-request"] = request_id
    headers.update(profile.headers)
    headers.update(auth_headers(profile, api_key))
    return headers


def redact(headers: dict[str, str]) -> dict[str, str]:
    hidden = ("authorization", "x-api-key", "x-goog-api-key")
    return {key: ("***" if key.lower() in hidden else value) for key, value in headers.items()}


# ── HTTP ─────────────────────────────────────────────────────────────────────


@dataclass
class HttpResult:
    status: int
    body: str
    ttft_ms: int | None = None
    elapsed_ms: int = 0


def http_get(url: str, headers: dict[str, str], timeout: float) -> HttpResult:
    started = time.monotonic()
    get_headers = {"Accept-Encoding": "identity", **headers}
    try:
        with request.urlopen(request.Request(url, headers=get_headers, method="GET"), timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return HttpResult(status=response.status, body=body, elapsed_ms=int((time.monotonic() - started) * 1000))
    except error.HTTPError as exc:
        return HttpResult(status=exc.code, body=exc.read().decode("utf-8", "replace"), elapsed_ms=int((time.monotonic() - started) * 1000))
    except error.URLError as exc:
        return HttpResult(status=0, body=f"transport error: {exc.reason}", elapsed_ms=int((time.monotonic() - started) * 1000))


def http_post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float, stream: bool) -> HttpResult:
    started = time.monotonic()
    data = json.dumps(payload).encode("utf-8")
    send_headers = {"Accept-Encoding": "identity", **headers}
    if stream:
        send_headers["Accept"] = "text/event-stream"
    req = request.Request(url, data=data, headers=send_headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            if not stream:
                body = response.read().decode("utf-8", "replace")
                return HttpResult(response.status, body, elapsed_ms=int((time.monotonic() - started) * 1000))
            return _consume_sse(response, started)
    except error.HTTPError as exc:
        return HttpResult(exc.code, exc.read().decode("utf-8", "replace"), elapsed_ms=int((time.monotonic() - started) * 1000))
    except error.URLError as exc:
        return HttpResult(0, f"transport error: {exc.reason}", elapsed_ms=int((time.monotonic() - started) * 1000))


def _consume_sse(response: Any, started: float) -> HttpResult:
    """Read an SSE stream, recording time-to-first-token and the joined deltas."""
    ttft_ms: int | None = None
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    chunks = 0

    for raw_line in response:
        line = raw_line.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        chunks += 1
        if ttft_ms is None:
            ttft_ms = int((time.monotonic() - started) * 1000)
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        for choice in event.get("choices") or []:
            if isinstance(choice.get("finish_reason"), str):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])

    summary = {
        "chunks": chunks,
        "content": "".join(content),
        "reasoning": "".join(reasoning),
        "finish_reason": finish_reason,
        "usage": usage,
    }
    return HttpResult(status=response.status, body=json.dumps(summary, ensure_ascii=False), ttft_ms=ttft_ms, elapsed_ms=int((time.monotonic() - started) * 1000))


# ── Checks ───────────────────────────────────────────────────────────────────


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str


def check_models(profile: ProviderProfile, api_key: str | None, timeout: float, session_id: str) -> Check:
    url = join_url(profile.base_url, profile.models_path)
    headers = build_headers(profile, api_key, session_id, f"req-{uuid.uuid4().hex}")
    result = http_get(url, headers, timeout)
    if result.status != 200:
        return Check("GET /models", "FAIL", f"HTTP {result.status or 'ERR'}: {result.body[:300]}")
    try:
        ids = [item.get("id") for item in (json.loads(result.body).get("data") or []) if isinstance(item, dict)]
    except json.JSONDecodeError:
        return Check("GET /models", "FAIL", f"non-JSON body: {result.body[:300]}")
    ids = [str(model_id) for model_id in ids if model_id]
    if not ids:
        return Check("GET /models", "FAIL", "200 but the model list is empty")
    preview = ", ".join(ids[:5]) + (" ..." if len(ids) > 5 else "")
    return Check("GET /models", "PASS", f"{len(ids)} models in {result.elapsed_ms} ms: {preview}")


def check_chat(profile: ProviderProfile, model: str, api_key: str | None, body_merge: dict[str, Any], args: argparse.Namespace, session_id: str, stream: bool) -> Check:
    check, truncated = attempt_chat(profile, model, api_key, body_merge, args, session_id, stream, args.max_tokens)
    if not truncated or args.no_retry:
        return check

    # Reasoning models burn the whole budget on reasoning_content, so retry once
    # with headroom before blaming the gateway.
    retry_budget = max(args.max_tokens * 16, RETRY_MAX_TOKENS)
    retry, _ = attempt_chat(profile, model, api_key, body_merge, args, session_id, stream, retry_budget)
    retry.detail += f" [auto-retried with max_tokens={retry_budget}; {args.max_tokens} went to reasoning]"
    return retry


def attempt_chat(
    profile: ProviderProfile,
    model: str,
    api_key: str | None,
    body_merge: dict[str, Any],
    args: argparse.Namespace,
    session_id: str,
    stream: bool,
    max_tokens: int,
) -> tuple[Check, bool]:
    """One request. The bool is True when the reply was truncated with no content yet."""
    payload = deep_merge(
        {
            "model": model,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": max_tokens,
            "stream": stream,
        },
        profile.body,
    )
    payload = deep_merge(payload, body_merge)
    if stream:
        payload["stream_options"] = {"include_usage": True}

    label = "POST /chat/completions (stream)" if stream else "POST /chat/completions"
    headers = build_headers(profile, api_key, session_id, f"req-{uuid.uuid4().hex}")
    url = join_url(profile.base_url, profile.chat_path)

    if args.dry_run:
        print(f"  [dry-run] {label} -> {url}")
        print(f"  [dry-run] headers: {json.dumps(redact(headers), indent=2)}")
        print(f"  [dry-run] body: {json.dumps(payload, indent=2)}")
        return Check(label, "SKIP", "dry run"), False

    result = http_post_json(url, headers, payload, args.timeout, stream)
    if result.status != 200:
        return Check(label, "FAIL", f"HTTP {result.status or 'ERR'}: {result.body[:400]}"), False

    if not stream:
        try:
            reply = json.loads(result.body)
        except json.JSONDecodeError:
            return Check(label, "FAIL", f"non-JSON body: {result.body[:300]}"), False
        choice = (reply.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()
        if not text:
            return empty_content_check(label, "finish_reason", choice.get("finish_reason"), message.get("reasoning_content"), max_tokens), choice.get("finish_reason") == "length"
        usage = reply.get("usage") or {}
        return Check(label, "PASS", f"{result.elapsed_ms} ms, content={text[:80]!r}, usage={usage or 'n/a'}"), False

    summary = json.loads(result.body)
    text = (summary.get("content") or "").strip()
    if not text:
        return empty_content_check(label, "summary", summary.get("finish_reason"), summary.get("reasoning"), max_tokens), summary.get("finish_reason") == "length"
    detail = f"ttft {result.ttft_ms} ms, total {result.elapsed_ms} ms, {summary.get('chunks')} chunks, content={text[:80]!r}"
    if summary.get("reasoning"):
        detail += f", reasoning={len(summary['reasoning'])} chars"
    if summary.get("usage"):
        detail += f", usage={summary['usage']}"
    return Check(label, "PASS", detail), False


def empty_content_check(label: str, where: str, finish_reason: Any, reasoning: Any, max_tokens: int) -> Check:
    """Explain a 200 with no visible content: budget exhaustion vs. a real problem."""
    preview = str(reasoning or "").strip()[:80]
    if finish_reason == "length":
        return Check(label, "FAIL", f"budget exhausted by reasoning (max_tokens={max_tokens}, finish_reason=length); reasoning={preview!r}")
    detail = f"200 but empty content ({where}={finish_reason})"
    if preview:
        detail += f", reasoning={preview!r}"
    return Check(label, "FAIL", detail)


def check_opencode_headers_required(
    profile: ProviderProfile,
    model: str,
    api_key: str | None,
    body_merge: dict[str, Any],
    args: argparse.Namespace,
    with_headers: Check,
) -> Check:
    """Same streaming call minus the x-opencode-* headers: do they matter?"""
    stripped = ProviderProfile(**{**profile.__dict__, "uses_opencode_headers": False})
    without_headers = check_chat(stripped, model, api_key, body_merge, args, f"probe-{uuid.uuid4().hex}", stream=True)

    if with_headers.status != "PASS":
        return Check("header sensitivity", "SKIP", "baseline (with headers) failed, nothing to compare")
    if without_headers.status == "PASS":
        return Check("header sensitivity", "PASS", "gateway accepts the call without x-opencode-* headers (optional)")
    return Check(
        "header sensitivity",
        "PASS",
        f"gateway REQUIRES x-opencode-* headers (without: {without_headers.detail[:160]})",
    )


# ── Entry point ──────────────────────────────────────────────────────────────


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe an OpenAI-compatible gateway.")
    parser.add_argument("--provider", default="opencode", help="profile name (default: opencode)")
    parser.add_argument("--config", help="JSON file in the opencode `provider.options` shape")
    parser.add_argument("--model", help="model id (default: the profile's)")
    parser.add_argument("--base-url", help="override the profile's base URL")
    parser.add_argument("--header", action="append", default=[], metavar="KEY=VALUE", help="extra header (repeatable)")
    parser.add_argument("--body", default="{}", help="extra request-body JSON merged over the profile body")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--no-retry", action="store_true", help="do not retry a reasoning-truncated reply with a larger budget")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout in seconds")
    parser.add_argument("--no-stream", action="store_true", help="skip the streaming check")
    parser.add_argument("--skip-models", action="store_true", help="skip the GET /models check")
    parser.add_argument("--probe-headers", action="store_true", help="compare with/without the x-opencode-* headers")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent; make no requests")
    return parser.parse_args(argv)


def resolve_profile(args: argparse.Namespace) -> ProviderProfile:
    profiles = load_config_profiles(args.config) if args.config else DEFAULT_PROFILES
    if args.provider not in profiles:
        available = ", ".join(sorted(profiles)) or "<none>"
        raise SystemExit(f"unknown provider {args.provider!r}; available: {available}")
    profile = profiles[args.provider]

    # A config-file provider may still be selected by --provider; CLI overrides win.
    if args.base_url:
        profile = ProviderProfile(**{**profile.__dict__, "base_url": args.base_url})
    if args.header:
        extra = {}
        for item in args.header:
            if "=" not in item:
                raise SystemExit(f"--header expects KEY=VALUE, got {item!r}")
            key, value = item.split("=", 1)
            extra[key.strip()] = value.strip()
        profile = ProviderProfile(**{**profile.__dict__, "headers": {**profile.headers, **extra}})
    return profile


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    profile = resolve_profile(args)

    try:
        body_merge = json.loads(args.body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--body is not valid JSON: {exc}") from exc
    if not isinstance(body_merge, dict):
        raise SystemExit("--body must be a JSON object")

    if not profile.base_url:
        raise SystemExit(f"provider {profile.name!r} has no base URL; pass --base-url or --config")

    api_key = os.environ.get(profile.api_key_env)
    model = args.model or profile.default_model

    print(f"provider : {profile.name}")
    print(f"base URL : {profile.base_url}")
    print(f"model    : {model}")
    print(f"api key  : {'set via ' + profile.api_key_env if api_key else 'MISSING (' + profile.api_key_env + ')'}")
    if profile.uses_opencode_headers:
        print("headers  : x-opencode-session / x-opencode-request / x-opencode-client")
    print()

    checks: list[Check] = []
    session_id = f"probe-{uuid.uuid4().hex[:12]}"

    if not api_key and not args.dry_run:
        print(f"FAIL  auth: {profile.api_key_env} is not set")
        return 1

    if args.dry_run:
        print(f"  [dry-run] GET {join_url(profile.base_url, profile.models_path)}")
    elif not args.skip_models:
        checks.append(check_models(profile, api_key, args.timeout, session_id))
        report(checks[-1])

    checks.append(check_chat(profile, model, api_key, body_merge, args, session_id, stream=False))
    report(checks[-1])

    if not args.no_stream:
        checks.append(check_chat(profile, model, api_key, body_merge, args, session_id, stream=True))
        report(checks[-1])

    if args.probe_headers and profile.uses_opencode_headers and not args.no_stream and not args.dry_run:
        checks.append(check_opencode_headers_required(profile, model, api_key, body_merge, args, checks[-1]))
        report(checks[-1])

    failures = [check for check in checks if check.status == "FAIL"]
    print()
    print(f"{len(checks) - len(failures)}/{len(checks)} checks passed")
    return 0 if not failures else 1


def report(check: Check) -> None:
    print(f"{check.status:<4}  {check.name}")
    print(f"      {check.detail}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
