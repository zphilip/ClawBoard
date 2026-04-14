package main

// proxy.go — v2 proxy server mode for clawproxy.
//
// ── Two endpoint families ─────────────────────────────────────────────────────
//
// 1. COMPAT endpoints — existing clients connect unchanged:
//
//    chat.py (zeroclaw client):
//      GET  /health                  → {"require_pairing":false,"paired":true}
//      POST /pair                    → {"token":"<internal>"}  (no-op, any code)
//      WS   /ws/chat?session_id=...  → raw relay → zeroclaw :42617
//
//    chat_picoclaw.py (picoclaw client):
//      GET  /api/pico/token          → {"token":"<internal>","ws_url":"ws://.../pico/ws","enabled":true}
//      WS   /pico/ws?session_id=...  → raw relay → picoclaw :18790
//
// 2. PROXY endpoint — multi-agent unified protocol:
//
//    WS   /proxy/ws                  → Pico Protocol + "agent":"zc"|"pc" field
//    GET  /proxy/status
//
// ── Architecture ─────────────────────────────────────────────────────────────
//
//   app / chat.py / chat_picoclaw.py
//         │
//   clawproxy :18780
//     ├── /ws/chat          → raw relay → zeroclaw  :42617  (zeroclaw.v1)
//     ├── /pico/ws          → raw relay → picoclaw  :18790  (Pico Protocol)
//     └── /proxy/ws         → unified proxy (per-session ZC + shared PC)

import (
"encoding/json"
"fmt"
"net/http"
"strings"
"sync"
"time"

"github.com/gorilla/websocket"
)

// ── Proxy server ──────────────────────────────────────────────────────────────

type proxyServer struct {
zcAuth        *zcAuth
pcAuth        *pcAuth
port          int
internalToken string // stable token for compat endpoints
upgrader      websocket.Upgrader
}

func newProxyServer(zca *zcAuth, pca *pcAuth, port int) *proxyServer {
return &proxyServer{
zcAuth:        zca,
pcAuth:        pca,
port:          port,
internalToken: fmt.Sprintf("clawproxy-%d", time.Now().UnixMilli()),
upgrader: websocket.Upgrader{
CheckOrigin: func(r *http.Request) bool { return true },
},
}
}

func runProxy(port int, zca *zcAuth, pca *pcAuth) {
s := newProxyServer(zca, pca, port)
mux := http.NewServeMux()

// ── compat: zeroclaw (chat.py) ──────────────────────────────────
mux.HandleFunc("/health",    s.handleHealth)
mux.HandleFunc("/pair",      s.handlePair)
mux.HandleFunc("/ws/chat",   s.handleZCCompat)

// ── compat: picoclaw (chat_picoclaw.py) ────────────────────────
mux.HandleFunc("/api/pico/token", s.handlePicoToken)
mux.HandleFunc("/pico/ws",        s.handlePCCompat)

// ── unified proxy endpoint ──────────────────────────────────────
mux.HandleFunc("/proxy/ws",     s.handleWS)
mux.HandleFunc("/proxy/status", s.handleStatus)

addr := fmt.Sprintf(":%d", port)
fmt.Printf("\n%s%sClawProxy v2%s — proxy mode\n", prefixSYS(), colBold, colReset)
fmt.Printf("%s  ZC compat  ←  GET /health · POST /pair · WS /ws/chat\n", prefixSYS())
fmt.Printf("%s  PC compat  ←  GET /api/pico/token · WS /pico/ws\n", prefixSYS())
fmt.Printf("%s  Unified    ←  WS /proxy/ws\n", prefixSYS())
fmt.Printf("%s  Status     ←  GET /proxy/status\n", prefixSYS())
zcOK := colGreen + "✓" + colReset
if zca == nil {
zcOK = colRed + "✗ unavailable" + colReset
}
pcOK := colGreen + "✓" + colReset
if pca == nil {
pcOK = colRed + "✗ unavailable" + colReset
}
fmt.Printf("%s  ZC: %s   PC: %s   listen: %s\n\n", prefixSYS(), zcOK, pcOK, addr)
if err := http.ListenAndServe(addr, mux); err != nil {
fmt.Printf("%sproxy server: %v\n", prefixERR(), err)
}
}

// ── Compat: zeroclaw HTTP endpoints ──────────────────────────────────────────

// GET /health — chat.py checks this before pairing.
// The proxy handles real ZC auth at startup so we always report paired=true.
func (s *proxyServer) handleHealth(w http.ResponseWriter, r *http.Request) {
w.Header().Set("Content-Type", "application/json")
json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
"require_pairing": false,
"paired":          true,
})
}

