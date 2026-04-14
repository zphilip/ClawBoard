// clawproxy — dual-agent CLI client for zeroclaw and picoclaw gateways.
//
// # Auth
//
//   zeroclaw  — pair-code flow:  GET /health → POST /pair → bearer token
//   picoclaw  — token flow:
//                 mode 1 (default): GET /api/pico/token from web-backend (:18800)
//                 mode 2 (--pc-direct): bearer token straight to gateway (:18790)
//
// # Usage
//
//clawproxy [flags]
//
// # Flags
//
//--zc-host       localhost      zeroclaw host
//--zc-port       42617          zeroclaw gateway port
//--zc-token      TOKEN          skip pairing, use existing token
//--zc-pair-code  CODE           supply pair code non-interactively
//--zc-sid        SESSION_ID     session ID (default: random)
//
//--pc-host       localhost      picoclaw host
//--pc-port       18800          picoclaw web-backend port (mode 1)
//--pc-gw-port    18790          picoclaw gateway port (mode 2, --pc-direct)
//--pc-direct                    connect directly to gateway, skip web-backend
//--pc-token      TOKEN          picoclaw token (required for --pc-direct)
//--pc-sid        SESSION_ID     session ID (default: random)
//
//--active        zc|pc          default agent (default: zc)
//
// # CLI commands
//
//@zc <text>   send to zeroclaw
//@pc <text>   send to picoclaw
///switch zc|pc  change active agent
///status        connection status
///quit
package main

import (
"bufio"
"encoding/json"
"flag"
"fmt"
"io"
"net/http"
"os"
"strings"
"sync"
"sync/atomic"
"time"

"github.com/gorilla/websocket"
)

// ── ANSI colour helpers ───────────────────────────────────────────────────────

const (
colReset  = "\033[0m"
colBold   = "\033[1m"
colGrey   = "\033[90m"
colCyan   = "\033[1;36m" // zeroclaw
colGreen  = "\033[1;32m" // picoclaw
colYellow = "\033[1;33m" // system / meta
colRed    = "\033[1;31m" // error
)

func prefixZC() string  { return colCyan + "[zc]" + colReset + " " }
func prefixPC() string  { return colGreen + "[pc]" + colReset + " " }
func prefixSYS() string { return colYellow + "[sys]" + colReset + " " }
func prefixERR() string { return colRed + "[err]" + colReset + " " }

// ── HTTP helpers ──────────────────────────────────────────────────────────────

func httpGet(url string) (map[string]any, error) {
resp, err := http.Get(url) //nolint:gosec
if err != nil {
return nil, err
}
defer resp.Body.Close()
body, _ := io.ReadAll(resp.Body)
var out map[string]any
if err := json.Unmarshal(body, &out); err != nil {
return nil, fmt.Errorf("bad JSON from %s: %w", url, err)
}
return out, nil
}

func httpPost(url string, headers map[string]string) (map[string]any, error) {
req, _ := http.NewRequest(http.MethodPost, url, nil)
for k, v := range headers {
req.Header.Set(k, v)
}
resp, err := http.DefaultClient.Do(req)
if err != nil {
return nil, err
}
defer resp.Body.Close()
body, _ := io.ReadAll(resp.Body)
if resp.StatusCode >= 400 {
return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
}
var out map[string]any
if err := json.Unmarshal(body, &out); err != nil {
return nil, fmt.Errorf("bad JSON: %w", err)
}
return out, nil
}

// ── zeroclaw auth: GET /health → POST /pair ───────────────────────────────────

type zcAuth struct {
host      string
port      int
token     string // pre-supplied; set after successful pair()
pairCode  string // from --zc-pair-code or interactive prompt
}

