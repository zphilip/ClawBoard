# clawproxy

Dual-agent CLI client for **zeroclaw** and **picoclaw** gateways.

Connects to both WebSocket gateways simultaneously and lets you send messages
to either agent — or both — from a single interactive terminal prompt.

---

## Build

```bash
cd ClawBoard/clawproxy
go build -o clawproxy .
```

## Usage

```bash
clawproxy \
  --zc-url   ws://localhost:42617/ws/chat \
  --zc-token TOKEN \
  --pc-url   ws://localhost:18790/ws \
  --pc-token TOKEN
```

One or both agents can be omitted. If only `--zc-url` is given, picoclaw
commands are rejected with a clear error and vice-versa.

## Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--zc-url` | — | zeroclaw gateway WS URL |
| `--zc-token` | — | zeroclaw bearer token |
| `--zc-sid` | random | zeroclaw session ID (for memory resumption) |
| `--pc-url` | — | picoclaw gateway WS URL |
| `--pc-token` | — | picoclaw bearer token |
| `--pc-sid` | random | picoclaw session ID |
| `--active` | `zc` | default agent for bare messages |

## CLI Commands

| Input | Action |
|-------|--------|
| `@zc <text>` | Send to zeroclaw regardless of active agent |
| `@pc <text>` | Send to picoclaw regardless of active agent |
| `/switch zc` | Make zeroclaw the active agent |
| `/switch pc` | Make picoclaw the active agent |
| `/status` | Show connection status of both agents |
| `/quit` | Exit |
| `<any text>` | Send to currently active agent |

## Protocol Details

### zeroclaw (`zeroclaw.v1`)
- Client → `{"type":"message","content":"..."}`
- Server → `{"type":"chunk","content":"..."}` (streamed), then `{"type":"done"}`
- Server → `{"type":"tool_call",...}` / `{"type":"tool_result",...}`

### picoclaw (Pico Protocol)
- Client → `{"type":"message.send","session_id":"...","payload":{"content":"..."}}`
- Server → `{"type":"message.create","payload":{"content":"..."}}`
- Keepalive: `{"type":"ping"}` / `{"type":"pong"}` every 30s

---

## Roadmap

This is **v1 — CLI client only**. Planned versions:

| Version | Description |
|---------|-------------|
| v1 | ✅ CLI client, dual-agent, interactive prompt |
| v2 | Proxy mode: app connects to clawproxy; clawproxy relays to both agents |
| v3 | Offline queue: buffer messages when app is offline, drain on reconnect |
| v4 | Persistent store (SQLite), TTL, delivery ack (`message.ack`) |