// POST /pair — chat.py exchanges a pairing code for a token.
// The proxy already holds the real ZC token; return the internal proxy token.
func (s *proxyServer) handlePair(w http.ResponseWriter, r *http.Request) {
w.Header().Set("Content-Type", "application/json")
json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
"token": s.internalToken,
})
}

// ── Compat: picoclaw HTTP endpoint ────────────────────────────────────────────

// GET /api/pico/token — chat_picoclaw.py (mode 1) fetches token + ws_url here.
func (s *proxyServer) handlePicoToken(w http.ResponseWriter, r *http.Request) {
w.Header().Set("Content-Type", "application/json")
// Build ws_url pointing back to this proxy's /pico/ws endpoint.
// Use the Host header so LAN clients get the right address.
host := r.Host
if host == "" {
host = fmt.Sprintf("127.0.0.1:%d", s.port)
}
wsURL := fmt.Sprintf("ws://%s/pico/ws", host)
json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
"token":   s.internalToken,
"ws_url":  wsURL,
"enabled": s.pcAuth != nil,
})
}

// ── Compat: zeroclaw WS relay (/ws/chat) ─────────────────────────────────────

// WS /ws/chat — raw bidirectional relay to zeroclaw upstream.
// chat.py connects here with subprotocol zeroclaw.v1; messages pass through unchanged.
func (s *proxyServer) handleZCCompat(w http.ResponseWriter, r *http.Request) {
if s.zcAuth == nil {
http.Error(w, `{"type":"error","message":"zeroclaw not configured"}`, http.StatusServiceUnavailable)
return
}

// Upgrade app connection — negotiate zeroclaw.v1 subprotocol.
upg := websocket.Upgrader{
CheckOrigin:  func(r *http.Request) bool { return true },
Subprotocols: []string{"zeroclaw.v1"},
}
appConn, err := upg.Upgrade(w, r, nil)
if err != nil {
fmt.Printf("%sZC compat upgrade: %v\n", prefixERR(), err)
return
}

sid := r.URL.Query().Get("session_id")
if sid == "" {
sid = fmt.Sprintf("compat-zc-%d", time.Now().UnixMilli())
}

// Connect to real ZC upstream.
upURL := s.zcAuth.wsURL(sid)
upHeader := http.Header{}
if s.zcAuth.token != "" {
upHeader.Set("Authorization", "Bearer "+s.zcAuth.token)
}
upDialer := &websocket.Dialer{
HandshakeTimeout: 10 * time.Second,
Subprotocols:     []string{"zeroclaw.v1"},
}
upConn, resp, err := upDialer.Dial(upURL, upHeader)
if resp != nil && resp.Body != nil {
resp.Body.Close()
}
if err != nil {
appConn.WriteMessage(websocket.TextMessage, //nolint:errcheck
[]byte(`{"type":"error","message":"zeroclaw upstream unavailable"}`))
appConn.Close()
return
}

fmt.Printf("%sZC compat relay  sid=%s  remote=%s\n", prefixSYS(), sid, r.RemoteAddr)
rawRelay(appConn, upConn)
fmt.Printf("%sZC compat closed sid=%s\n", prefixSYS(), sid)
}

// ── Compat: picoclaw WS relay (/pico/ws) ─────────────────────────────────────

// WS /pico/ws — raw bidirectional relay to picoclaw upstream.
// chat_picoclaw.py connects here with Authorization: Bearer + token.* subprotocol.
// The proxy accepts any non-empty token (auth is handled upstream).
func (s *proxyServer) handlePCCompat(w http.ResponseWriter, r *http.Request) {
if s.pcAuth == nil {
http.Error(w, `{"type":"error","message":"picoclaw not configured"}`, http.StatusServiceUnavailable)
return
}

// Echo back any token.* subprotocol the client requests.
var subprotos []string
for _, p := range websocket.Subprotocols(r) {
if strings.HasPrefix(p, "token.") {
subprotos = []string{p}
break
}
}
upg := websocket.Upgrader{
CheckOrigin:  func(r *http.Request) bool { return true },
Subprotocols: subprotos,
}
appConn, err := upg.Upgrade(w, r, nil)
if err != nil {
fmt.Printf("%sPC compat upgrade: %v\n", prefixERR(), err)
return
}

sid := r.URL.Query().Get("session_id")
if sid == "" {
sid = fmt.Sprintf("compat-pc-%d", time.Now().UnixMilli())
}

// Connect to real PC upstream.
pcWsURL := appendSessionID(s.pcAuth.wsURL, sid)
upHeader := http.Header{}
upHeader.Set("Authorization", "Bearer "+s.pcAuth.token)
upDialer := &websocket.Dialer{
HandshakeTimeout: 10 * time.Second,
Subprotocols:     []string{"token." + s.pcAuth.token},
}
upConn, resp, err := upDialer.Dial(pcWsURL, upHeader)
if resp != nil && resp.Body != nil {
resp.Body.Close()
}
if err != nil {
appConn.WriteMessage(websocket.TextMessage, //nolint:errcheck
[]byte(`{"type":"error","message":"picoclaw upstream unavailable"}`))
appConn.Close()
return
}

fmt.Printf("%sPC compat relay  sid=%s  remote=%s\n", prefixSYS(), sid, r.RemoteAddr)
rawRelay(appConn, upConn)
fmt.Printf("%sPC compat closed sid=%s\n", prefixSYS(), sid)
}

