package main

// proxy.go — v3 proxy server mode for clawproxy.
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
//    WS   /proxy/ws?client_id=<id>  → Pico Protocol + "agent":"zc"|"pc" field
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
//                             ?client_id=X  — optional; enables offline queue

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
	store         *sessionStore
	pcCompat      *pcCompatStore // persistent PC compat sessions (keyed by app token)
	healthMu      sync.RWMutex
	zcHealth      string // "not_configured"|"unknown"|"online"|"offline"|"auth_error"
	pcHealth      string
}

func newProxyServer(zca *zcAuth, pca *pcAuth, port, maxQueue int) *proxyServer {
zcH, pcH := "not_configured", "not_configured"
if zca != nil {
zcH = "unknown"
}
if pca != nil {
pcH = "unknown"
}
return &proxyServer{
		zcAuth:        zca,
		pcAuth:        pca,
		port:          port,
		internalToken: fmt.Sprintf("clawproxy-%d", time.Now().UnixMilli()),
		store:         newSessionStore(maxQueue),
		pcCompat:      newPCCompatStore(maxQueue),
		zcHealth:      zcH,
		pcHealth:      pcH,
		upgrader: websocket.Upgrader{
			CheckOrigin: func(r *http.Request) bool { return true },
		},
	}
}

// ── Upstream health tracking ──────────────────────────────────────────────────

func (s *proxyServer) setHealth(agent, status string) {
s.healthMu.Lock()
if agent == "zc" {
s.zcHealth = status
} else {
s.pcHealth = status
}
s.healthMu.Unlock()
}

func (s *proxyServer) getHealth() (zc, pc string) {
s.healthMu.RLock()
zc, pc = s.zcHealth, s.pcHealth
s.healthMu.RUnlock()
return
}

// dialProbe does a quick connect-and-close to check if an upstream is reachable.
// Returns "online", "offline", or "auth_error".
func dialProbe(wsURL, token string, subprotos []string) string {
dialer := &websocket.Dialer{
HandshakeTimeout: 5 * time.Second,
Subprotocols:     subprotos,
}
header := http.Header{}
if token != "" {
header.Set("Authorization", "Bearer "+token)
}
conn, resp, err := dialer.Dial(wsURL, header)
if resp != nil && resp.Body != nil {
resp.Body.Close()
}
if err == nil {
conn.Close()
return "online"
}
if resp != nil && (resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden) {
return "auth_error"
}
return "offline"
}

// startHealthProbe runs an initial probe then re-probes every 30 s in the background.
func (s *proxyServer) startHealthProbe() {
go func() {
s.probeAll()
t := time.NewTicker(30 * time.Second)
defer t.Stop()
for range t.C {
s.probeAll()
}
}()
}

func tokenPreview(t string) string {
	if len(t) <= 16 {
		return t
	}
	return t[:8] + "\u2026" + t[len(t)-4:]
}

func (s *proxyServer) probeAll() {
	if s.zcAuth != nil {
		sid := fmt.Sprintf("probe-%d", time.Now().UnixMilli())
		status := dialProbe(s.zcAuth.wsURL(sid), s.zcAuth.token, []string{"zeroclaw.v1"})
		s.setHealth("zc", status)
		fmt.Printf("%sZC probe: %s\n", prefixSYS(), status)
	}
	if s.pcAuth != nil {
		sid := fmt.Sprintf("probe-%d", time.Now().UnixMilli())
		wsURL := appendSessionID(s.pcAuth.wsURL, sid)

		// If we have no token yet, try to read it from the picoclaw runtime files.
		// picoclaw may not have started when clawproxy first launched.
		if s.pcAuth.token == "" {
			if fresh, err := readPicoTokenFromConfig(); err == nil {
				s.pcAuth.token = fresh
				fmt.Printf("%sPC token loaded from runtime files: %s\n", prefixSYS(), tokenPreview(fresh))
			} else {
				fmt.Printf("%sPC token not yet available: %v\n", prefixSYS(), err)
				s.setHealth("pc", "not_configured")
				return
			}
		}
		fmt.Printf("%sPC probe: url=%s  token=%s\n", prefixSYS(), s.pcAuth.wsURL, tokenPreview(s.pcAuth.token))
		status := dialProbe(wsURL, s.pcAuth.token, []string{"token." + s.pcAuth.token})
		if status == "auth_error" {
			// Stale token — wipe the cache and re-assemble from runtime files.
			fmt.Printf("%sPC probe: auth_error — discarding cached token, re-reading from runtime files\n", prefixSYS())
			deleteToken("pc_token")
			if fresh, err := readPicoTokenFromConfig(); err == nil {
				s.pcAuth.token = fresh
				fmt.Printf("%sPC token refreshed: %s\n", prefixSYS(), tokenPreview(fresh))
				// Re-probe with the new token.
				wsURL = appendSessionID(s.pcAuth.wsURL, fmt.Sprintf("probe2-%d", time.Now().UnixMilli()))
				status = dialProbe(wsURL, s.pcAuth.token, []string{"token." + s.pcAuth.token})
				fmt.Printf("%sPC re-probe after refresh: %s\n", prefixSYS(), status)
			} else {
				fmt.Printf("%sPC token refresh failed: %v\n", prefixERR(), err)
			}
		}
		s.setHealth("pc", status)
		fmt.Printf("%sPC probe: %s\n", prefixSYS(), status)
	}
}