// setup resolves the bearer token, prompting for a pair code if needed.
func (z *zcAuth) setup() error {
base := fmt.Sprintf("http://%s:%d", z.host, z.port)

// health check
health, err := httpGet(base + "/health")
if err != nil {
return fmt.Errorf("cannot reach zeroclaw at %s: %w\n  Is zeroclaw running?  zeroclaw gateway", base, err)
}

requirePairing, _ := health["require_pairing"].(bool)
fmt.Printf("%szeroclaw gateway: %s  require_pairing=%v\n", prefixZC(), base, requirePairing)

if !requirePairing || z.token != "" {
return nil // nothing to do
}

// need to pair
code := z.pairCode
if code == "" {
fmt.Printf("%sGet your pair code:  %szeroclaw gateway get-paircode%s\n",
prefixZC(), colYellow, colReset)
fmt.Print(prefixZC() + "Pairing code: ")
fmt.Scanln(&code)
code = strings.TrimSpace(code)
}
if code == "" {
return fmt.Errorf("pairing code required")
}

data, err := httpPost(base+"/pair", map[string]string{"X-Pairing-Code": code})
if err != nil {
return fmt.Errorf("pairing failed: %w", err)
}
tok, ok := data["token"].(string)
if !ok || tok == "" {
return fmt.Errorf("pair response missing token: %v", data)
}
z.token = tok
fmt.Printf("%sPaired! token=%s…\n", prefixZC(), tok[:min(12, len(tok))])
return nil
}

// wsURL builds the zeroclaw WebSocket URL including the token.
func (z *zcAuth) wsURL(sessionID string) string {
url := fmt.Sprintf("ws://%s:%d/ws/chat?session_id=%s", z.host, z.port, sessionID)
if z.token != "" {
url += "&token=" + z.token
}
return url
}

// ── picoclaw auth: web-backend (/api/pico/token) or direct ───────────────────

type pcAuth struct {
host    string
webPort int    // web-backend port (:18800), used in mode 1
gwPort  int    // gateway port (:18790), used in mode 2
direct  bool   // skip web-backend, connect straight to gateway
token   string // resolved token
wsURL   string // resolved WebSocket URL (set by setup())
}

// setup resolves the token and WS URL.
func (p *pcAuth) setup() error {
if p.direct {
// Mode 2 — direct to gateway
if p.token == "" {
return fmt.Errorf(
"--pc-direct requires --pc-token\n  hint: get token from picoclaw config.json: jq '.channels.pico.token' ~/.picoclaw/config.json")
}
p.wsURL = fmt.Sprintf("ws://%s:%d/pico/ws", p.host, p.gwPort)
fmt.Printf("%spicoclaw direct gateway: %s\n", prefixPC(), p.wsURL)
return nil
}

// Mode 1 — via web-backend
base := fmt.Sprintf("http://%s:%d", p.host, p.webPort)
data, err := httpGet(base + "/api/pico/token")
if err != nil {
return fmt.Errorf(
"cannot reach picoclaw-web at %s: %w\n  Is picoclaw-web running?\n  Or use direct mode: --pc-direct --pc-token <token>",
base, err)
}

tok, _ := data["token"].(string)
wsURL, _ := data["ws_url"].(string)
enabled, _ := data["enabled"].(bool)

if tok == "" || wsURL == "" {
return fmt.Errorf("invalid /api/pico/token response: %v\n  Run --pc-setup or use --pc-direct", data)
}

p.token = tok
p.wsURL = wsURL
fmt.Printf("%spicoclaw web-backend: %s  enabled=%v\n", prefixPC(), base, enabled)
return nil
}

// ── Protocol types ────────────────────────────────────────────────────────────

// zeroclaw.v1 — inbound from server
type zcMsg struct {
Type         string          `json:"type"`
Content      string          `json:"content,omitempty"`
FullResponse string          `json:"full_response,omitempty"`
SessionID    string          `json:"session_id,omitempty"`
Name         string          `json:"name,omitempty"`
ToolName     string          `json:"name,omitempty"`
Args         json.RawMessage `json:"args,omitempty"`
Output       string          `json:"output,omitempty"`
Message      string          `json:"message,omitempty"`
Resumed      bool            `json:"resumed,omitempty"`
MessageCount int             `json:"message_count,omitempty"`
}

