#!/usr/bin/env python3
"""
test_proxy.py — interactive test client for clawproxy v2 proxy server.

Connects to ws://localhost:18780/proxy/ws and lets you chat with
zeroclaw and picoclaw through the proxy.

Usage:
    python3 test_proxy.py [--host 127.0.0.1] [--port 18780] [--agent zc|pc]

CLI commands (same as clawproxy CLI mode):
    @zc <text>      send to zeroclaw
    @pc <text>      send to picoclaw
    /switch zc|pc   change active agent
    /session <id>   set session ID (default: auto)
    /status         ask proxy for agent status
    /quit
    <text>          send to active agent
"""

import argparse
import json
import sys
import threading
import time
import uuid

try:
    import websocket
except ImportError:
    print("Missing dependency: pip install websocket-client")
    sys.exit(1)

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREY   = "\033[90m"
CYAN   = "\033[1;36m"   # zeroclaw
GREEN  = "\033[1;32m"   # picoclaw
YELLOW = "\033[1;33m"   # system
RED    = "\033[1;31m"   # error

def pzc(s):  return f"{CYAN}[zc]{RESET} {s}"
def ppc(s):  return f"{GREEN}[pc]{RESET} {s}"
def psys(s): return f"{YELLOW}[sys]{RESET} {s}"
def perr(s): return f"{RED}[err]{RESET} {s}"

# ── State ─────────────────────────────────────────────────────────────────────
active_agent = "zc"
session_ids  = {"zc": f"test-zc-{uuid.uuid4().hex[:8]}",
                "pc": f"test-pc-{uuid.uuid4().hex[:8]}"}
zc_buf       = []   # accumulate chunk fragments

# ── Message builder ───────────────────────────────────────────────────────────
def make_send(agent, content, session_id):
    return json.dumps({
        "type":       "message.send",
        "agent":      agent,
        "session_id": session_id,
        "id":         f"msg-{uuid.uuid4().hex[:8]}",
        "timestamp":  int(time.time() * 1000),
        "payload":    {"content": content},
    })

# ── Inbound message handler ───────────────────────────────────────────────────
def on_message(ws, raw):
    global zc_buf
    try:
        msg = json.loads(raw)
    except Exception:
        print(perr(f"bad JSON: {raw}"))
        return

    agent = msg.get("agent", "?")
    sid   = msg.get("session_id", "")
    typ   = msg.get("type", "")

    def tag(a):
        return pzc("") if a == "zc" else ppc("")

    if agent == "zc":
        if typ == "session_start":
            pl = msg.get("payload", msg)
            resumed = pl.get("resumed", False)
            status  = f"resumed, {pl.get('message_count',0)} msgs" if resumed else "new"
            name    = pl.get("name", sid)
            print(pzc(f"session ready  id={sid}  status={status}  name={name}"))

        elif typ == "chunk":
            content = msg.get("content", "")
            zc_buf.append(content)
            print(f"{CYAN}{content}{RESET}", end="", flush=True)

        elif typ == "thinking":
            print(f"{GREY}·{RESET}", end="", flush=True)

        elif typ == "tool_call":
            name = msg.get("name", "")
            args = msg.get("args", {})
            print(f"\n{pzc('')}{GREY}tool_call{RESET} {name}  args={json.dumps(args)}")

        elif typ == "tool_result":
            name   = msg.get("name", "")
            output = str(msg.get("output", ""))[:120].replace("\n", " ")
            print(f"{pzc('')}{GREY}tool_result{RESET} {name}  {output}")

        elif typ == "done":
            if zc_buf:
                print()   # newline after streamed chunks
                zc_buf.clear()
            else:
                fr = msg.get("full_response", "")
                if fr:
                    print(pzc(fr))

        elif typ == "chunk_reset":
            zc_buf.clear()

        elif typ == "error":
            print(perr(f"zeroclaw: {msg.get('message',raw)}"))

        elif typ == "session_busy":
            print(psys("zeroclaw busy — previous turn still running"))

        else:
            print(pzc(f"{GREY}[{typ}]{RESET} {raw}"))

    elif agent == "pc":
        if typ == "message.create":
            content = (msg.get("payload") or {}).get("content", "")
            print(ppc(content))

        elif typ == "message.update":
            content = (msg.get("payload") or {}).get("content", "")
            print(ppc(f"{GREY}(update){RESET} {content}"))

        elif typ == "typing.start":
            print(ppc(f"{GREY}(typing...){RESET}"), end="", flush=True)

        elif typ == "typing.stop":
            pass

        elif typ == "pong":
            pass  # keepalive, silent

        elif typ == "error":
            print(perr(f"picoclaw: {msg.get('message',raw)}"))

        else:
            print(ppc(f"{GREY}[{typ}]{RESET} {raw}"))

    elif typ == "pong":
        pass  # proxy-level keepalive

    elif typ == "status":
        agents = msg.get("agents", {})
        print(psys(f"proxy status: zc={agents.get('zc','?')}  pc={agents.get('pc','?')}"))

    elif typ == "error":
        print(perr(f"proxy: {msg.get('message', raw)}"))

    else:
        print(psys(f"{GREY}[{typ}]{RESET} {raw}"))