func runProxy(port int, zca *zcAuth, pca *pcAuth, maxQueue int) {
s := newProxyServer(zca, pca, port, maxQueue)
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
fmt.Printf("\n%s%sClawProxy v3%s — proxy mode\n", prefixSYS(), colBold, colReset)
fmt.Printf("%s  ZC compat  ←  GET /health · POST /pair · WS /ws/chat\n", prefixSYS())
fmt.Printf("%s  PC compat  ←  GET /api/pico/token · WS /pico/ws\n", prefixSYS())
fmt.Printf("%s  Unified    ←  WS /proxy/ws?client_id=<id>\n", prefixSYS())
fmt.Printf("%s  Status     ←  GET /proxy/status\n", prefixSYS())
zcOK := colGreen + "✓" + colReset
if zca == nil {
zcOK = colRed + "✗ unavailable" + colReset
}
pcOK := colGreen + "✓" + colReset
if pca == nil {
pcOK = colRed + "✗ unavailable" + colReset
}
queueStr := "disabled"
if maxQueue > 0 {
queueStr = fmt.Sprintf("%d msgs/session", maxQueue)
}
fmt.Printf("%s  ZC: %s   PC: %s   queue: %s   listen: %s\n\n",
prefixSYS(), zcOK, pcOK, queueStr, addr)
s.startHealthProbe()
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
// dialPCUpstream dials the picoclaw gateway at wsURL.
// If the dial is rejected with an auth error (HTTP 401/403), it immediately
// refreshes the token from the picoclaw runtime files and retries once.
// This handles the common case where picoclaw restarted with a new session
// token before the 30-second health probe had a chance to pick it up.
func (s *proxyServer) dialPCUpstream(wsURL string) (*websocket.Conn, error) {
	tryDial := func(tok string) (*websocket.Conn, int, error) {
		h := http.Header{}
		h.Set("Authorization", "Bearer "+tok)
		d := &websocket.Dialer{
			HandshakeTimeout: 10 * time.Second,
			Subprotocols:     []string{"token." + tok},
		}
		c, resp, err := d.Dial(wsURL, h)
		status := 0
		if resp != nil {
			status = resp.StatusCode
			resp.Body.Close()
		}
		return c, status, err
	}

	c, status, err := tryDial(s.pcAuth.token)
	if err == nil {
		return c, nil
	}
	// Auth failure — picoclaw may have restarted with a new token.
	// Refresh immediately rather than waiting for the 30s probe cycle.
	if status == 401 || status == 403 {
		fmt.Printf("%sPC dial auth error (HTTP %d) — refreshing token\n", prefixSYS(), status)
		deleteToken("pc_token")
		if fresh, rerr := readPicoTokenFromConfig(); rerr == nil {
			fmt.Printf("%sPC token refreshed: %s\n", prefixSYS(), tokenPreview(fresh))
			s.pcAuth.token = fresh
			if c2, _, err2 := tryDial(fresh); err2 == nil {
				return c2, nil
			} else {
				return nil, err2
			}
		} else {
			fmt.Printf("%sPC token refresh failed: %v\n", prefixERR(), rerr)
		}
	}
	return nil, err
}

func (s *proxyServer) handlePCCompat(w http.ResponseWriter, r *http.Request) {
	if s.pcAuth == nil {
		http.Error(w, `{"type":"error","message":"picoclaw not configured"}`, http.StatusServiceUnavailable)
		return
	}

	// Derive a stable session key from the app's token subprotocol.
	// The same device always sends the same token, so this naturally
	// identifies returning clients across reconnects.
	sessionKey := ""
	var subprotos []string
	for _, p := range websocket.Subprotocols(r) {
		if strings.HasPrefix(p, "token.") {
			sessionKey = strings.TrimPrefix(p, "token.")
			subprotos = []string{p}
			break
		}
	}
	if sessionKey == "" {
		sessionKey = r.URL.Query().Get("proxy_client_id")
	}
	if sessionKey == "" {
		sessionKey = fmt.Sprintf("pc-%d", time.Now().UnixNano())
	}

	// Upgrade app WebSocket; expose the session key so the client can reuse it.
	respHdr := http.Header{"X-Proxy-Client-Id": []string{sessionKey}}
	upg := websocket.Upgrader{
		CheckOrigin:  func(r *http.Request) bool { return true },
		Subprotocols: subprotos,
	}
	appConn, err := upg.Upgrade(w, r, respHdr)
	if err != nil {
		fmt.Printf("%sPC compat upgrade: %v\n", prefixERR(), err)
		return
	}

	keyShort := sessionKey
	if len(keyShort) > 8 {
		keyShort = keyShort[:8]
	}

	// Get or create the persistent session for this app identity.
	cs := s.pcCompat.getOrCreate(
		sessionKey,
		func() string { return s.pcAuth.token },
		func(sid string) string { return appendSessionID(s.pcAuth.wsURL, sid) },
	)
	cs.start() // no-op if already running

	// Attach the new app connection; drain any buffered upstream messages.
	queued := cs.attachApp(appConn)
	fmt.Printf("%sPC compat attach  key=%s  remote=%s  queued=%d\n",
		prefixSYS(), keyShort, r.RemoteAddr, len(queued))
	for _, data := range queued {
		preview := string(data)
		if len(preview) > 80 {
			preview = preview[:80] + "\u2026"
		}
		fmt.Printf("%sPC compat[%s] drain→app (%d B): %s\n", prefixSYS(), keyShort, len(data), preview)
		if err := appConn.WriteMessage(websocket.TextMessage, data); err != nil {
			cs.detachApp()
			appConn.Close()
			return
		}
	}

	// App → upstream relay loop.
	defer func() {
		cs.detachApp()
		appConn.Close()
		fmt.Printf("%sPC compat detach  key=%s\n", prefixSYS(), keyShort)
	}()
	for {
		msgType, data, err := appConn.ReadMessage()
		if err != nil {
			return
		}
		preview := string(data)
		if len(preview) > 120 {
			preview = preview[:120] + "\u2026"
		}
		fmt.Printf("%sPC relay[%s] app\u2192gw (%d B): %s\n", prefixSYS(), keyShort, len(data), preview)
		if werr := cs.sendToUpstream(msgType, data); werr != nil {
			fmt.Printf("%sPC relay[%s] app\u2192gw write-err: %v\n", prefixERR(), keyShort, werr)
		}
	}}
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

// loggedRelay is rawRelay with per-frame debug logging (app→upstream and upstream→app).
// Used by handlePCCompat to trace message flow.
func loggedRelay(sid string, app, up *websocket.Conn) {
	var once sync.Once
	done := make(chan struct{})

	relay := func(src, dst *websocket.Conn, direction string) {
		defer func() {
			once.Do(func() { close(done) })
			src.Close()
			dst.Close()
		}()
		for {
			msgType, data, err := src.ReadMessage()
			if err != nil {
				fmt.Printf("%sPC relay[%s] %s read-err: %v\n", prefixSYS(), sid, direction, err)
				return
			}
			preview := string(data)
			if len(preview) > 120 {
				preview = preview[:120] + "…"
			}
			fmt.Printf("%sPC relay[%s] %s (%d B): %s\n", prefixSYS(), sid, direction, len(data), preview)
			if err := dst.WriteMessage(msgType, data); err != nil {
				fmt.Printf("%sPC relay[%s] %s write-err: %v\n", prefixSYS(), sid, direction, err)
				return
			}
		}
	}

	go relay(app, up, "app→gw")
	go relay(up, app, "gw→app")
	<-done
}

// ── PC compat persistent session ─────────────────────────────────────────────
//
// pcCompatSession keeps a picoclaw upstream connection alive across app
// reconnects.  The session is keyed by the app's token subprotocol value
// (stable across reconnects for the same device), so the proxy can recognise
// a returning client and drain buffered upstream messages before normal relay.

type pcCompatSession struct {
	srv string // upstream wsURL prefix (filled on start)
	key string // session key (app token value)
	maxQ int

	// upstream connection
	upMu   sync.Mutex
	upConn *websocket.Conn

	// offline queue: frames from upstream buffered while app is away
	qMu   sync.Mutex
	queue [][]byte

	// currently attached app (nil = offline)
	appMu sync.RWMutex
	app   *websocket.Conn

	getToken func() string        // reads current token from proxyServer
	getWSURL func(sid string) string // builds upstream wsURL

	startOnce sync.Once
	stopCh    chan struct{}
}

func (cs *pcCompatSession) start() {
	cs.startOnce.Do(func() { go cs.upstreamLoop() })
}

func (cs *pcCompatSession) upstreamLoop() {
	keyShort := cs.key
	if len(keyShort) > 8 {
		keyShort = keyShort[:8]
	}
	for {
		select {
		case <-cs.stopCh:
			return
		default:
		}
		tok := cs.getToken()
		wsURL := cs.getWSURL("pc-compat-" + keyShort)
		h := http.Header{}
		h.Set("Authorization", "Bearer "+tok)
		d := &websocket.Dialer{
			HandshakeTimeout: 10 * time.Second,
			Subprotocols:     []string{"token." + tok},
		}
		conn, resp, err := d.Dial(wsURL, h)
		if resp != nil && resp.Body != nil {
			resp.Body.Close()
		}
		if err != nil {
			fmt.Printf("%sPC compat[%s] upstream dial failed: %v — retry 5s\n", prefixERR(), keyShort, err)
			select {
			case <-cs.stopCh:
				return
			case <-time.After(5 * time.Second):
			}
			continue
		}
		cs.upMu.Lock()
		cs.upConn = conn
		cs.upMu.Unlock()
		fmt.Printf("%sPC compat[%s] upstream connected\n", prefixSYS(), keyShort)
		for {
			_, data, rerr := conn.ReadMessage()
			if rerr != nil {
				fmt.Printf("%sPC compat[%s] upstream read: %v\n", prefixSYS(), keyShort, rerr)
				break
			}
			cs.deliverOrBuffer(data)
		}
		cs.upMu.Lock()
		cs.upConn = nil
		cs.upMu.Unlock()
		conn.Close()
		// Brief pause before reconnect so we don't spin if picoclaw is restarting.
		select {
		case <-cs.stopCh:
			return
		case <-time.After(2 * time.Second):
		}
	}
}

func (cs *pcCompatSession) attachApp(app *websocket.Conn) [][]byte {
	cs.appMu.Lock()
	cs.app = app
	cs.appMu.Unlock()
	cs.qMu.Lock()
	q := append([][]byte(nil), cs.queue...)
	cs.queue = nil
	cs.qMu.Unlock()
	return q
}

func (cs *pcCompatSession) detachApp() {
	cs.appMu.Lock()
	cs.app = nil
	cs.appMu.Unlock()
}

func (cs *pcCompatSession) deliverOrBuffer(data []byte) {
	cs.appMu.RLock()
	app := cs.app
	cs.appMu.RUnlock()
	if app != nil {
		if err := app.WriteMessage(websocket.TextMessage, data); err == nil {
			return
		}
		// Write failed — app likely disconnected; fall through to buffer.
	}
	if cs.maxQ <= 0 {
		return
	}
	cp := make([]byte, len(data))
	copy(cp, data)
	cs.qMu.Lock()
	if len(cs.queue) >= cs.maxQ {
		cs.queue = cs.queue[1:]
	}
	cs.queue = append(cs.queue, cp)
	cs.qMu.Unlock()
}

func (cs *pcCompatSession) sendToUpstream(msgType int, data []byte) error {
	cs.upMu.Lock()
	conn := cs.upConn
	cs.upMu.Unlock()
	if conn == nil {
		return fmt.Errorf("upstream reconnecting")
	}
	return conn.WriteMessage(msgType, data)
}

// pcCompatStore maps session key → pcCompatSession.
type pcCompatStore struct {
	mu       sync.Mutex
	entries  map[string]*pcCompatSession
	maxQueue int
}

func newPCCompatStore(maxQueue int) *pcCompatStore {
	return &pcCompatStore{entries: make(map[string]*pcCompatSession), maxQueue: maxQueue}
}

func (s *pcCompatStore) getOrCreate(key string, getToken func() string, getWSURL func(string) string) *pcCompatSession {
	s.mu.Lock()
	defer s.mu.Unlock()
	if e, ok := s.entries[key]; ok {
		return e
	}
	e := &pcCompatSession{
		key:      key,
		maxQ:     s.maxQueue,
		getToken: getToken,
		getWSURL: getWSURL,
		stopCh:   make(chan struct{}),
	}
	s.entries[key] = e
	return e
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

// handleWS upgrades the connection and hands it to a proxyClient.
// The optional ?client_id=<id> query parameter is the reconnect key:
// passing the same client_id on reconnect drains queued messages first.
func (s *proxyServer) handleWS(w http.ResponseWriter, r *http.Request) {
conn, err := s.upgrader.Upgrade(w, r, nil)
if err != nil {
fmt.Printf("%supgrade: %v\n", prefixERR(), err)
return
}
clientID := r.URL.Query().Get("client_id")
if clientID == "" {
clientID = fmt.Sprintf("c%d", time.Now().UnixNano()%1_000_000_000)
}
c := newProxyClient(conn, s, clientID)
fmt.Printf("%sapp connected    id=%s  remote=%s\n", prefixSYS(), c.id, r.RemoteAddr)
c.run()
fmt.Printf("%sapp disconnected id=%s\n", prefixSYS(), c.id)
}

func (s *proxyServer) handleStatus(w http.ResponseWriter, r *http.Request) {
w.Header().Set("Content-Type", "application/json")
zcH, pcH := s.getHealth()
json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
"version": "3",
"agents": map[string]any{
"zc": map[string]any{"configured": s.zcAuth != nil, "status": zcH},
"pc": map[string]any{"configured": s.pcAuth != nil, "status": pcH},
},
"sessions":  s.store.size(),
"queue_max": s.store.maxQueue,
})
}