// Pico Protocol — inbound from server
type picoMsg struct {
Type      string         `json:"type"`
ID        string         `json:"id,omitempty"`
SessionID string         `json:"session_id,omitempty"`
Timestamp int64          `json:"timestamp,omitempty"`
Payload   map[string]any `json:"payload,omitempty"`
}

func newPicoSend(sessionID, content string) picoMsg {
return picoMsg{
Type:      "message.send",
ID:        fmt.Sprintf("msg-%d", time.Now().UnixMilli()),
SessionID: sessionID,
Timestamp: time.Now().UnixMilli(),
Payload:   map[string]any{"content": content},
}
}

func newPicoPing() picoMsg {
return picoMsg{Type: "ping", ID: fmt.Sprintf("ping-%d", time.Now().UnixMilli()), Timestamp: time.Now().UnixMilli()}
}

// ── Agent connection ──────────────────────────────────────────────────────────

type agentKind string

const (
kindZC agentKind = "zc"
kindPC agentKind = "pc"
)

type agentConn struct {
kind      agentKind
wsURL     string // fully resolved WS URL (token embedded or in header)
token     string // used for picoclaw Sec-WebSocket-Protocol subprotocol
sessionID string

mu        sync.Mutex
conn      *websocket.Conn
connected atomic.Bool

zcBuf strings.Builder // accumulate zeroclaw stream chunks
}

// dial connects and starts the read loop.
func (a *agentConn) dial() error {
header := http.Header{}
var dialer *websocket.Dialer

if a.kind == kindPC && a.token != "" {
// picoclaw: Authorization header + Sec-WebSocket-Protocol: token.<value>
// (mirrors chat_picoclaw.py: both auth methods for maximum compatibility)
header.Set("Authorization", "Bearer "+a.token)
dialer = &websocket.Dialer{
HandshakeTimeout: 10 * time.Second,
Subprotocols:     []string{"token." + a.token},
}
} else {
// zeroclaw: token already in WS URL query param; add header as fallback
if a.token != "" {
header.Set("Authorization", "Bearer "+a.token)
}
dialer = &websocket.Dialer{
HandshakeTimeout: 10 * time.Second,
Subprotocols:     []string{"zeroclaw.v1"},
}
}

conn, resp, err := dialer.Dial(a.wsURL, header)
if resp != nil && resp.Body != nil {
resp.Body.Close()
}
if err != nil {
return err
}

a.mu.Lock()
a.conn = conn
a.mu.Unlock()
a.connected.Store(true)

go a.readLoop()
return nil
}

// reconnectLoop keeps re-dialing after disconnection.
func (a *agentConn) reconnectLoop() {
for {
if !a.connected.Load() {
fmt.Printf("%s%s reconnecting...\n", prefixSYS(), a.kind)
if err := a.dial(); err != nil {
fmt.Printf("%s%s connect failed: %v — retry in 5s\n", prefixERR(), a.kind, err)
time.Sleep(5 * time.Second)
continue
}
fmt.Printf("%s%s reconnected\n", prefixSYS(), a.kind)
}
time.Sleep(2 * time.Second)
}
}

// readLoop reads messages from the gateway and prints them.
func (a *agentConn) readLoop() {
defer func() {
a.connected.Store(false)
a.mu.Lock()
if a.conn != nil {
a.conn.Close()
a.conn = nil
}
a.mu.Unlock()
fmt.Printf("%s%s disconnected\n", prefixSYS(), a.kind)
}()

for {
a.mu.Lock()
conn := a.conn
a.mu.Unlock()
if conn == nil {
return
}
_, raw, err := conn.ReadMessage()
if err != nil {
if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
fmt.Printf("%s%s read error: %v\n", prefixERR(), a.kind, err)
}
return
}
if a.kind == kindZC {
a.handleZC(raw)
} else {
a.handlePC(raw)
}
}
}