// ── Raw bidirectional relay ───────────────────────────────────────────────────

// rawRelay copies frames between two WebSocket connections in both directions.
// Blocks until either side closes.
func rawRelay(a, b *websocket.Conn) {
var once sync.Once
done := make(chan struct{})

relay := func(src, dst *websocket.Conn) {
defer func() {
once.Do(func() { close(done) })
src.Close()
dst.Close()
}()
for {
msgType, data, err := src.ReadMessage()
if err != nil {
return
}
if err := dst.WriteMessage(msgType, data); err != nil {
return
}
}
}

go relay(a, b)
go relay(b, a)
<-done
}

// ── Unified /proxy/ws endpoint ────────────────────────────────────────────────

// appMsg is a message from an app client to clawproxy.
type appMsg struct {
Type      string         `json:"type"`
Agent     string         `json:"agent,omitempty"`
SessionID string         `json:"session_id,omitempty"`
ID        string         `json:"id,omitempty"`
Timestamp int64          `json:"timestamp,omitempty"`
Payload   map[string]any `json:"payload,omitempty"`
}

func (s *proxyServer) handleWS(w http.ResponseWriter, r *http.Request) {
conn, err := s.upgrader.Upgrade(w, r, nil)
if err != nil {
fmt.Printf("%supgrade: %v\n", prefixERR(), err)
return
}
c := newProxyClient(conn, s)
fmt.Printf("%sapp connected    id=%s  remote=%s\n", prefixSYS(), c.id, r.RemoteAddr)
c.run()
fmt.Printf("%sapp disconnected id=%s\n", prefixSYS(), c.id)
}

func (s *proxyServer) handleStatus(w http.ResponseWriter, r *http.Request) {
w.Header().Set("Content-Type", "application/json")
json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
"version": "2",
"agents": map[string]any{
"zc": map[string]any{"configured": s.zcAuth != nil},
"pc": map[string]any{"configured": s.pcAuth != nil},
},
})
}

// ── Proxy client (unified /proxy/ws) ─────────────────────────────────────────

type proxyClient struct {
id     string
conn   *websocket.Conn
mu     sync.Mutex
server *proxyServer
done   chan struct{}

zcMu    sync.RWMutex
zcConns map[string]*agentConn

pcMu   sync.Mutex
pcConn *agentConn
}

func newProxyClient(conn *websocket.Conn, s *proxyServer) *proxyClient {
return &proxyClient{
id:      fmt.Sprintf("c%d", time.Now().UnixNano()%1_000_000_000),
conn:    conn,
server:  s,
done:    make(chan struct{}),
zcConns: make(map[string]*agentConn),
}
}

func (c *proxyClient) run() {
defer func() {
close(c.done)
c.closeAll()
}()
for {
_, raw, err := c.conn.ReadMessage()
if err != nil {
if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
fmt.Printf("%sclient %s: %v\n", prefixERR(), c.id, err)
}
return
}
var msg appMsg
if err := json.Unmarshal(raw, &msg); err != nil {
c.sendError("", "", "invalid JSON: "+err.Error())
continue
}
c.dispatch(msg)
}
}

func (c *proxyClient) dispatch(msg appMsg) {
switch msg.Type {
case "ping":
c.sendRaw(map[string]any{"type": "pong", "id": msg.ID})
case "message.send":
if msg.SessionID == "" {
msg.SessionID = fmt.Sprintf("sess-%d", time.Now().UnixMilli())
}
switch agentKind(msg.Agent) {
case kindZC:
c.sendToZC(msg)
case kindPC:
c.sendToPC(msg)
default:
c.sendError(msg.Agent, msg.SessionID, `agent must be "zc" or "pc"`)
}
case "status":
c.sendStatus()
default:
c.sendError(msg.Agent, msg.SessionID, "unknown message type: "+msg.Type)
}
}

