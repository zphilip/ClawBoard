#!/usr/bin/env python3
"""
test_agent.py — CLI test client for clawproxy's unified /proxy/ws endpoint.

Sends one message to a chosen claw agent (zeroclaw `zc` or picoclaw `pc`) and
prints the running output. This is the quickest way to poke either agent
through clawproxy — the bridge that a phone app uses to reach the backend
claw agents running on the radxa / raspberry pi.

Usage:

    # Start clawproxy in proxy mode (in another terminal):
    #   ./clawproxy --proxy --zc-url ws://localhost:42617/ws/chat --zc-token TOKEN \
    #               --pc-url ws://localhost:18790/pico/ws --pc-token TOKEN

    # One-shot to zeroclaw (default agent):
    python3 test_agent.py --text "你好，介绍一下你自己"

    # One-shot to picoclaw:
    python3 test_agent.py --agent pc --text "你好"

    # Scriptable: only the final response on stdout, logs on stderr:
    python3 test_agent.py --agent zc --text "1+1=?" --quiet

    # Dump every raw frame (debug the bridge itself):
    python3 test_agent.py --agent pc --text "hi" --raw

    # Run artemis-control directly (skips clawproxy): launch detached, then
    # poll its log file and print live step-by-step progress:
    python3 test_agent.py --artemis --text "打开百度地图,搜索南岸花城并开始导航"

    # Interactive REPL (memory-resuming session), no --text:
    python3 test_agent.py --agent zc

Options:
    --agent      claw agent to talk to: zc (zeroclaw) or pc (picoclaw)
    --text, -m   one-shot message; omit for interactive REPL
    --host       clawproxy host (default: 127.0.0.1)
    --port       clawproxy proxy port (default: 18780)
    --session    session ID for memory resumption (default: random)
    --timeout    seconds to wait for a turn to finish (default: 900)
    --quiet      suppress streaming; print only the final response to stdout
    --raw        dump every inbound frame as JSON to stderr
    --artemis    run artemis-control directly (daemon + log polling) instead
                 of talking to clawproxy; PATH defaults to the bundled skill
    --profile    artemis profile: flash (default) or pro (--artemis mode)
    --poll       log poll interval in seconds (default: 3)
    --wait       max seconds to wait for an artemis result (default: 900)

Interactive commands:
    @zc <text>   send to zeroclaw
    @pc <text>   send to picoclaw
    /switch zc|pc
    /session <id>
    /status
    /quit
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    websocket = None
    HAS_WEBSOCKET = False

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


# ── Shared state ──────────────────────────────────────────────────────────────
active_agent = "zc"
session_ids  = {"zc": f"test-zc-{uuid.uuid4().hex[:8]}",
                "pc": f"test-pc-{uuid.uuid4().hex[:8]}"}

# A Turn tracks one outstanding request and captures its final text.
turn_done   = threading.Event()
final_text  = ""
final_error = ""
zc_buf      = []

conn_ready  = threading.Event()
conn_closed = threading.Event()


def out(s, end="\n"):
    """Streaming output → suppressed in --quiet mode (goes to stderr instead)."""
    if args.quiet:
        print(s, end=end, file=sys.stderr, flush=True)
    else:
        print(s, end=end, flush=True)


def tag4(a):
    return pzc("") if a == "zc" else ppc("")


def make_send(agent, content, session_id):
    return json.dumps({
        "type":       "message.send",
        "agent":      agent,
        "session_id": session_id,
        "id":         f"msg-{uuid.uuid4().hex[:8]}",
        "timestamp":  int(time.time() * 1000),
        "payload":    {"content": content},
    })


# ── Standalone artemis runner (--artemis) ─────────────────────────────────────
# Launches artemis_agent.py with --daemon (returns immediately with a
# run_started JSON), then polls its log file and prints each JSONL event as it
# lands. This mirrors exactly what the zeroclaw agent does through its shell
# tool, and is the quickest way to see live step-by-step progress on the test
# side without waiting minutes for one big buffered tool_result.

# Default artemis skill script, resolved relative to this test client so the
# repo layout (clawproxy/ + skills/ side by side) works out of the box.
DEFAULT_ARTEMIS_SKILL = str(
    (Path(__file__).resolve().parent.parent / "skills" / "artemis-control-v2" / "artemis_agent.py")
)
_ARTEMIS_EXIT = {"success": 0, "clarify": 2, "timeout": 3, "error": 1}


def _print_artemis_line(line: str):
    """Print one JSONL (or human-log) line. Returns the exit code when the
    line is the final `result` record, else None."""
    line = line.rstrip("\n")
    if not line.strip():
        return None
    try:
        obj = json.loads(line)
    except Exception:
        print(f"{GREY}[log]{RESET} {line}")
        return None

    t = obj.get("type")
    if t == "progress":
        step = obj.get("step")
        action = obj.get("action", "")
        msg = obj.get("message", "")
        shot = obj.get("screenshot", "")
        extra = f"  {GREY}[shot: {shot}]{RESET}" if shot else ""
        print(f"{CYAN}[step {step}]{RESET} {action} — {msg}{extra}")
    elif t == "incident":
        print(f"{YELLOW}[incident]{RESET} step {obj.get('step')}: "
              f"{obj.get('category')} — {obj.get('reason', '')} (action={obj.get('action', '')})")
    elif t == "plan":
        print(f"{CYAN}[plan]{RESET} {obj.get('total_milestones', 0)} milestones")
        if obj.get("plan_text"):
            print(obj["plan_text"])
    elif t == "result":
        status = obj.get("status")
        colour = GREEN if status == "success" else RED if status == "error" else YELLOW
        print(f"{colour}[result]{RESET} status={status} steps={obj.get('steps')}: "
              f"{obj.get('message', '')}")
        return _ARTEMIS_EXIT.get(status, 1)
    else:
        print(f"{GREY}[{t}]{RESET} {line}")
    return None


def run_artemis_direct(instruction, skill, profile, poll_s, wait_s):
    """Launch artemis detached, then poll its log file to completion.

    Returns the process exit code mapped from artemis' `result.status`
    (0 success, 2 clarify, 3 timeout, 1 error)."""
    py = getattr(sys, "executable", None) or "python3"
    cmd = [py, skill, "--instruction", instruction, "--profile", profile, "--daemon"]
    print(psys(f"launching artemis (daemon): {skill}"))
    print(psys(f"instruction: {instruction}"))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(perr("artemis did not emit run_started within 30s (daemonize failed?)"))
        return 1

    log_file = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "run_started":
            log_file = obj.get("log_file")
            break
    if not log_file:
        print(perr("no run_started JSON from artemis; raw output:"))
        print(proc.stdout or proc.stderr or "")
        return 1

    print(psys(f"detached  log_file={log_file}"))
    print(psys(f"polling every {poll_s}s (max {wait_s}s) — Ctrl-C to stop"))

    offset = 0
    deadline = time.monotonic() + wait_s
    saw_result = False
    while time.monotonic() < deadline:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
        except FileNotFoundError:
            chunk = ""
        if chunk:
            for raw in chunk.splitlines():
                code = _print_artemis_line(raw)
                if code is not None:
                    saw_result = True
                    return code
        if saw_result:
            break
        time.sleep(poll_s)

    print(perr(f"no result after {wait_s}s — still running; tail the log: {log_file}"))
    return 3


# ── Inbound message handler ───────────────────────────────────────────────────
def on_message(ws, raw):
    global final_text, final_error, zc_buf

    if args.raw:
        print(f"{GREY}[raw]{RESET} {raw}", file=sys.stderr, flush=True)

    try:
        msg = json.loads(raw)
    except Exception:
        out(perr(f"bad JSON: {raw}"))
        return

    agent = msg.get("agent", "?")
    typ   = msg.get("type", "")

    # Proxy-level frames carry no agent and are worth surfacing.
    if agent == "?":
        if typ == "status":
            agents = msg.get("agents", {})
            q = msg.get("queue", {})
            out(psys(f"proxy status: zc={agents.get('zc','-')}  pc={agents.get('pc','-')}  buffered={q.get('buffered',0)}"))
        else:
            out(psys(f"{GREY}[{typ}]{RESET} {raw}"))
        return

    if agent == "zc":
        if typ == "session_start":
            pl = msg.get("payload", msg)
            resumed = pl.get("resumed", False)
            status  = f"resumed, {pl.get('message_count', 0)} msgs" if resumed else "new"
            name    = pl.get("name", msg.get("session_id", ""))
            out(pzc(f"session ready  id={name}  status={status}"))

        elif typ == "chunk":
            content = msg.get("content", "")
            zc_buf.append(content)
            out(f"{CYAN}{content}{RESET}", end="")

        elif typ == "thinking":
            out(f"{GREY}·{RESET}", end="")

        elif typ == "tool_call":
            name = msg.get("name", "")
            tool_args = msg.get("args", {})
            out(f"\n{pzc('')}{GREY}tool_call{RESET} {name}  args={json.dumps(tool_args)}")

        elif typ == "tool_result":
            name   = msg.get("name", "")
            output = str(msg.get("output", ""))[:120].replace("\n", " ")
            out(f"{pzc('')}{GREY}tool_result{RESET} {name}  {output}")

        elif typ == "chunk_reset":
            zc_buf.clear()

        elif typ == "done":
            if zc_buf:
                out("")  # newline after streamed chunks
            final_text = "".join(zc_buf) or msg.get("full_response", "") or msg.get("content", "")
            zc_buf = []
            turn_done.set()

        elif typ == "error":
            final_error = msg.get("message", raw)
            out(perr(f"zeroclaw: {final_error}"))
            turn_done.set()

        elif typ == "session_busy":
            out(psys("zeroclaw busy — previous turn still running"))
            final_error = "session busy"
            turn_done.set()

        else:
            out(pzc(f"{GREY}[{typ}]{RESET} {raw}"))

    elif agent == "pc":
        if typ == "message.create":
            content = (msg.get("payload") or {}).get("content", "")
            final_text = content
            out(ppc(content))
            turn_done.set()

        elif typ == "message.update":
            content = (msg.get("payload") or {}).get("content", "")
            out(ppc(f"{GREY}(update){RESET} {content}"))

        elif typ == "typing.start":
            out(f"{ppc('')}{GREY}(typing...){RESET}", end="")

        elif typ == "typing.stop":
            pass

        elif typ == "pong":
            pass

        elif typ == "error":
            final_error = msg.get("message", raw)
            out(perr(f"picoclaw: {final_error}"))
            turn_done.set()

        else:
            out(ppc(f"{GREY}[{typ}]{RESET} {raw}"))


def on_error(ws, err):
    out(perr(str(err)))
    final_error = str(err)
    turn_done.set()


def on_close(ws, code, msg):
    out(psys(f"disconnected (code={code})"))
    conn_closed.set()
    turn_done.set()


def on_open(ws):
    out(psys("connected to clawproxy /proxy/ws"))
    conn_ready.set()
    ws.send(json.dumps({"type": "status"}))


# ── Send + await a turn ───────────────────────────────────────────────────────
def run_turn(ws, agent, content, sid, timeout):
    global final_text, final_error, zc_buf
    final_text = ""
    final_error = ""
    zc_buf = []
    turn_done.clear()

    ws.send(make_send(agent, content, sid))
    if not turn_done.wait(timeout):
        out(perr(f"timeout after {timeout}s — no completion from {agent}"))
        return None, "timeout"
    if final_error:
        return final_text, final_error
    return final_text, None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global active_agent, session_ids, args

    parser = argparse.ArgumentParser(description="clawproxy agent test client")
    parser.add_argument("--agent", default="zc", choices=["zc", "pc"],
                        help="claw agent to talk to: zc (zeroclaw) or pc (picoclaw)")
    parser.add_argument("--text", "-m", default=None, help="one-shot message (omit for interactive)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=18780, type=int)
    parser.add_argument("--session", default=None, help="session ID for the active agent")
    parser.add_argument("--timeout", default=900, type=int,
                        help="seconds to wait for a turn to finish (default: 900; artemis phone runs can take minutes)")
    parser.add_argument("--artemis", nargs="?", const=DEFAULT_ARTEMIS_SKILL, metavar="PATH",
                        default=None, dest="artemis",
                        help="run artemis-control directly (daemon + log polling) instead of via clawproxy; "
                             "PATH defaults to the bundled skill (../skills/artemis-control-v2/artemis_agent.py)")
    parser.add_argument("--profile", default="flash", choices=["flash", "pro"],
                        dest="artemis_profile",
                        help="artemis execution profile (--artemis mode; default: flash)")
    parser.add_argument("--poll", default=3.0, type=float, dest="artemis_poll",
                        help="log poll interval in seconds (--artemis mode; default: 3)")
    parser.add_argument("--wait", default=900, type=int, dest="artemis_wait",
                        help="max seconds to wait for an artemis result (--artemis mode; default: 900)")
    parser.add_argument("--quiet", action="store_true",
                        help="print only the final response to stdout")
    parser.add_argument("--raw", action="store_true",
                        help="dump every inbound frame to stderr")
    args = parser.parse_args()

    # ── Direct artemis mode: no clawproxy/websocket involved ─────────────────
    if args.artemis:
        if not args.text:
            out(perr("--artemis requires --text/-m <instruction>"))
            sys.exit(2)
        code = run_artemis_direct(args.text, args.artemis,
                                  args.artemis_profile, args.artemis_poll,
                                  args.artemis_wait)
        sys.exit(code)

    if not HAS_WEBSOCKET:
        print("Missing dependency: pip install websocket-client", file=sys.stderr)
        sys.exit(1)

    active_agent = args.agent
    if args.session:
        session_ids[active_agent] = args.session

    url = f"ws://{args.host}:{args.port}/proxy/ws?client_id=test-{uuid.uuid4().hex[:8]}"
    out(psys(f"connecting to {url}"))
    out(psys(f"agent={active_agent}  session={session_ids[active_agent]}"))

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    t = threading.Thread(target=ws.run_forever, kwargs={"reconnect": 0}, daemon=True)
    t.start()

    if not conn_ready.wait(args.timeout):
        out(perr("could not connect to clawproxy — is it running with --proxy?"))
        sys.exit(1)

    # ── One-shot mode ─────────────────────────────────────────────────────────
    if args.text is not None:
        text, err = run_turn(ws, active_agent, args.text,
                             session_ids[active_agent], args.timeout)
        ws.close()
        if err == "timeout":
            sys.exit(2)
        if err:
            sys.exit(1)
        if args.quiet:
            print(text, end="")
        sys.exit(0)

    # ── Interactive REPL ──────────────────────────────────────────────────────
    print(f"\n{BOLD}{YELLOW}ClawProxy agent test client{RESET}")
    print(psys(f"  {CYAN}@zc <text>{RESET}        send to zeroclaw"))
    print(psys(f"  {GREEN}@pc <text>{RESET}        send to picoclaw"))
    print(psys(f"  /switch zc|pc      change active agent (current: {YELLOW}{active_agent}{RESET})"))
    print(psys(f"  /session <id>      change session for active agent"))
    print(psys(f"  /status            proxy status"))
    print(psys(f"  /quit              exit\n"))

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
                    out(perr(f"unknown agent {target!r}"))
                continue
            if line.startswith("/session "):
                session_ids[active_agent] = line.split(None, 1)[1].strip()
                print(psys(f"{active_agent} session → {session_ids[active_agent]}"))
                continue

            if line.startswith("@zc "):
                agent, content = "zc", line[4:].strip()
            elif line.startswith("@pc "):
                agent, content = "pc", line[4:].strip()
            else:
                agent, content = active_agent, line

            run_turn(ws, agent, content, session_ids[agent], args.timeout)

    except KeyboardInterrupt:
        print()
        print(psys("interrupted"))
    finally:
        ws.close()


if __name__ == "__main__":
    main()