// handleZC processes a zeroclaw gateway message.
func (a *agentConn) handleZC(raw []byte) {
var msg zcMsg
if err := json.Unmarshal(raw, &msg); err != nil {
fmt.Printf("%sbad json: %s\n", prefixERR(), raw)
return
}
switch msg.Type {
case "session_start":
status := "new"
if msg.Resumed {
status = fmt.Sprintf("resumed, %d msgs", msg.MessageCount)
}
name := msg.SessionID
if msg.Name != "" {
name = msg.Name
}
fmt.Printf("%ssession ready  id=%s  status=%s  name=%s\n", prefixZC(), msg.SessionID, status, name)
case "chunk":
a.zcBuf.WriteString(msg.Content)
fmt.Print(prefixZC() + colCyan + msg.Content + colReset)
case "thinking":
fmt.Print(colGrey + "·" + colReset)
case "tool_call":
fmt.Printf("\n%s%stool_call%s %s  args=%s\n", prefixZC(), colGrey, colReset, msg.ToolName, msg.Args)
case "tool_result":
fmt.Printf("%s%stool_result%s %s  output=%s\n", prefixZC(), colGrey, colReset, msg.ToolName, truncate(msg.Output, 120))
case "done":
if a.zcBuf.Len() > 0 {
fmt.Println()
a.zcBuf.Reset()
} else if msg.FullResponse != "" {
fmt.Printf("%s%s\n", prefixZC(), msg.FullResponse)
}
case "chunk_reset":
a.zcBuf.Reset()
case "error":
fmt.Printf("%szeroclaw error: %s\n", prefixERR(), msg.Message)
case "session_busy":
fmt.Printf("%szeroclaw busy — previous turn still running\n", prefixSYS())
default:
fmt.Printf("%s%s[%s]%s %s\n", prefixZC(), colGrey, msg.Type, colReset, raw)
}
}

// handlePC processes a picoclaw Pico Protocol message.
func (a *agentConn) handlePC(raw []byte) {
var msg picoMsg
if err := json.Unmarshal(raw, &msg); err != nil {
fmt.Printf("%sbad json: %s\n", prefixERR(), raw)
return
}
switch msg.Type {
case "pong":
// keepalive, silent
case "message.create":
content, _ := msg.Payload["content"].(string)
fmt.Printf("%s%s\n", prefixPC(), content)
case "message.update":
content, _ := msg.Payload["content"].(string)
fmt.Printf("%s%s(update)%s %s\n", prefixPC(), colGrey, colReset, content)
case "typing.start":
fmt.Printf("%s%s(typing...)%s\n", prefixPC(), colGrey, colReset)
case "typing.stop":
// silent
default:
fmt.Printf("%s%s[%s]%s %s\n", prefixPC(), colGrey, msg.Type, colReset, raw)
}
}

// sendZC sends a chat message to zeroclaw.
func (a *agentConn) sendZC(content string) error {
if !a.connected.Load() {
return fmt.Errorf("zeroclaw not connected")
}
a.mu.Lock()
defer a.mu.Unlock()
return a.conn.WriteJSON(map[string]any{"type": "message", "content": content})
}

// sendPC sends a message via Pico Protocol.
func (a *agentConn) sendPC(content string) error {
if !a.connected.Load() {
return fmt.Errorf("picoclaw not connected")
}
a.mu.Lock()
defer a.mu.Unlock()
return a.conn.WriteJSON(newPicoSend(a.sessionID, content))
}

