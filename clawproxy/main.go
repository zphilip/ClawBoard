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
colCyan   = "\033[1;36m"
colGreen  = "\033[1;32m"
colYellow = "\033[1;33m"
colRed    = "\033[1;31m"
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

// ── zeroclaw auth ─────────────────────────────────────────────────────────────

type zcAuth struct {
host     string
port     int
token    string
pairCode string
}

func (z *zcAuth) setup() error {
	base := fmt.Sprintf("http://%s:%d", z.host, z.port)

	// load saved token if none supplied
	if z.token == "" {
		z.token = loadToken("zc_token")
		if z.token != "" {
			fmt.Printf("%szeroclaw token loaded from ~/.clawproxy/zc_token\n", prefixZC())
		}
	}

	health, err := httpGet(base + "/health")
	if err != nil {
		return fmt.Errorf("cannot reach zeroclaw at %s: %w", base, err)
	}
	requirePairing, _ := health["require_pairing"].(bool)
	fmt.Printf("%szeroclaw %s  require_pairing=%v\n", prefixZC(), base, requirePairing)
	if !requirePairing || z.token != "" {
		return nil
	}

	// need to pair
	code := z.pairCode
	if code == "" {
		fmt.Printf("%sGet pair code:  %szeroclaw gateway get-paircode%s\n", prefixZC(), colYellow, colReset)
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
	saveToken("zc_token", tok)
	fmt.Printf("%sPaired!  token saved to ~/.clawproxy/zc_token\n", prefixZC())
	return nil
}

func (z *zcAuth) wsURL(sessionID string) string {
u := fmt.Sprintf("ws://%s:%d/ws/chat?session_id=%s", z.host, z.port, sessionID)
if z.token != "" {
u += "&token=" + z.token
}
return u
}

// ── picoclaw auth ─────────────────────────────────────────────────────────────

type pcAuth struct {
host    string
webPort int
gwPort  int
direct  bool
token   string
wsURL   string
}

// ── Token store (~/.clawproxy/) ──────────────────────────────────────────────
//
// Tokens are persisted as plain text files so auth survives restarts:
//   ~/.clawproxy/zc_token   — zeroclaw bearer token
//   ~/.clawproxy/pc_token   — picoclaw bearer token

func clawproxyDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	dir := home + "/.clawproxy"
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return dir, nil
}

func saveToken(filename, token string) {
	dir, err := clawproxyDir()
	if err != nil {
		return
	}
	_ = os.WriteFile(dir+"/"+filename, []byte(token), 0o600)
}