def on_error(ws, err):
    print(perr(str(err)))

def on_close(ws, code, msg):
    print(psys(f"disconnected (code={code})"))

def on_open(ws):
    print(psys("connected to clawproxy"))
    ws.send(json.dumps({"type": "status"}))

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global active_agent, session_ids

    parser = argparse.ArgumentParser(description="clawproxy test client")
    parser.add_argument("--host",    default="127.0.0.1")
    parser.add_argument("--port",    default=18780, type=int)
    parser.add_argument("--agent",   default="zc", choices=["zc", "pc"],
                        help="default active agent")
    parser.add_argument("--zc-session", default=None, help="zeroclaw session ID")
    parser.add_argument("--pc-session", default=None, help="picoclaw session ID")
    args = parser.parse_args()

    active_agent = args.agent
    if args.zc_session:
        session_ids["zc"] = args.zc_session
    if args.pc_session:
        session_ids["pc"] = args.pc_session

    url = f"ws://{args.host}:{args.port}/proxy/ws"
    print(psys(f"connecting to {url}"))
    print(psys(f"sessions  zc={session_ids['zc']}  pc={session_ids['pc']}"))

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    t = threading.Thread(target=ws.run_forever, kwargs={"reconnect": 3}, daemon=True)
    t.start()
    time.sleep(0.5)  # wait for connection

    # ── Banner ────────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{YELLOW}ClawProxy test client{RESET}")
    print(psys(f"  {CYAN}@zc <text>{RESET}        send to zeroclaw"))
    print(psys(f"  {GREEN}@pc <text>{RESET}        send to picoclaw"))
    print(psys(f"  /switch zc|pc      change active agent (current: {YELLOW}{active_agent}{RESET})"))
    print(psys(f"  /session <id>      change session ID for active agent"))
    print(psys(f"  /status            ask proxy for status"))
    print(psys(f"  /quit              exit\n"))

    # ── REPL ──────────────────────────────────────────────────────────────────
    try:
        while True:
            color = CYAN if active_agent == "zc" else GREEN
            try:
                line = input(f"{BOLD}[{color}{active_agent}{RESET}{BOLD}]{RESET} ").strip()
            except EOFError:
                break

            if not line:
                continue

            if line in ("/quit", "/exit"):
                print(psys("bye!"))
                break

            if line == "/status":
                ws.send(json.dumps({"type": "status"}))
                continue

            if line.startswith("/switch "):
                target = line.split(None, 1)[1].strip()
                if target in ("zc", "pc"):
                    active_agent = target
                    print(psys(f"active → {target}"))
                else:
                    print(perr(f"unknown agent {target!r}"))
                continue

            if line.startswith("/session "):
                new_sid = line.split(None, 1)[1].strip()
                session_ids[active_agent] = new_sid
                print(psys(f"{active_agent} session → {new_sid}"))
                continue

            # resolve agent + content
            if line.startswith("@zc "):
                agent   = "zc"
                content = line[4:].strip()
            elif line.startswith("@pc "):
                agent   = "pc"
                content = line[4:].strip()
            else:
                agent   = active_agent
                content = line

            sid = session_ids[agent]
            ws.send(make_send(agent, content, sid))

    except KeyboardInterrupt:
        print()
        print(psys("interrupted"))
    finally:
        ws.close()

if __name__ == "__main__":
    main()
