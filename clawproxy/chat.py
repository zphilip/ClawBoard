#!/usr/bin/env python3
"""
ZeroClaw gateway chat client — mirrors the web dashboard chat exactly.

Flow (same as ws.ts + AgentChat.tsx):
  1. GET  /health          — check if pairing is required
  2. POST /pair            — exchange one-time code for bearer token (if needed)
  3. WS   /ws/chat?token=… — send messages, stream responses

Usage:
  python3 chat.py                          # auto-detect, auto-pair via CLI if needed
  python3 chat.py --host localhost --port 42617
  python3 chat.py --token zc_mytoken       # skip pairing, use existing token
  python3 chat.py --pair-code 123456       # supply pair code non-interactively
  python3 chat.py --message "Hello!"       # single-shot, non-interactive
"""

import argparse
import json
import re
import subprocess
import sys
import uuid
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------
try:
    import websockets
    import asyncio
except ImportError:
    print("Missing dependency. Install with:")
    print("  pip install websockets")
    sys.exit(1)

import asyncio


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no requests needed)
# ---------------------------------------------------------------------------

def http_get(url: str, token: str | None = None) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def http_post(url: str, headers: dict = {}) -> dict:
    req = urllib.request.Request(url, data=b"", method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Step 1 — check /health
# ---------------------------------------------------------------------------

def check_health(base_http: str) -> dict:
    try:
        return http_get(f"{base_http}/health")
    except Exception as e:
        print(f"[error] Cannot reach gateway at {base_http}: {e}")
        print("  Is zeroclaw running?  →  zeroclaw gateway")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2 — POST /pair  (mirrors api.ts pair())
# ---------------------------------------------------------------------------

def do_pair(base_http: str, code: str) -> str | None:
    """Exchange a one-time pairing code for a bearer token. Returns None on failure."""
    try:
        data = http_post(f"{base_http}/pair", headers={"X-Pairing-Code": code})
        token = data.get("token")
        if not token:
            print(f"[error] Pair response missing token: {data}")
        return token
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[error] Pairing failed ({e.code}): {body}")
        return None


# ---------------------------------------------------------------------------
# Pair-code helpers — auto-fetch via CLI
# ---------------------------------------------------------------------------

def _parse_pair_code(output: str) -> str | None:
    """Extract a numeric pair code from `zeroclaw gateway get-paircode` output."""
    # Matches the code inside the box border:  │  156155  │
    m = re.search(r"│\s*(\d{4,8})\s*│", output)
    if m:
        return m.group(1).strip()
    # Fallback: bare 4-8 digit number on its own line
    m = re.search(r"^\s*(\d{4,8})\s*$", output, re.MULTILINE)
    return m.group(1).strip() if m else None


def get_pair_code_auto() -> str | None:
    """
    Try to obtain a pair code automatically via the zeroclaw CLI.

    1. Run `zeroclaw gateway get-paircode` — if it already has an active code, parse it.
    2. If it says no active code, run `zeroclaw gateway get-paircode --new` to generate one.
    Returns the code string, or None if unavailable.
    """
    try:
        r = subprocess.run(
            ["zeroclaw", "gateway", "get-paircode"],
            capture_output=True, text=True, timeout=10,
        )
        output = r.stdout + r.stderr
        code = _parse_pair_code(output)
        if code:
            print(f"[info] Got pair code from CLI: {code}")
            return code

        # No active code — generate a new one
        print("[info] No active pair code; generating a new one via --new …")
        r2 = subprocess.run(
            ["zeroclaw", "gateway", "get-paircode", "--new"],
            capture_output=True, text=True, timeout=10,
        )
        output2 = r2.stdout + r2.stderr
        code2 = _parse_pair_code(output2)
        if code2:
            print(f"[info] Generated new pair code: {code2}")
            return code2

        print("[warn] Could not parse pair code from CLI output:")
        print(output2[:400])
        return None
    except FileNotFoundError:
        print("[warn] zeroclaw CLI not found; cannot auto-fetch pair code")
        return None
    except subprocess.TimeoutExpired:
        print("[warn] zeroclaw CLI timed out")
        return None


# ---------------------------------------------------------------------------
# Sentinel exception — signals that a re-pair is needed
# ---------------------------------------------------------------------------

class AuthRejected(Exception):
    pass


# ---------------------------------------------------------------------------
# Step 3 — WebSocket chat  (mirrors WebSocketClient in ws.ts)
# ---------------------------------------------------------------------------

async def ws_chat(base_ws: str, token: str | None, one_shot: str | None):
    session_id = str(uuid.uuid4())

    params = f"session_id={session_id}"
    if token:
        params = f"token={token}&{params}"

    url = f"{base_ws}/ws/chat?{params}"

    print(f"[info] Connecting to {url}")

    # subprotocol 'zeroclaw.v1' — mirrors new WebSocket(url, ['zeroclaw.v1'])
    try:
        ws_conn = await websockets.connect(url, subprotocols=["zeroclaw.v1"])
    except websockets.exceptions.InvalidStatus as e:
        if e.response.status_code in (401, 403):
            raise AuthRejected(f"Gateway rejected connection ({e.response.status_code}): token invalid or missing")
        raise

    async with ws_conn as ws:
        print("[info] Connected ✓\n")

        async def send_message(content: str):
            payload = json.dumps({"type": "message", "content": content})
            await ws.send(payload)

        async def receive_loop() -> str:
            """Collect frames until 'done' or 'error', return full response."""
            full_response = ""
            pending_chunks = ""

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                # --- mirror AgentChat.tsx onMessage switch ---

                if msg_type == "chunk":
                    chunk = msg.get("content", "")
                    pending_chunks += chunk
                    print(chunk, end="", flush=True)

                elif msg_type == "done":
                    full_response = (
                        msg.get("full_response")
                        or msg.get("content")
                        or pending_chunks
                    )
                    # If we were streaming chunks, the final done might be empty
                    if not pending_chunks:
                        print(full_response, end="")
                    print()  # newline after response
                    pending_chunks = ""
                    return full_response

                elif msg_type == "message":
                    # Non-streaming single response
                    content = msg.get("content", "")
                    print(content)
                    return content

                elif msg_type == "tool_call":
                    name = msg.get("name", "unknown")
                    args = json.dumps(msg.get("args", {}))
                    print(f"  [tool call]   {name}({args})")

                elif msg_type == "tool_result":
                    output = msg.get("output", "")
                    print(f"  [tool result] {output}")

                elif msg_type == "error":
                    err = msg.get("message", "Unknown error")
                    print(f"\n[agent error] {err}")
                    return ""

            return full_response

        # ── single-shot mode ─────────────────────────────────────────────────
        if one_shot:
            print(f"You: {one_shot}")
            print("Agent: ", end="", flush=True)
            await send_message(one_shot)
            await receive_loop()
            return

        # ── interactive REPL ─────────────────────────────────────────────────
        print("Type your message and press Enter. Ctrl+C or 'exit' to quit.\n")
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[bye]")
                break

            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", "bye"}:
                print("[bye]")
                break

            print("Agent: ", end="", flush=True)
            await send_message(user_input)
            await receive_loop()
            print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _acquire_token(base_http: str, args) -> str | None:
    """
    Obtain a bearer token by pairing.  Auto-fetches a pair code from the CLI
    if one is not supplied explicitly.  Returns the token, or None on failure.
    """
    code = getattr(args, "pair_code", None)

    if not code:
        # Try to get the code automatically via the zeroclaw CLI
        code = get_pair_code_auto()

    if not code:
        # Last resort: ask the user interactively
        print("\n[info] Could not auto-fetch a pair code.")
        print("  Run manually:  zeroclaw gateway get-paircode --new")
        try:
            code = input("Pairing code: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

    if not code:
        return None

    token = do_pair(base_http, code)
    if token:
        print(f"[info] Paired! Token: {token[:12]}…\n")
    return token


def main():
    parser = argparse.ArgumentParser(
        description="ZeroClaw gateway chat client (mirrors the web dashboard chat)"
    )
    parser.add_argument("--host",        default="127.0.0.1", help="Gateway host (default: 127.0.0.1)")
    parser.add_argument("--port",        default=42617, type=int, help="Gateway port (default: 42617)")
    parser.add_argument("--token",       default=None, help="Bearer token (skip pairing)")
    parser.add_argument("--pair-code",   default=None, help="One-time pairing code")
    parser.add_argument("--message", "-m", default=None, help="Single message (non-interactive)")
    args = parser.parse_args()

    base_http = f"http://{args.host}:{args.port}"
    base_ws   = f"ws://{args.host}:{args.port}"

    token = args.token

    # ── 1. health check ──────────────────────────────────────────────────────
    health = check_health(base_http)
    require_pairing = health.get("require_pairing", True)
    already_paired  = health.get("paired", False)

    print(f"[info] Gateway: {base_http}")
    print(f"[info] Requires pairing: {require_pairing}  |  Already paired: {already_paired}")

    # ── 2. pair if pairing is required and we have no token ──────────────────
    if require_pairing and not token:
        token = _acquire_token(base_http, args)
        if not token:
            print("[error] Could not obtain a token. Aborting.")
            sys.exit(1)

    # ── 3. WebSocket chat — retry once if the gateway rejects the token ──────
    for attempt in range(2):
        try:
            asyncio.run(ws_chat(base_ws, token, args.message))
            break
        except AuthRejected as e:
            print(f"\n[warn] {e}")
            if attempt == 0 and require_pairing:
                print("[info] Attempting re-pair …")
                token = _acquire_token(base_http, args)
                if not token:
                    print("[error] Re-pair failed. Aborting.")
                    sys.exit(1)
            else:
                print("[error] Authentication failed and re-pair is not available. Aborting.")
                sys.exit(1)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