func loadToken(filename string) string {
	dir, err := clawproxyDir()
	if err != nil {
		return ""
	}
	b, err := os.ReadFile(dir + "/" + filename)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func deleteToken(filename string) {
	dir, err := clawproxyDir()
	if err != nil {
		return
	}
	_ = os.Remove(dir + "/" + filename)
}

// readPicoTokenFromConfig assembles the picoclaw bearer token from the two
// runtime files that picoclaw writes under /var/lib/picoclaw/.picoclaw/:
//
//	.picoclaw.pid   — the PID token (numeric string)
//	.security.yml   — the channel token under channels.pico.token
//
// The combined token has the form:  pico-<pidToken><picoToken>
// which is what the picoclaw gateway expects as the WS bearer token.
func readPicoTokenFromConfig() (string, error) {
	const (
		pidFile = "/var/lib/picoclaw/.picoclaw/.picoclaw.pid"
		ymlFile = "/var/lib/picoclaw/.picoclaw/.security.yml"
	)

	// ── 1. PID token ──────────────────────────────────────────────────────
	pidRaw, err := os.ReadFile(pidFile)
	if err != nil {
		return "", fmt.Errorf("cannot read %s: %w", pidFile, err)
	}
	pidToken := strings.TrimSpace(string(pidRaw))
	if pidToken == "" {
		return "", fmt.Errorf("%s is empty", pidFile)
	}

	// ── 2. Channel token from .security.yml ──────────────────────────────
	ymlRaw, err := os.ReadFile(ymlFile)
	if err != nil {
		return "", fmt.Errorf("cannot read %s: %w", ymlFile, err)
	}
	// Parse only the lines we care about; avoid a YAML dependency.
	// We look for the value under the 'pico:' block's 'token:' key.
	picoToken, parseErr := extractYAMLPicoToken(string(ymlRaw))
	if parseErr != nil {
		return "", fmt.Errorf("cannot parse pico token from %s: %w", ymlFile, parseErr)
	}

	return fmt.Sprintf("pico-%s%s", pidToken, picoToken), nil
}

// extractYAMLPicoToken is a minimal parser for the .security.yml file.
// It finds the 'pico:' section and extracts its 'token:' value.
// Handles both the top-level 'channels:' wrapper and a bare 'pico:' block.
func extractYAMLPicoToken(yml string) (string, error) {
	lines := strings.Split(yml, "\n")
	inChannels := false
	inPico := false
	for _, raw := range lines {
		line := strings.TrimRight(raw, "\r")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		// Detect top-level 'channels:' block (no leading spaces)
		if !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t") {
			inChannels = strings.TrimSuffix(trimmed, ":") == "channels"
			if !inChannels {
				inPico = false
			}
			continue
		}
		// Inside 'channels:' — look for '  pico:' (one indent level)
		if inChannels {
			indent := len(raw) - len(strings.TrimLeft(raw, " \t"))
			if indent <= 2 {
				key := strings.TrimSuffix(trimmed, ":")
				inPico = (key == "pico")
				continue
			}
		}
		// Inside 'pico:' block — look for 'token:'
		if inPico && strings.HasPrefix(trimmed, "token:") {
			val := strings.TrimSpace(strings.TrimPrefix(trimmed, "token:"))
			if val != "" {
				return val, nil
			}
		}
	}
	return "", fmt.Errorf("channels.pico.token not found in YAML")
}

func (p *pcAuth) setup() error {
	if p.direct {
		if p.token == "" {
			// 1. try ~/.clawproxy/pc_token (previously entered)
			if saved := loadToken("pc_token"); saved != "" {
				p.token = saved
				fmt.Printf("%spicoclaw token loaded from ~/.clawproxy/pc_token\n", prefixPC())
			} else if tok, err := readPicoTokenFromConfig(); err == nil {
				// 2. try /var/lib/picoclaw/.picoclaw/{.picoclaw.pid,.security.yml}
				p.token = tok
				fmt.Printf("%spicoclaw token assembled from runtime files (pico-<pid><token>)\n", prefixPC())
			} else {
				// 3. prompt interactively
				fmt.Printf("%sToken not found in runtime files: %v\n", prefixPC(), err)
				fmt.Printf("%sExpected: /var/lib/picoclaw/.picoclaw/.picoclaw.pid + .security.yml\n", prefixPC())
				fmt.Print(prefixPC() + "Picoclaw token: ")
				fmt.Scanln(&p.token)
				p.token = strings.TrimSpace(p.token)
				if p.token == "" {
					return fmt.Errorf("picoclaw token is required")
				}
				saveToken("pc_token", p.token)
				fmt.Printf("%spicoclaw token saved to ~/.clawproxy/pc_token\n", prefixPC())
			}
		}
		p.wsURL = fmt.Sprintf("ws://%s:%d/pico/ws", p.host, p.gwPort)
		fmt.Printf("%spicoclaw direct %s\n", prefixPC(), p.wsURL)
		return nil
	}
	base := fmt.Sprintf("http://%s:%d", p.host, p.webPort)
	data, err := httpGet(base + "/api/pico/token")
	if err != nil {
		return fmt.Errorf("cannot reach picoclaw-web at %s: %w\n  Use --pc-direct (or set --pc-token)", base, err)
	}
	tok, _ := data["token"].(string)
	wsURL, _ := data["ws_url"].(string)
	enabled, _ := data["enabled"].(bool)
	if tok == "" || wsURL == "" {
		return fmt.Errorf("invalid /api/pico/token response: %v", data)
	}
	p.token = tok
	p.wsURL = wsURL
	fmt.Printf("%spicoclaw web-backend %s  enabled=%v\n", prefixPC(), base, enabled)
	return nil
}