func (c *proxyClient) getOrCreateZC(sessionID string) (*agentConn, error) {
c.zcMu.RLock()
conn := c.zcConns[sessionID]
c.zcMu.RUnlock()
if conn != nil {
return conn, nil
}
zca := c.server.zcAuth
if zca == nil {
return nil, fmt.Errorf("zeroclaw not configured on this proxy")
}
recv := make(chan []byte, 128)
stop := make(chan struct{})
conn = &agentConn{
kind: kindZC, wsURL: zca.wsURL(sessionID), token: zca.token,
sessionID: sessionID, recv: recv, stop: stop,
}
if err := conn.dial(); err != nil {
return nil, fmt.Errorf("zeroclaw connect: %w", err)
}
go conn.reconnectLoop()
go c.drainZC(sessionID, recv, stop)
c.zcMu.Lock()
c.zcConns[sessionID] = conn
c.zcMu.Unlock()
fmt.Printf("%sclient %s: ZC session=%s\n", prefixSYS(), c.id, sessionID)
return conn, nil
}

func (c *proxyClient) sendToZC(msg appMsg) {
conn, err := c.getOrCreateZC(msg.SessionID)
if err != nil {
c.sendError("zc", msg.SessionID, err.Error())
return
}
content, _ := msg.Payload["content"].(string)
if err := conn.sendZC(content); err != nil {
c.sendError("zc", msg.SessionID, err.Error())
}
}

func (c *proxyClient) drainZC(sessionID string, recv chan []byte, stop chan struct{}) {
for {
select {
case <-c.done:
return
case <-stop:
return
case raw, ok := <-recv:
if !ok {
return
}
c.relayRaw("zc", sessionID, raw)
}
}
}

func (c *proxyClient) getOrCreatePC() (*agentConn, error) {
c.pcMu.Lock()
defer c.pcMu.Unlock()
if c.pcConn != nil {
return c.pcConn, nil
}
pca := c.server.pcAuth
if pca == nil {
return nil, fmt.Errorf("picoclaw not configured on this proxy")
}
sid := "proxy-" + c.id
wsURL := appendSessionID(pca.wsURL, sid)
recv := make(chan []byte, 128)
stop := make(chan struct{})
conn := &agentConn{
kind: kindPC, wsURL: wsURL, token: pca.token,
sessionID: sid, recv: recv, stop: stop,
}
if err := conn.dial(); err != nil {
return nil, fmt.Errorf("picoclaw connect: %w", err)
}
c.pcConn = conn
go conn.reconnectLoop()
go c.drainPC(recv, stop)
go func() {
t := time.NewTicker(30 * time.Second)
defer t.Stop()
for {
select {
case <-c.done:
return
case <-stop:
return
case <-t.C:
conn.pingPC()
}
}
}()
fmt.Printf("%sclient %s: PC connected\n", prefixSYS(), c.id)
return conn, nil
}

func (c *proxyClient) sendToPC(msg appMsg) {
conn, err := c.getOrCreatePC()
if err != nil {
c.sendError("pc", msg.SessionID, err.Error())
return
}
content, _ := msg.Payload["content"].(string)
if err := conn.sendPCWithSession(msg.SessionID, content); err != nil {
c.sendError("pc", msg.SessionID, err.Error())
}
}

func (c *proxyClient) drainPC(recv chan []byte, stop chan struct{}) {
for {
select {
case <-c.done:
return
case <-stop:
return
case raw, ok := <-recv:
if !ok {
return
}
var peek struct {
SessionID string `json:"session_id"`
}
json.Unmarshal(raw, &peek) //nolint:errcheck
c.relayRaw("pc", peek.SessionID, raw)
}
}
}

func (c *proxyClient) relayRaw(agent, sessionID string, raw []byte) {
var m map[string]any
if err := json.Unmarshal(raw, &m); err != nil {
return
}
m["agent"] = agent
if sessionID != "" {
m["session_id"] = sessionID
}
out, _ := json.Marshal(m)
c.mu.Lock()
defer c.mu.Unlock()
c.conn.WriteMessage(websocket.TextMessage, out) //nolint:errcheck
}

func (c *proxyClient) sendRaw(v any) {
c.mu.Lock()
defer c.mu.Unlock()
c.conn.WriteJSON(v) //nolint:errcheck
}

func (c *proxyClient) sendError(agent, sessionID, message string) {
c.sendRaw(map[string]any{
"type": "error", "agent": agent, "session_id": sessionID, "message": message,
})
}

func (c *proxyClient) sendStatus() {
zc, pc := "unavailable", "unavailable"
if c.server.zcAuth != nil {
zc = "available"
}
if c.server.pcAuth != nil {
pc = "available"
}
c.sendRaw(map[string]any{
"type":   "status",
"agents": map[string]string{"zc": zc, "pc": pc},
})
}

func (c *proxyClient) closeAll() {
c.zcMu.Lock()
for _, conn := range c.zcConns {
conn.close()
}
c.zcMu.Unlock()
c.pcMu.Lock()
if c.pcConn != nil {
c.pcConn.close()
}
c.pcMu.Unlock()
}

var _ = strings.Contains