func (a *agentConn) pingPC() {
if !a.connected.Load() {
return
}
a.mu.Lock()
defer a.mu.Unlock()
_ = a.conn.WriteJSON(newPicoPing())
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func truncate(s string, n int) string {
s = strings.ReplaceAll(s, "\n", " ")
if len(s) <= n {
return s
}
return s[:n] + "…"
}

func min(a, b int) int {
if a < b {
return a
}
return b
}

// ── Main ──────────────────────────────────────────────────────────────────────

func main() {
// zeroclaw flags
zcHost      := flag.String("zc-host",      "127.0.0.1", "zeroclaw host")
zcPort      := flag.Int("zc-port",         42617,        "zeroclaw gateway port")
zcToken     := flag.String("zc-token",     "",           "zeroclaw bearer token (skips pairing)")
zcPairCode  := flag.String("zc-pair-code", "",           "zeroclaw pair code (non-interactive)")
zcSID       := flag.String("zc-sid",       "",           "zeroclaw session ID")
zcEnable    := flag.Bool("zc",             false,        "enable zeroclaw connection")

// picoclaw flags
pcHost      := flag.String("pc-host",      "127.0.0.1", "picoclaw host")
pcPort      := flag.Int("pc-port",         18800,        "picoclaw web-backend port (mode 1)")
pcGWPort    := flag.Int("pc-gw-port",      18790,        "picoclaw gateway port (--pc-direct mode)")
pcDirect    := flag.Bool("pc-direct",      false,        "connect directly to gateway, skip web-backend")
pcToken     := flag.String("pc-token",     "",           "picoclaw token (required for --pc-direct)")
pcSID       := flag.String("pc-sid",       "",           "picoclaw session ID")
pcEnable    := flag.Bool("pc",             false,        "enable picoclaw connection")

active := flag.String("active", "zc", "default active agent: zc or pc")
flag.Parse()

// Enable an agent if its host/token flags were explicitly set, or via --zc/--pc
if *zcToken != "" || *zcPairCode != "" {
*zcEnable = true
}
if *pcToken != "" || *pcDirect {
*pcEnable = true
}
// If neither explicit enable, enable both (let auth setup fail gracefully)
if !*zcEnable && !*pcEnable {
*zcEnable = true
*pcEnable = true
}

// Session IDs
if *zcSID == "" {
*zcSID = fmt.Sprintf("clawproxy-zc-%d", time.Now().UnixMilli())
}
if *pcSID == "" {
*pcSID = fmt.Sprintf("clawproxy-pc-%d", time.Now().UnixMilli())
}

activeAgent := agentKind(*active)
if activeAgent != kindZC && activeAgent != kindPC {
fmt.Fprintf(os.Stderr, "Error: --active must be 'zc' or 'pc'\n")
os.Exit(1)
}

// ── zeroclaw: pair-code auth ──────────────────────────────────────
var zc *agentConn
if *zcEnable {
auth := &zcAuth{host: *zcHost, port: *zcPort, token: *zcToken, pairCode: *zcPairCode}
if err := auth.setup(); err != nil {
fmt.Printf("%s%v\n", prefixERR(), err)
} else {
zc = &agentConn{
kind:      kindZC,
wsURL:     auth.wsURL(*zcSID),
token:     auth.token,
sessionID: *zcSID,
}
fmt.Printf("%sConnecting: %s\n", prefixZC(), zc.wsURL)
if err := zc.dial(); err != nil {
fmt.Printf("%szeroclaw connect failed: %v\n", prefixERR(), err)
zc = nil
} else {
go zc.reconnectLoop()
}
}
}

// ── picoclaw: token auth (web-backend or direct) ──────────────────
var pc *agentConn
if *pcEnable {
auth := &pcAuth{
host:    *pcHost,
webPort: *pcPort,
gwPort:  *pcGWPort,
direct:  *pcDirect,
token:   *pcToken,
}
if err := auth.setup(); err != nil {
fmt.Printf("%s%v\n", prefixERR(), err)
} else {
wsURL := auth.wsURL
if !strings.Contains(wsURL, "session_id=") {
sep := "?"
if strings.Contains(wsURL, "?") {
sep = "&"
}
wsURL += sep + "session_id=" + *pcSID
}
pc = &agentConn{
kind:      kindPC,
wsURL:     wsURL,
token:     auth.token,
sessionID: *pcSID,
}
fmt.Printf("%sConnecting: %s\n", prefixPC(), pc.wsURL)
if err := pc.dial(); err != nil {
fmt.Printf("%spicoclaw connect failed: %v\n", prefixERR(), err)
pc = nil
} else {
go pc.reconnectLoop()
go func() {
t := time.NewTicker(30 * time.Second)
defer t.Stop()
for range t.C {
pc.pingPC()
}
}()
}
}
}

if zc == nil && pc == nil {
fmt.Fprintln(os.Stderr, "Error: no agents connected. Check flags and gateway availability.")
os.Exit(1)
}

// ── Banner ────────────────────────────────────────────────────────
fmt.Printf("\n%s%sClawProxy v1%s — dual-agent CLI\n", prefixSYS(), colBold, colReset)
fmt.Printf("%s  %s@zc <text>%s     send to zeroclaw\n", prefixSYS(), colCyan, colReset)
fmt.Printf("%s  %s@pc <text>%s     send to picoclaw\n", prefixSYS(), colGreen, colReset)
fmt.Printf("%s  /switch zc|pc    change active agent (current: %s%s%s)\n",
prefixSYS(), colYellow, activeAgent, colReset)
fmt.Printf("%s  /status          connection status\n", prefixSYS())
fmt.Printf("%s  /quit            exit\n\n", prefixSYS())

// ── Interactive loop ──────────────────────────────────────────────
scanner := bufio.NewScanner(os.Stdin)
for {
var promptColor string
if activeAgent == kindZC {
promptColor = colCyan
} else {
promptColor = colGreen
}
fmt.Printf("%s[%s%s%s]%s ", colBold, promptColor, activeAgent, colReset+colBold, colReset)

if !scanner.Scan() {
break
}
line := strings.TrimSpace(scanner.Text())
if line == "" {
continue
}

if line == "/quit" || line == "/exit" {
fmt.Println(prefixSYS() + "bye!")
break
}

if line == "/status" {
printStatus(zc, pc, activeAgent)
continue
}

if strings.HasPrefix(line, "/switch ") {
target := agentKind(strings.TrimSpace(strings.TrimPrefix(line, "/switch ")))
switch target {
case kindZC:
if zc == nil {
fmt.Println(prefixERR() + "zeroclaw not connected")
} else {
activeAgent = kindZC
fmt.Printf("%sActive → %szc%s\n", prefixSYS(), colCyan, colReset)
}
case kindPC:
if pc == nil {
fmt.Println(prefixERR() + "picoclaw not connected")
} else {
activeAgent = kindPC
fmt.Printf("%sActive → %spc%s\n", prefixSYS(), colGreen, colReset)
}
default:
fmt.Printf("%sunknown agent %q — use 'zc' or 'pc'\n", prefixERR(), target)
}
continue
}

// resolve target + content
var target agentKind
var content string
switch {
case strings.HasPrefix(line, "@zc "):
target = kindZC
content = strings.TrimSpace(strings.TrimPrefix(line, "@zc "))
case strings.HasPrefix(line, "@pc "):
target = kindPC
content = strings.TrimSpace(strings.TrimPrefix(line, "@pc "))
default:
target = activeAgent
content = line
}

var sendErr error
switch target {
case kindZC:
if zc == nil {
fmt.Println(prefixERR() + "zeroclaw not connected")
continue
}
sendErr = zc.sendZC(content)
case kindPC:
if pc == nil {
fmt.Println(prefixERR() + "picoclaw not connected")
continue
}
sendErr = pc.sendPC(content)
}
if sendErr != nil {
fmt.Printf("%ssend to %s failed: %v\n", prefixERR(), target, sendErr)
}
}

if err := scanner.Err(); err != nil {
fmt.Fprintf(os.Stderr, "stdin error: %v\n", err)
}
}

func printStatus(zc, pc *agentConn, active agentKind) {
print1 := func(a *agentConn, label agentKind) {
if a == nil {
fmt.Printf("%s  %s: %snot connected%s\n", prefixSYS(), label, colGrey, colReset)
return
}
state := colRed + "disconnected" + colReset
if a.connected.Load() {
state = colGreen + "connected" + colReset
}
marker := ""
if label == active {
marker = colYellow + " ← active" + colReset
}
fmt.Printf("%s  %s: %s  url=%s  sid=%s%s\n",
prefixSYS(), label, state, a.wsURL, a.sessionID, marker)
}
fmt.Println(prefixSYS() + "Connection status:")
print1(zc, kindZC)
print1(pc, kindPC)
}