// ── Proxy client (unified /proxy/ws) ─────────────────────────────────────────

// proxyClient is the per-WebSocket-connection handler.
// It is a thin layer over clientSession, which owns the upstream connections
// and the offline queue.
type proxyClient struct {
id      string
conn    *websocket.Conn
mu      sync.Mutex
server  *proxyServer
session *clientSession
done    chan struct{}
}

func newProxyClient(conn *websocket.Conn, s *proxyServer, clientID string) *proxyClient {
return &proxyClient{
id:      clientID,
conn:    conn,
server:  s,
session: s.store.getOrCreate(clientID),
done:    make(chan struct{}),
}
}

func (c *proxyClient) run() {
// Attach to session; drain any messages buffered while offline.
queued := c.session.attach(c)
for _, qm := range queued {
c.relayRaw(qm.agent, qm.sessionID, qm.raw)
}
if len(queued) > 0 {
fmt.Printf("%sclient %s: drained %d queued messages\n", prefixSYS(), c.id, len(queued))
}

defer func() {
close(c.done)
c.session.detach()
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

func (c *proxyClient) sendToZC(msg appMsg) {
conn, err := c.session.getOrCreateZC(c.server, msg.SessionID)
if err != nil {
c.sendError("zc", msg.SessionID, err.Error())
return
}
content, _ := msg.Payload["content"].(string)
if err := conn.sendZC(content); err != nil {
c.sendError("zc", msg.SessionID, err.Error())
}
}

func (c *proxyClient) sendToPC(msg appMsg) {
conn, err := c.session.getOrCreatePC(c.server)
if err != nil {
c.sendError("pc", msg.SessionID, err.Error())
return
}
content, _ := msg.Payload["content"].(string)
if err := conn.sendPCWithSession(msg.SessionID, content); err != nil {
c.sendError("pc", msg.SessionID, err.Error())
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
zcH, pcH := c.server.getHealth()
c.session.qMu.Lock()
queuedCount := len(c.session.queue)
c.session.qMu.Unlock()
c.sendRaw(map[string]any{
"type":   "status",
"agents": map[string]string{"zc": zcH, "pc": pcH},
"queue":  map[string]any{"buffered": queuedCount, "max": c.server.store.maxQueue},
})
}

var _ = strings.Contains