// ── Protocol types ────────────────────────────────────────────────────────────

type zcMsg struct {
Type         string          `json:"type"`
Content      string          `json:"content,omitempty"`
FullResponse string          `json:"full_response,omitempty"`
SessionID    string          `json:"session_id,omitempty"`
Name         string          `json:"name,omitempty"`
Args         json.RawMessage `json:"args,omitempty"`
Output       string          `json:"output,omitempty"`
Message      string          `json:"message,omitempty"`
Resumed      bool            `json:"resumed,omitempty"`
MessageCount int             `json:"message_count,omitempty"`
}

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
wsURL     string
token     string
sessionID string

mu        sync.Mutex
conn      *websocket.Conn
connected atomic.Bool

lastDialHTTPStatus int // HTTP status from last failed dial (0 = none)
zcBuf strings.Builder // CLI mode: accumulate ZC stream chunks
recv  chan []byte      // proxy mode: raw gateway messages (nil = CLI mode)
stop  chan struct{}    // close to stop reconnectLoop
}

func (a *agentConn) dial() error {
header := http.Header{}
var dialer *websocket.Dialer
if a.kind == kindPC && a.token != "" {
header.Set("Authorization", "Bearer "+a.token)
dialer = &websocket.Dialer{
HandshakeTimeout: 10 * time.Second,
Subprotocols:     []string{"token." + a.token},
}
} else {
if a.token != "" {
header.Set("Authorization", "Bearer "+a.token)
}
dialer = &websocket.Dialer{
HandshakeTimeout: 10 * time.Second,
Subprotocols:     []string{"zeroclaw.v1"},
}
}
conn, resp, err := dialer.Dial(a.wsURL, header)
if resp != nil {
a.lastDialHTTPStatus = resp.StatusCode
if resp.Body != nil {
resp.Body.Close()
}
}
if err != nil {
if resp != nil {
return fmt.Errorf("%w (HTTP %d)", err, resp.StatusCode)
}
return err
}
a.mu.Lock()
a.conn = conn
a.mu.Unlock()
a.connected.Store(true)
go a.readLoop()
return nil
}

func (a *agentConn) isStopped() bool {
if a.stop == nil {
return false
}
select {
case <-a.stop:
return true
default:
return false
}
}

func (a *agentConn) sleepOrStop(d time.Duration) bool {
if a.stop == nil {
time.Sleep(d)
return false
}
select {
case <-a.stop:
return true
case <-time.After(d):
return false
}
}

func (a *agentConn) reconnectLoop() {
for {
if a.isStopped() {
return
}
if !a.connected.Load() {
fmt.Printf("%s%s reconnecting...\n", prefixSYS(), a.kind)
if err := a.dial(); err != nil {
fmt.Printf("%s%s connect failed: %v — retry in 5s\n", prefixERR(), a.kind, err)
if a.sleepOrStop(5 * time.Second) {
return
}
continue
}
fmt.Printf("%s%s reconnected\n", prefixSYS(), a.kind)
}
if a.sleepOrStop(2 * time.Second) {
return
}
}
}

func (a *agentConn) close() {
if a.stop != nil {
select {
case <-a.stop:
default:
close(a.stop)
}
}
a.mu.Lock()
if a.conn != nil {
a.conn.Close()
a.conn = nil
}
a.mu.Unlock()
}

func (a *agentConn) readLoop() {
defer func() {
a.connected.Store(false)
a.mu.Lock()
if a.conn != nil {
a.conn.Close()
a.conn = nil
}
a.mu.Unlock()
if a.recv == nil {
fmt.Printf("%s%s disconnected\n", prefixSYS(), a.kind)
}
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
if a.recv != nil {
// proxy mode: route raw bytes to drain goroutine
select {
case a.recv <- raw:
default: // drop if full (backpressure)
}
} else if a.kind == kindZC {
a.handleZC(raw)
} else {
a.handlePC(raw)
}
}
}

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
fmt.Printf("\n%s%stool_call%s %s  args=%s\n", prefixZC(), colGrey, colReset, msg.Name, msg.Args)
case "tool_result":
fmt.Printf("%s%stool_result%s %s  output=%s\n", prefixZC(), colGrey, colReset, msg.Name, truncate(msg.Output, 120))
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

