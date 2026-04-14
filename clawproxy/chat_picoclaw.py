#!/usr/bin/env python3
"""
PicoClaw chat client for the Pico channel (mirrors the web frontend behavior).

ARCHITECTURE — two separate ports:
  ┌─────────────────────────────────────────────────────────────────┐
  │  Web backend (picoclaw-web)  :18800   <── this script (default) │
  │    GET /api/pico/token                                          │
  │    GET /pico/ws  (proxies to gateway at :18790)                 │
  │                                                                 │
  │  PicoClaw gateway            :18790   <── direct mode (--direct)│
  │    GET /pico/ws  (served by pico channel directly)              │
  └─────────────────────────────────────────────────────────────────┘

MODE 1 — via web backend (default, mirrors the browser dashboard):
  1) GET  http://host:18800/api/pico/token  →  { token, ws_url, enabled }
  2) WS   ws://host:18800/pico/ws?session_id=<id>
     Auth: Authorization: Bearer <token>  (+ Sec-WebSocket-Protocol: token.<token>)

MODE 2 — direct to gateway (--direct, or --gateway-port explicitly set):
  1) WS   ws://host:18790/pico/ws?session_id=<id>
     Auth: Authorization: Bearer <token>
     Token from --token flag, or read from picoclaw config.json.

AUTH NOTES:
  The gateway accepts Bearer token in Authorization header (simplest, always forwarded)
  OR Sec-WebSocket-Protocol "token.<value>" subprotocol (browser-native, but may be
  stripped by some proxy configs).  This script sends BOTH for maximum compatibility.

Usage:
  # Mode 1 — via web backend on :18800 (auto-fetches token)
  python3 chat_picoclaw.py
  python3 chat_picoclaw.py --host 192.168.1.28 --port 18800
  python3 chat_picoclaw.py --setup          # auto-enable pico channel first
  python3 chat_picoclaw.py -m "hello"       # single-shot

  # Mode 2 — direct to gateway on :18790 (provide token yourself)
  python3 chat_picoclaw.py --direct --token <token>
  python3 chat_picoclaw.py --direct                         # reads token from ~/.picoclaw/config.json
  python3 chat_picoclaw.py --direct --host 192.168.1.28 --token <token>
  python3 chat_picoclaw.py --gateway-port 18790 --token <token>  # explicit port → also direct mode
  python3 chat_picoclaw.py --direct --config ~/.picoclaw/config.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
import urllib.error
import urllib.request
from typing import Any

try:
    import websockets
except ImportError:
    print("Missing dependency: websockets")
    print("Install one of:")
    print("  sudo apt-get install -y python3-websockets")
    print("  # or")
    print("  python3 -m pip install websockets")
    sys.exit(1)


DEFAULT_WEB_PORT     = 18800  # picoclaw-web backend
DEFAULT_GATEWAY_PORT = 18790  # picoclaw gateway (direct)


def http_json(url: str, method: str = "GET") -> dict[str, Any]:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def maybe_setup(base_http: str) -> None:
    try:
        data = http_json(f"{base_http}/api/pico/setup", method="POST")
        changed = bool(data.get("changed", False))
        print(f"[info] pico setup complete (changed={changed})")
    except Exception as e:
        print(f"[warn] setup failed: {e}")


def get_pico_token(base_http: str) -> dict[str, Any]:
    """Mode 1: fetch token + ws_url from the web backend (:18800)."""
    try:
        return http_json(f"{base_http}/api/pico/token", method="GET")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"/api/pico/token failed ({e.code}): {body}") from e
    except Exception as e:
        raise RuntimeError(f"cannot reach {base_http}: {e}") from e


def read_token_from_config(config_path: str) -> str:
    """Mode 2: read the pico token directly from picoclaw config.json."""
    import os
    if not config_path:
        default = os.path.expanduser("~/.picoclaw/config.json")
        config_path = default
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        token = (
            cfg.get("channels", {}).get("pico", {}).get("token")
            or cfg.get("pico", {}).get("token")
        )
        if not token:
            raise RuntimeError(f"no pico.token found in {config_path}")
        return str(token)
    except FileNotFoundError:
        raise RuntimeError(f"config not found: {config_path}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON in {config_path}: {e}")


def normalize_ws_url_for_cli(ws_url: str, host_override: str | None) -> str:
    # Browser code rewrites localhost when dashboard is accessed remotely.
    # For CLI we optionally support --ws-host to force replacement.
    if not host_override:
        return ws_url

    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(ws_url)
        hostname = parsed.hostname or ""
        if hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
            return ws_url

        port = parsed.port
        if port is None:
            return ws_url

        netloc = f"{host_override}:{port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        return ws_url


async def run_chat(
    ws_url: str,
    token: str,
    session_id: str,
    one_shot: str | None,
    origin: str | None = None,
) -> None:
    sep = "&" if "?" in ws_url else "?"
    ws_url = f"{ws_url}{sep}session_id={session_id}"

    print(f"[info] connecting: {ws_url}")
    print(f"[info] session_id: {session_id}")

    message_counter = 0
    pending_updates: dict[str, str] = {}

    # Auth strategy:
    #  • Authorization: Bearer — explicit header, NOT a hop-by-hop header so it
    #    survives the web-backend reverse proxy unchanged.  Works for both modes.
    #  • Sec-WebSocket-Protocol: token.<value> — browser-native auth (browsers
    #    cannot set custom headers).  Also sent for full compatibility.
    #  • Origin — set to the web-backend URL so AllowOrigins checks pass even
    #    when the pico channel was configured via browser setup.
    connect_headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
    }
    if origin:
        connect_headers["Origin"] = origin

    async with websockets.connect(
        ws_url,
        subprotocols=[f"token.{token}"],
        additional_headers=connect_headers,
    ) as ws:
        print("[info] connected ✓")

        async def recv_until_done() -> str:
            final_text = ""
            while True:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[warn] non-json frame: {raw}")
                    continue

                msg_type = msg.get("type")
                payload = msg.get("payload") or {}

                if msg_type == "typing.start":
                    print("[typing] ...")
                    continue

                if msg_type == "typing.stop":
                    continue

                if msg_type == "message.create":
                    content = str(payload.get("content", ""))
                    message_id = str(payload.get("message_id", ""))
                    if message_id:
                        pending_updates[message_id] = content
                    final_text = content
                    print(f"Assistant: {content}")
                    return final_text

                if msg_type == "message.update":
                    message_id = str(payload.get("message_id", ""))
                    content = str(payload.get("content", ""))
                    if message_id:
                        pending_updates[message_id] = content
                    # Show update as a line to make edits observable in terminal.
                    print(f"Assistant(update): {content}")
                    final_text = content
                    continue

                if msg_type == "error":
                    code = payload.get("code", "error")
                    text = payload.get("message", "unknown error")
                    print(f"[server error] {code}: {text}")
                    return ""

                if msg_type == "pong":
                    continue

                print(f"[debug] {msg}")

        async def send_user_message(content: str) -> None:
            nonlocal message_counter
            message_counter += 1
            msg_id = f"msg-{message_counter}-{int(time.time() * 1000)}"
            payload = {
                "type": "message.send",
                "id": msg_id,
                "payload": {"content": content},
            }
            await ws.send(json.dumps(payload))

        if one_shot:
            print(f"You: {one_shot}")
            await send_user_message(one_shot)
            await recv_until_done()
            return

        print("Type a message. 'exit' to quit.\n")
        while True:
            try:
                user = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[bye]")
                break

            if not user:
                continue
            if user.lower() in {"exit", "quit", "bye"}:
                print("[bye]")
                break

            await send_user_message(user)
            await recv_until_done()
            print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PicoClaw Pico-channel chat client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
PORT GUIDE:
  :18800  web backend (picoclaw-web)  — default mode, auto-fetches token
  :18790  picoclaw gateway            — direct mode (--direct or --gateway-port)

AUTH:
  Mode 1 sends Authorization: Bearer <token> header which the reverse proxy
  at :18800 forwards intact to the gateway.  The Sec-WebSocket-Protocol
  subprotocol is also sent for full browser-protocol compatibility.

EXAMPLES:
  # LAN machine, via web backend
  python3 chat_picoclaw.py --host 192.168.1.28

  # Direct to gateway (localhost) — reads token from ~/.picoclaw/config.json
  python3 chat_picoclaw.py --direct

  # Direct to gateway (localhost) — explicit token
  python3 chat_picoclaw.py --direct --token ecc1c072c33986aeebc3d48d940352e2

  # Direct to LAN gateway
  python3 chat_picoclaw.py --direct --host 192.168.1.28 --token <token>

  # Direct with explicit port (also triggers direct mode)
  python3 chat_picoclaw.py --gateway-port 18790 --token <token>
        """
    )
    # Web backend (mode 1)
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument("--port", default=DEFAULT_WEB_PORT, type=int,
                        help=f"Web backend port (default: {DEFAULT_WEB_PORT})")
    # Direct gateway mode (mode 2)
    parser.add_argument("--direct", action="store_true",
                        help=f"Connect directly to gateway, bypass web backend (port: {DEFAULT_GATEWAY_PORT})")
    # --gateway-port default=None so any explicit value triggers direct mode
    parser.add_argument("--gateway-port", default=None, type=int,
                        help=f"Gateway port — if set, implies --direct (default: {DEFAULT_GATEWAY_PORT})")
    parser.add_argument("--token", default=None,
                        help="Pico token (auto-read in mode 1; required or from config in --direct)")
    parser.add_argument("--config", default=None,
                        help="Path to picoclaw config.json to read token from (direct mode only)")
    # Shared options
    parser.add_argument("--session-id", default=None, help="Reuse a specific session id")
    parser.add_argument("--ws-host", default=None,
                        help="Override hostname in ws_url returned by web backend (useful for LAN)")
    parser.add_argument("--setup", action="store_true",
                        help="Call POST /api/pico/setup on web backend first (mode 1 only)")
    parser.add_argument("--message", "-m", default=None, help="Single-shot message (non-interactive)")
    args = parser.parse_args()

    session_id = args.session_id or str(uuid.uuid4())

    # Resolve effective gateway port: explicit arg wins, else use default
    gw_port = args.gateway_port if args.gateway_port is not None else DEFAULT_GATEWAY_PORT

    # Direct mode is triggered by --direct flag OR by any explicit --gateway-port
    use_direct = args.direct or (args.gateway_port is not None)

    origin: str | None = None

    # ── MODE 2: direct to gateway ────────────────────────────────────────────
    if use_direct:
        token = args.token
        if not token:
            try:
                token = read_token_from_config(args.config or "")
            except Exception as e:
                print(f"[error] {e}")
                print("\nProvide token via --token or --config, e.g.:")
                print(f"  {sys.argv[0]} --direct --token <your-pico-token>")
                print(f"  {sys.argv[0]} --direct --config ~/.picoclaw/config.json")
                sys.exit(1)

        ws_url = f"ws://{args.host}:{gw_port}/pico/ws"
        print(f"[info] mode: direct to gateway")
        print(f"[info] gateway: ws://{args.host}:{gw_port}")

    # ── MODE 1: via web backend ───────────────────────────────────────────────
    else:
        base_http = f"http://{args.host}:{args.port}"

        if args.setup:
            maybe_setup(base_http)

        if args.token:
            # Token provided — still call the web backend just to get ws_url
            try:
                token_resp = get_pico_token(base_http)
                ws_url = str(token_resp.get("ws_url") or f"ws://{args.host}:{args.port}/pico/ws")
            except Exception:
                ws_url = f"ws://{args.host}:{args.port}/pico/ws"
            token = args.token
        else:
            try:
                token_resp = get_pico_token(base_http)
            except Exception as e:
                print(f"[error] {e}")
                print(f"\nHints:")
                print(f"  Is picoclaw-web running?  It listens on :{DEFAULT_WEB_PORT}")
                print(f"  Enable pico first:  {sys.argv[0]} --setup")
                print(f"  Or connect directly:   {sys.argv[0]} --direct --token <token>")
                print(f"  Read token from config: {sys.argv[0]} --direct --config ~/.picoclaw/config.json")
                sys.exit(1)

            token = str(token_resp.get("token") or "")
            ws_url = str(token_resp.get("ws_url") or "")
            enabled = bool(token_resp.get("enabled", False))

            if not token or not ws_url:
                print(f"[error] invalid /api/pico/token response: {token_resp}")
                print(f"  Run: {sys.argv[0]} --setup  to enable the pico channel")
                sys.exit(1)

            print(f"[info] mode: via web backend")
            print(f"[info] backend: {base_http}")
            print(f"[info] pico enabled: {enabled}")

        ws_url = normalize_ws_url_for_cli(ws_url, args.ws_host)

        # Set Origin to match the web backend — satisfies AllowOrigins check if
        # the pico channel was initially configured via the browser dashboard.
        from urllib.parse import urlparse
        parsed = urlparse(ws_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        origin = f"{scheme}://{parsed.netloc}"

    try:
        asyncio.run(run_chat(ws_url, token, session_id, args.message, origin=origin))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        err = str(e)
        print(f"[error] websocket failed: {err}")
        if "401" in err:
            print("\n401 Unauthorized — token mismatch or pico channel not enabled.")
            print("  Check token:   grep -A5 '\"pico\"' ~/.picoclaw/config.json")
            print(f"  Try direct:    {sys.argv[0]} --direct --token <token>")
            print(f"  Re-enable:     {sys.argv[0]} --setup")
        elif "403" in err:
            print("\n403 Forbidden — Origin not in AllowOrigins list.")
            print("  Open the dashboard in a browser to trigger setup, OR")
            print("  clear pico.allow_origins in config.json and reload the gateway.")
        elif "502" in err or "503" in err:
            print("\nGateway unavailable — is the picoclaw gateway running?")
            print(f"  Direct mode:  {sys.argv[0]} --direct --token <token>  (port {DEFAULT_GATEWAY_PORT})")
        sys.exit(1)


if __name__ == "__main__":
    main()
