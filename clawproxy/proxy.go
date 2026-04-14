package main

// proxy.go — v2 proxy server mode for clawproxy.
//
// Architecture:
//
//   app (dashboard / any WS client)
//         │  ws://localhost:18780/proxy/ws   (Pico Protocol + "agent" field)
//         ▼
//   proxyServer
//         │  one proxyClient per app connection
//         ▼
//   proxyClient
//     ├── zcConns map[sessionID → agentConn]  (one ZC conn per isolated session)
//     └── pcConn  *agentConn                  (one shared PC conn, demux by session_id)
//
// Wire protocol (app ↔ clawproxy):
//
//   App sends:
//     {"type":"message.send","agent":"zc"|"pc","session_id":"...","payload":{"content":"..."}}
//     {"type":"ping","id":"..."}
//     {"type":"status"}
//
//   Proxy sends back (all gateway messages forwarded with "agent" field injected):
//     {"type":"chunk",         "agent":"zc","session_id":"...","payload":{"content":"..."}}
//     {"type":"done",          "agent":"zc","session_id":"..."}
//     {"type":"tool_call",     "agent":"zc","session_id":"...","payload":{...}}
//     {"type":"tool_result",   "agent":"zc","session_id":"...","payload":{...}}
//     {"type":"session_start", "agent":"zc","session_id":"...","payload":{...}}
//     {"type":"message.create","agent":"pc","session_id":"...","payload":{"content":"..."}}
//     {"type":"typing.start",  "agent":"pc","session_id":"..."}
//     {"type":"error",         "agent":"zc"|"pc","session_id":"...","message":"..."}
//     {"type":"agent_status",  "agent":"zc"|"pc","status":"connected"|"disconnected"}
//     {"type":"pong","id":"..."}
//     {"type":"status","agents":{"zc":"available","pc":"unavailable"}}

import (
"encoding/json"
"fmt"
"net/http"
"strings"
"sync"
"time"

"github.com/gorilla/websocket"
)

// appMsg is a message from an app client to clawproxy.
type appMsg struct {
Type      string         `json:"type"`
Agent     string         `json:"agent,omitempty"`
SessionID string         `json:"session_id,omitempty"`
ID        string         `json:"id,omitempty"`
Timestamp int64          `json:"timestamp,omitempty"`
Payload   map[string]any `json:"payload,omitempty"`
}

// ── Proxy server ──────────────────────────────────────────────────────────────

type proxyServer struct {
zcAuth   *zcAuth // nil if ZC not configured
pcAuth   *pcAuth // nil if PC not configured
upgrader websocket.Upgrader
}

func newProxyServer(zca *zcAuth, pca *pcAuth) *proxyServer {
return &proxyServer{
zcAuth: zca,
pcAuth: pca,
upgrader: websocket.Upgrader{
CheckOrigin: func(r *http.Request) bool { return true },
},
}
}

func runProxy(port int, zca *zcAuth, pca *pcAuth) {
s := newProxyServer(zca, pca)
mux := http.NewServeMux()
mux.HandleFunc("/proxy/ws", s.handleWS)
mux.HandleFunc("/proxy/status", s.handleStatus)
addr := fmt.Sprintf(":%d", port)
fmt.Printf("\n%s%sClawProxy v2%s — proxy mode\n", prefixSYS(), colBold, colReset)
fmt.Printf("%s  ws://localhost%s/proxy/ws\n", prefixSYS(), addr)
fmt.Printf("%s  http://localhost%s/proxy/status\n", prefixSYS(), addr)
zcOK := "✓"
if zca == nil {
zcOK = "✗ unavailable"
}
pcOK := "✓"
if pca == nil {
pcOK = "✗ unavailable"
}
fmt.Printf("%s  ZC: %s   PC: %s\n\n", prefixSYS(), zcOK, pcOK)
if err := http.ListenAndServe(addr, mux); err != nil {
fmt.Printf("%sproxy server: %v\n", prefixERR(), err)
}
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

// ── Proxy client ──────────────────────────────────────────────────────────────

type proxyClient struct {
id     string
conn   *websocket.Conn
mu     sync.Mutex // protects conn writes
server *proxyServer
done   chan struct{}

// ZC: one upstream connection per session_id (lazy-created)
zcMu    sync.RWMutex
zcConns map[string]*agentConn

// PC: one shared upstream connection per app client
// (PC demuxes responses by session_id embedded in each message)
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

// run is the main read loop for an app client. Blocks until client disconnects.
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

// ── ZC upstream: one connection per session_id ────────────────────────────────

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
kind:      kindZC,
wsURL:     zca.wsURL(sessionID),
token:     zca.token,
sessionID: sessionID,
recv:      recv,
stop:      stop,
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

// drainZC relays raw ZC bytes to the app, injecting agent + session_id.
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

// ── PC upstream: one shared connection per app client ────────────────────────

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
kind:      kindPC,
wsURL:     wsURL,
token:     pca.token,
sessionID: sid,
recv:      recv,
stop:      stop,
}
if err := conn.dial(); err != nil {
return nil, fmt.Errorf("picoclaw connect: %w", err)
}
c.pcConn = conn
go conn.reconnectLoop()
go c.drainPC(recv, stop)
go func() {
// keepalive ping every 30s
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

// drainPC relays raw PC bytes to the app.
// PC messages already contain session_id so we just inject the agent tag.
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

// ── Wire helpers ──────────────────────────────────────────────────────────────

// relayRaw injects "agent" and "session_id" into a raw gateway message and sends it to the app.
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
"type":       "error",
"agent":      agent,
"session_id": sessionID,
"message":    message,
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
"type": "status",
"agents": map[string]string{
"zc": zc,
"pc": pc,
},
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

// Ensure gorilla/websocket is used in this file (suppress unused import if needed).
var _ = websocket.TextMessage

// Ensure strings is used.
var _ = strings.Contains