func (a *agentConn) handlePC(raw []byte) {
var msg picoMsg
if err := json.Unmarshal(raw, &msg); err != nil {
fmt.Printf("%sbad json: %s\n", prefixERR(), raw)
return
}
switch msg.Type {
case "pong":
case "message.create":
content, _ := msg.Payload["content"].(string)
fmt.Printf("%s%s\n", prefixPC(), content)
case "message.update":
content, _ := msg.Payload["content"].(string)
fmt.Printf("%s%s(update)%s %s\n", prefixPC(), colGrey, colReset, content)
case "typing.start":
fmt.Printf("%s%s(typing...)%s\n", prefixPC(), colGrey, colReset)
case "typing.stop":
default:
fmt.Printf("%s%s[%s]%s %s\n", prefixPC(), colGrey, msg.Type, colReset, raw)
}
}

func (a *agentConn) sendZC(content string) error {
if !a.connected.Load() {
return fmt.Errorf("zeroclaw not connected")
}
a.mu.Lock()
defer a.mu.Unlock()
return a.conn.WriteJSON(map[string]any{"type": "message", "content": content})
}

func (a *agentConn) sendPC(content string) error {
return a.sendPCWithSession(a.sessionID, content)
}

func (a *agentConn) sendPCWithSession(sessionID, content string) error {
if !a.connected.Load() {
return fmt.Errorf("picoclaw not connected")
}
a.mu.Lock()
defer a.mu.Unlock()
return a.conn.WriteJSON(newPicoSend(sessionID, content))
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

func appendSessionID(wsURL, sessionID string) string {
if strings.Contains(wsURL, "session_id=") {
return wsURL
}
sep := "?"
if strings.Contains(wsURL, "?") {
sep = "&"
}
return wsURL + sep + "session_id=" + sessionID
}

// ── Main ──────────────────────────────────────────────────────────────────────

func main() {
// zeroclaw flags
zcHost     := flag.String("zc-host",      "127.0.0.1", "zeroclaw host")
zcPort     := flag.Int("zc-port",         42617,        "zeroclaw gateway port")
zcToken    := flag.String("zc-token",     "",           "zeroclaw bearer token (skips pairing)")
zcPairCode := flag.String("zc-pair-code", "",           "zeroclaw pair code (non-interactive)")
zcSID      := flag.String("zc-sid",       "",           "zeroclaw session ID (CLI mode)")
zcEnable   := flag.Bool("zc",             false,        "enable zeroclaw")

// picoclaw flags
pcHost    := flag.String("pc-host",    "127.0.0.1", "picoclaw host")
pcPort    := flag.Int("pc-port",       18800,        "picoclaw web-backend port")
pcGWPort  := flag.Int("pc-gw-port",    18790,        "picoclaw gateway port")
	pcDirect  := flag.Bool("pc-direct",   true,         "connect directly to gateway (default; skips web-backend)")
	pcToken   := flag.String("pc-token",  "",           "picoclaw token (default: read from ~/.picoclaw/config.json)")
pcSID     := flag.String("pc-sid",    "",           "picoclaw session ID (CLI mode)")
pcEnable  := flag.Bool("pc",          false,        "enable picoclaw")

// mode
active    := flag.String("active",     "zc",   "default agent in CLI mode: zc|pc")
proxyMode := flag.Bool("proxy",        false,  "run as proxy server (v2)")
proxyPort  := flag.Int("proxy-port",   18780, "proxy server listen port")
	queueDepth := flag.Int("queue-depth",  256,   "offline queue depth per session (0 = disabled)")

flag.Parse()

// auto-enable based on supplied flags
if *zcToken != "" || *zcPairCode != "" {
*zcEnable = true
}
if *pcToken != "" {
*pcEnable = true
}
if !*zcEnable && !*pcEnable {
*zcEnable = true
*pcEnable = true
}

// default session IDs (CLI mode)
if *zcSID == "" {
*zcSID = fmt.Sprintf("clawproxy-zc-%d", time.Now().UnixMilli())
}
if *pcSID == "" {
*pcSID = fmt.Sprintf("clawproxy-pc-%d", time.Now().UnixMilli())
}

// ── auth (shared between CLI and proxy mode) ──────────────────────
var zca *zcAuth
if *zcEnable {
a := &zcAuth{host: *zcHost, port: *zcPort, token: *zcToken, pairCode: *zcPairCode}
if err := a.setup(); err != nil {
fmt.Printf("%s%v\n", prefixERR(), err)
} else {
zca = a
}
}

var pca *pcAuth
if *pcEnable {
a := &pcAuth{
host:    *pcHost,
webPort: *pcPort,
gwPort:  *pcGWPort,
direct:  *pcDirect,
token:   *pcToken,
}
if err := a.setup(); err != nil {
fmt.Printf("%s%v\n", prefixERR(), err)
} else {
pca = a
}
}

if zca == nil && pca == nil {
fmt.Fprintln(os.Stderr, "Error: no agents configured.")
os.Exit(1)
}

// ── proxy mode ────────────────────────────────────────────────────
if *proxyMode {
		runProxy(*proxyPort, zca, pca, *queueDepth)
}

// ── CLI mode: connect ─────────────────────────────────────────────
activeAgent := agentKind(*active)
if activeAgent != kindZC && activeAgent != kindPC {
fmt.Fprintf(os.Stderr, "Error: --active must be 'zc' or 'pc'\n")
os.Exit(1)
}

var zc *agentConn
if zca != nil {
zc = &agentConn{kind: kindZC, wsURL: zca.wsURL(*zcSID), token: zca.token, sessionID: *zcSID}
fmt.Printf("%sConnecting: %s\n", prefixZC(), zc.wsURL)
if err := zc.dial(); err != nil {
fmt.Printf("%szeroclaw connect failed: %v\n", prefixERR(), err)
zc = nil
} else {
go zc.reconnectLoop()
}
}

var pc *agentConn
if pca != nil {
wsURL := appendSessionID(pca.wsURL, *pcSID)
pc = &agentConn{kind: kindPC, wsURL: wsURL, token: pca.token, sessionID: *pcSID}
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

if zc == nil && pc == nil {
fmt.Fprintln(os.Stderr, "Error: no agents connected.")
os.Exit(1)
}

// ── CLI banner ────────────────────────────────────────────────────
fmt.Printf("\n%s%sClawProxy v1%s — CLI mode\n", prefixSYS(), colBold, colReset)
fmt.Printf("%s  %s@zc <text>%s   send to zeroclaw\n", prefixSYS(), colCyan, colReset)
fmt.Printf("%s  %s@pc <text>%s   send to picoclaw\n", prefixSYS(), colGreen, colReset)
fmt.Printf("%s  /switch zc|pc  change active agent (current: %s%s%s)\n",
prefixSYS(), colYellow, activeAgent, colReset)
fmt.Printf("%s  /status        connection status\n", prefixSYS())
fmt.Printf("%s  /quit          exit\n\n", prefixSYS())

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

switch {
case line == "/quit" || line == "/exit":
fmt.Println(prefixSYS() + "bye!")
return
case line == "/status":
printStatus(zc, pc, activeAgent)
case strings.HasPrefix(line, "/switch "):
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
fmt.Printf("%sunknown agent %q\n", prefixERR(), target)
}
default:
var target agentKind
var content string
switch {
case strings.HasPrefix(line, "@zc "):
target = kindZC
content = strings.TrimPrefix(line, "@zc ")
case strings.HasPrefix(line, "@pc "):
target = kindPC
content = strings.TrimPrefix(line, "@pc ")
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
