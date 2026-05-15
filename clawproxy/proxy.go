package main

// proxy.go — v4 proxy server mode for clawproxy.
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
//      GET  /api/pico/info           → {"ws_url":"ws://.../pico/ws","enabled":true}
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
"net"
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
	db            *queueStore    // SQLite-backed offline queue (shared by all sessions)
	store         *sessionStore
	pcCompat      *pcCompatStore // persistent PC compat sessions (keyed by app token)
	zcCompat      *zcCompatStore // persistent ZC compat sessions (keyed by session_id/IP)
	healthMu      sync.RWMutex
	zcHealth      string // "not_configured"|"unknown"|"online"|"offline"|"auth_error"
	pcHealth      string
}

func newProxyServer(zca *zcAuth, pca *pcAuth, port int, db *queueStore, sessionTTL time.Duration) *proxyServer {
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
		db:            db,
		store:         newSessionStore(db, sessionTTL),
		pcCompat:      newPCCompatStore(db),
		zcCompat:      newZCCompatStore(db),
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
		if status == "auth_error" {
			// Token rejected — clear it and try to re-pair automatically.
			fmt.Printf("%sZC probe: auth_error — clearing token, re-pairing\n", prefixSYS())
			deleteToken("zc_token")
			s.zcAuth.token = ""
			base := fmt.Sprintf("http://%s:%d", s.zcAuth.host, s.zcAuth.port)
			if pErr := s.zcAuth.acquireToken(base); pErr != nil {
				fmt.Printf("%sZC re-pair failed: %v\n", prefixERR(), pErr)
			} else {
				// Re-probe with the fresh token.
				sid2 := fmt.Sprintf("probe2-%d", time.Now().UnixMilli())
				status = dialProbe(s.zcAuth.wsURL(sid2), s.zcAuth.token, []string{"zeroclaw.v1"})
				fmt.Printf("%sZC re-probe after re-pair: %s\n", prefixSYS(), status)
			}
		}
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

func runProxy(port int, zca *zcAuth, pca *pcAuth, maxQueue int, dbPath string, ttl time.Duration) {
db, err := openQueueStore(dbPath, maxQueue, ttl)
if err != nil {
fmt.Printf("%scannot open queue DB %q: %v — falling back to in-memory\n", prefixERR(), dbPath, err)
db, _ = openQueueStore(":memory:", maxQueue, ttl)
}
s := newProxyServer(zca, pca, port, db, 24*time.Hour)
mux := http.NewServeMux()

// ── compat: zeroclaw (chat.py) ──────────────────────────────────
mux.HandleFunc("/health",    s.handleHealth)
mux.HandleFunc("/pair",      s.handlePair)
mux.HandleFunc("/ws/chat",   s.handleZCCompat)

// ── compat: picoclaw (chat_picoclaw.py) ────────────────────────
mux.HandleFunc("/api/pico/info",   s.handlePicoToken)
mux.HandleFunc("/pico/ws",         s.handlePCCompat)

// ── unified proxy endpoint ──────────────────────────────────────
mux.HandleFunc("/proxy/ws",     s.handleWS)
mux.HandleFunc("/proxy/status", s.handleStatus)

addr := fmt.Sprintf(":%d", port)
fmt.Printf("\n%s%sClawProxy v3%s — proxy mode\n", prefixSYS(), colBold, colReset)
fmt.Printf("%s  ZC compat  ←  GET /health · POST /pair · WS /ws/chat\n", prefixSYS())
fmt.Printf("%s  PC compat  \u2190  GET /api/pico/info · WS /pico/ws\n", prefixSYS())
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

// WS /ws/chat — persistent relay to zeroclaw upstream.
// chat.py connects here with subprotocol zeroclaw.v1.
// The upstream ZC connection is kept alive across app disconnects; frames
// buffered while the app is offline are drained on reconnect.
func (s *proxyServer) handleZCCompat(w http.ResponseWriter, r *http.Request) {
	if s.zcAuth == nil {
		http.Error(w, `{"type":"error","message":"zeroclaw not configured"}`, http.StatusServiceUnavailable)
		return
	}

	// Derive a stable session key that survives app reconnects.
	// Priority:
	//  1. ?session_id= query param — chat.py sends this on every connect (stable per chat session)
	//  2. ip-<clientIP> fallback   — same device always resumes its session
	sessionKey := r.URL.Query().Get("session_id")
	if sessionKey == "" {
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err != nil {
			host = r.RemoteAddr
		}
		sessionKey = "ip-" + host
	}

	fmt.Printf("%sZC compat connect  key=%s  subprotos=%v  remote=%s\n",
		prefixSYS(), sessionKey, websocket.Subprotocols(r), r.RemoteAddr)

	// Upgrade app WebSocket; negotiate zeroclaw.v1 and expose the session key.
	respHdr := http.Header{"X-Proxy-Session-Id": []string{sessionKey}}
	upg := websocket.Upgrader{
		CheckOrigin:  func(r *http.Request) bool { return true },
		Subprotocols: []string{"zeroclaw.v1"},
	}
	appConn, err := upg.Upgrade(w, r, respHdr)
	if err != nil {
		fmt.Printf("%sZC compat upgrade: %v\n", prefixERR(), err)
		return
	}

	// Get or create the persistent session for this app identity.
	cs := s.zcCompat.getOrCreate(
		sessionKey,
		func() string { return s.zcAuth.token },
		func(sid string) string { return s.zcAuth.wsURL(sid) },
	)
	keyShort := cs.keyShort
	cs.start() // no-op if already running

	// Attach the new app connection; drain any buffered upstream messages.
	queued := cs.attachApp(appConn)
	fmt.Printf("%sZC compat attach  key=%s  remote=%s  queued=%d\n",
		prefixSYS(), keyShort, r.RemoteAddr, len(queued))
	for _, data := range queued {
		preview := string(data)
		if len(preview) > 80 {
			preview = preview[:80] + "\u2026"
		}
		fmt.Printf("%sZC compat[%s] drain→app (%d B): %s\n", prefixSYS(), keyShort, len(data), preview)
		cs.appWrMu.Lock()
		err := appConn.WriteMessage(websocket.TextMessage, data)
		cs.appWrMu.Unlock()
		if err != nil {
			cs.detachApp()
			appConn.Close()
			return
		}
	}

	// App → upstream relay loop.
	defer func() {
		cs.detachApp()
		appConn.Close()
		fmt.Printf("%sZC compat detach  key=%s\n", prefixSYS(), keyShort)
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
		fmt.Printf("%sZC relay[%s] app\u2192gw (%d B): %s\n", prefixSYS(), keyShort, len(data), preview)
		if werr := cs.sendToUpstream(msgType, data); werr != nil {
			fmt.Printf("%sZC relay[%s] app\u2192gw write-err: %v\n", prefixERR(), keyShort, werr)
			continue
		}
		// After forwarding a message, start a 90-second silent-failure watcher.
		{
			var m struct {
				Type string `json:"type"`
				ID   string `json:"id"`
			}
			if jerr := json.Unmarshal(data, &m); jerr == nil && m.Type == "message.send" {
				msgID := m.ID
				respCh := cs.markPending()
				go func() {
					select {
					case <-respCh:
						// Response arrived — nothing to do.
					case <-time.After(90 * time.Second):
						fmt.Printf("%sZC relay[%s] no gateway response for msg %s after 90 s — synthesising error\n",
							prefixERR(), keyShort, msgID)
						errorPayload := fmt.Sprintf(
							`{"type":"error","id":%q,"payload":{"code":"timeout","message":"No response from AI gateway after 90 s. The LLM provider may be misconfigured or unavailable."}}`,
							msgID)
						cs.deliverOrBuffer([]byte(errorPayload), keyShort)
					}
				}()
			}
		}
	}
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

	// Derive a stable session key that survives app reconnects.
	// Priority:
	//  1. Authorization: Bearer <token>  — sent by chat_picoclaw.py on every connect
	//  2. token.* WebSocket subprotocol  — browser-native alternative
	//  3. ?proxy_client_id=<id>          — explicit override
	//  4. UnixNano fallback              — no identity, buffering won't work
	sessionKey := ""
	var subprotos []string

	if auth := r.Header.Get("Authorization"); strings.HasPrefix(auth, "Bearer ") {
		sessionKey = strings.TrimPrefix(auth, "Bearer ")
	}
	for _, p := range websocket.Subprotocols(r) {
		if strings.HasPrefix(p, "token.") {
			subprotos = []string{p}
			if sessionKey == "" {
				sessionKey = strings.TrimPrefix(p, "token.")
			}
			break
		}
	}
	if q := r.URL.Query().Get("proxy_client_id"); q != "" {
		sessionKey = q
	}
	if sessionKey == "" {
		// Stable fallback: key on the client IP so the same device always
		// resumes its buffered session even without a token or subprotocol.
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err != nil {
			host = r.RemoteAddr
		}
		sessionKey = "ip-" + host
	}

	fmt.Printf("%sPC compat connect  key=%s  subprotos=%v  remote=%s\n",
		prefixSYS(), sessionKey, websocket.Subprotocols(r), r.RemoteAddr)

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

	keyShort := sessionKey // will be replaced by cs.keyShort after getOrCreate
	_ = keyShort           // suppress unused-variable error before reassignment

	// Get or create the persistent session for this app identity.
	cs := s.pcCompat.getOrCreate(
		sessionKey,
		func() string { return s.pcAuth.token },
		func(sid string) string { return appendSessionID(s.pcAuth.wsURL, sid) },
	)
	keyShort = cs.keyShort // use the one stored in the session (consistent across all log lines)
	cs.start()             // no-op if already running

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
		cs.appWrMu.Lock()
		err := appConn.WriteMessage(websocket.TextMessage, data)
		cs.appWrMu.Unlock()
		if err != nil {
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
			continue
		}
		// After forwarding a message.send, start a 90-second watcher.
		// If the gateway goes silent the proxy synthesises an error frame so
		// the app doesn't hang indefinitely (e.g. when the LLM provider has
		// no API key configured).
		{
			var m struct {
				Type string `json:"type"`
				ID   string `json:"id"`
			}
			if jerr := json.Unmarshal(data, &m); jerr == nil && m.Type == "message.send" {
				msgID := m.ID
				respCh := cs.markPending()
				go func() {
					select {
					case <-respCh:
						// Response arrived — nothing to do.
					case <-time.After(90 * time.Second):
						fmt.Printf("%sPC relay[%s] no gateway response for msg %s after 90 s — synthesising error\n",
							prefixERR(), keyShort, msgID)
						errorPayload := fmt.Sprintf(
						`{"type":"error","id":%q,"payload":{"code":"timeout","message":"No response from AI gateway after 90 s. The LLM provider may be misconfigured or unavailable."}}`,
						msgID)
						cs.deliverOrBuffer([]byte(errorPayload), keyShort)
					}
				}()
			}
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
	srv      string // upstream wsURL prefix (filled on start)
	key      string // session key (app token value)
	keyShort string // display-safe truncation of key, computed once
	db       *queueStore

	// upstream connection
	upMu   sync.Mutex
	upConn *websocket.Conn

	// currently attached app (nil = offline)
	// appWrMu serialises all WriteMessage calls to cs.app so the drain loop
	// in handlePCCompat and deliverOrBuffer never write concurrently.
	appMu   sync.RWMutex
	appWrMu sync.Mutex
	app     *websocket.Conn

	// pending response tracking: notified when any gw→app frame arrives
	// after app sends a message.send. Used to detect silent failures.
	pendingMu  sync.Mutex
	pendingCh  chan struct{} // closed/replaced when a response frame arrives
	pendingReq bool

	getToken func() string           // reads current token from proxyServer
	getWSURL func(sid string) string // builds upstream wsURL

	startOnce sync.Once
	stopCh    chan struct{}
}

func (cs *pcCompatSession) start() {
	cs.startOnce.Do(func() { go cs.upstreamLoop() })
}

func (cs *pcCompatSession) upstreamLoop() {
	keyShort := cs.keyShort // consistent with handlePCCompat logs
	for {
		select {
		case <-cs.stopCh:
			return
		default:
		}
		tok := cs.getToken()
		// Use the FULL key as the picoclaw session_id so each proxy session
		// gets its own picoclaw session.  Using only keyShort (8 chars) caused
		// every reconnect to map to the same session_id, sending duplicate
		// frames to both upstreamLoops and confusing picoclaw's task scheduler.
		wsURL := cs.getWSURL("pc-compat-" + cs.key)
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
		fmt.Printf("%sPC compat[%s] upstream connected  url=%s\n", prefixSYS(), keyShort, wsURL)
		frameCount := 0
		for {
			_, data, rerr := conn.ReadMessage()
			if rerr != nil {
				fmt.Printf("%sPC compat[%s] upstream read-err after %d frames: %v\n", prefixSYS(), keyShort, frameCount, rerr)
				break
			}
			frameCount++
			preview := string(data)
			if len(preview) > 120 {
				preview = preview[:120] + "\u2026"
			}
			fmt.Printf("%sPC compat[%s] gw\u2192app frame#%d (%d B): %s\n", prefixSYS(), keyShort, frameCount, len(data), preview)
			cs.deliverOrBuffer(data, keyShort)
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
	msgs := cs.db.drain(cs.key)
	q := make([][]byte, len(msgs))
	for i, m := range msgs {
		q[i] = m.data
	}
	return q
}

func (cs *pcCompatSession) detachApp() {
	cs.appMu.Lock()
	cs.app = nil
	cs.appMu.Unlock()
}

func (cs *pcCompatSession) deliverOrBuffer(data []byte, keyShort string) {
	// Notify any pending response watcher that a frame arrived from the gateway.
	cs.pendingMu.Lock()
	if cs.pendingReq && cs.pendingCh != nil {
		close(cs.pendingCh)
		cs.pendingCh = nil
		cs.pendingReq = false
	}
	cs.pendingMu.Unlock()

	cs.appMu.RLock()
	app := cs.app
	cs.appMu.RUnlock()
	if app != nil {
		cs.appWrMu.Lock()
		err := app.WriteMessage(websocket.TextMessage, data)
		cs.appWrMu.Unlock()
		if err == nil {
			return // delivered
		}
		// Write failed — app disconnected; clear the dead reference immediately
		// so subsequent frames go straight to the buffer instead of retrying.
		cs.detachApp()
		fmt.Printf("%sPC compat[%s] deliver→app failed (app disconnected?), buffering\n", prefixERR(), keyShort)
	}
	if cs.db.maxQ <= 0 {
		fmt.Printf("%sPC compat[%s] app offline, buffering disabled — frame dropped\n", prefixSYS(), keyShort)
		return
	}
	cs.db.push(cs.key, "", "", data)
	fmt.Printf("%sPC compat[%s] buffered (%d B), queue=%d\n", prefixSYS(), keyShort, len(data), cs.db.count(cs.key))
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

// markPending registers that the app just sent a message and we expect a
// response from the gateway.  Returns a channel that is closed when any
// gateway→app frame arrives (or the timeout fires).
func (cs *pcCompatSession) markPending() <-chan struct{} {
	ch := make(chan struct{})
	cs.pendingMu.Lock()
	if cs.pendingCh != nil {
		// Previous watcher still open — close it so that goroutine exits.
		close(cs.pendingCh)
	}
	cs.pendingCh = ch
	cs.pendingReq = true
	cs.pendingMu.Unlock()
	return ch
}

// pcCompatStore maps session key → pcCompatSession.
type pcCompatStore struct {
	mu      sync.Mutex
	entries map[string]*pcCompatSession
	db      *queueStore
}

func newPCCompatStore(db *queueStore) *pcCompatStore {
	return &pcCompatStore{entries: make(map[string]*pcCompatSession), db: db}
}

func (s *pcCompatStore) getOrCreate(key string, getToken func() string, getWSURL func(string) string) *pcCompatSession {
	s.mu.Lock()
	defer s.mu.Unlock()
	if e, ok := s.entries[key]; ok {
		return e
	}
	ks := key
	if len(ks) > 16 {
		ks = ks[:8] + "\u2026" + ks[len(ks)-4:]
	}
	e := &pcCompatSession{
		key:      key,
		keyShort: ks,
		db:       s.db,
		getToken: getToken,
		getWSURL: getWSURL,
		stopCh:   make(chan struct{}),
	}
	s.entries[key] = e
	return e
}

// ── ZC compat persistent session ─────────────────────────────────────────────
//
// zcCompatSession keeps a zeroclaw upstream connection alive across app
// reconnects.  The session is keyed by the ?session_id= query param that
// chat.py sends on every connect (stable across reconnects for the same chat
// session), falling back to the client IP.  Buffered upstream frames are
// drained to the app on reconnect.

type zcCompatSession struct {
	key      string // session key (query session_id or ip-<clientIP>)
	keyShort string // display-safe truncation of key, computed once
	db       *queueStore

	// upstream connection
	upMu   sync.Mutex
	upConn *websocket.Conn

	// currently attached app (nil = offline)
	// appWrMu serialises all WriteMessage calls to cs.app so the drain loop
	// in handleZCCompat and deliverOrBuffer never write concurrently.
	appMu   sync.RWMutex
	appWrMu sync.Mutex
	app     *websocket.Conn

	// pending response tracking: notified when any gw→app frame arrives
	// after app sends a message.  Used to detect silent failures.
	pendingMu  sync.Mutex
	pendingCh  chan struct{} // closed/replaced when a response frame arrives
	pendingReq bool

	getToken func() string           // reads current ZC token from proxyServer
	getWSURL func(sid string) string // builds upstream zeroclaw wsURL

	startOnce sync.Once
	stopCh    chan struct{}
}

func (cs *zcCompatSession) start() {
	cs.startOnce.Do(func() { go cs.upstreamLoop() })
}

func (cs *zcCompatSession) upstreamLoop() {
	keyShort := cs.keyShort
	for {
		select {
		case <-cs.stopCh:
			return
		default:
		}
		// Use the stable session key as the zeroclaw session_id so ZC's cron
		// jobs always deliver to the same session, even after reconnects.
		wsURL := cs.getWSURL("zc-compat-" + cs.key)
		tok := cs.getToken()
		h := http.Header{}
		if tok != "" {
			h.Set("Authorization", "Bearer "+tok)
		}
		d := &websocket.Dialer{
			HandshakeTimeout: 10 * time.Second,
			Subprotocols:     []string{"zeroclaw.v1"},
		}
		conn, resp, err := d.Dial(wsURL, h)
		if resp != nil && resp.Body != nil {
			resp.Body.Close()
		}
		if err != nil {
			fmt.Printf("%sZC compat[%s] upstream dial failed: %v — retry 5s\n", prefixERR(), keyShort, err)
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
		fmt.Printf("%sZC compat[%s] upstream connected  url=%s\n", prefixSYS(), keyShort, wsURL)
		frameCount := 0
		for {
			_, data, rerr := conn.ReadMessage()
			if rerr != nil {
				fmt.Printf("%sZC compat[%s] upstream read-err after %d frames: %v\n", prefixSYS(), keyShort, frameCount, rerr)
				break
			}
			frameCount++
			preview := string(data)
			if len(preview) > 120 {
				preview = preview[:120] + "\u2026"
			}
			fmt.Printf("%sZC compat[%s] gw\u2192app frame#%d (%d B): %s\n", prefixSYS(), keyShort, frameCount, len(data), preview)
			cs.deliverOrBuffer(data, keyShort)
		}
		cs.upMu.Lock()
		cs.upConn = nil
		cs.upMu.Unlock()
		conn.Close()
		// Brief pause before reconnect so we don't spin if zeroclaw is restarting.
		select {
		case <-cs.stopCh:
			return
		case <-time.After(2 * time.Second):
		}
	}
}

func (cs *zcCompatSession) attachApp(app *websocket.Conn) [][]byte {
	cs.appMu.Lock()
	cs.app = app
	cs.appMu.Unlock()
	msgs := cs.db.drain(cs.key)
	q := make([][]byte, len(msgs))
	for i, m := range msgs {
		q[i] = m.data
	}
	return q
}

func (cs *zcCompatSession) detachApp() {
	cs.appMu.Lock()
	cs.app = nil
	cs.appMu.Unlock()
}

func (cs *zcCompatSession) deliverOrBuffer(data []byte, keyShort string) {
	// Notify any pending response watcher that a frame arrived from the gateway.
	cs.pendingMu.Lock()
	if cs.pendingReq && cs.pendingCh != nil {
		close(cs.pendingCh)
		cs.pendingCh = nil
		cs.pendingReq = false
	}
	cs.pendingMu.Unlock()

	cs.appMu.RLock()
	app := cs.app
	cs.appMu.RUnlock()
	if app != nil {
		cs.appWrMu.Lock()
		err := app.WriteMessage(websocket.TextMessage, data)
		cs.appWrMu.Unlock()
		if err == nil {
			return // delivered
		}
		// Write failed — app disconnected; clear the dead reference immediately
		// so subsequent frames go straight to the buffer instead of retrying.
		cs.detachApp()
		fmt.Printf("%sZC compat[%s] deliver→app failed (app disconnected?), buffering\n", prefixERR(), keyShort)
	}
	if cs.db.maxQ <= 0 {
		fmt.Printf("%sZC compat[%s] app offline, buffering disabled — frame dropped\n", prefixSYS(), keyShort)
		return
	}
	cs.db.push(cs.key, "", "", data)
	fmt.Printf("%sZC compat[%s] buffered (%d B), queue=%d\n", prefixSYS(), keyShort, len(data), cs.db.count(cs.key))
}

func (cs *zcCompatSession) sendToUpstream(msgType int, data []byte) error {
	cs.upMu.Lock()
	conn := cs.upConn
	cs.upMu.Unlock()
	if conn == nil {
		return fmt.Errorf("upstream reconnecting")
	}
	return conn.WriteMessage(msgType, data)
}

// markPending registers that the app just sent a message and we expect a
// response from the gateway.  Returns a channel that is closed when any
// gateway→app frame arrives (or the timeout fires).
func (cs *zcCompatSession) markPending() <-chan struct{} {
	ch := make(chan struct{})
	cs.pendingMu.Lock()
	if cs.pendingCh != nil {
		// Previous watcher still open — close it so that goroutine exits.
		close(cs.pendingCh)
	}
	cs.pendingCh = ch
	cs.pendingReq = true
	cs.pendingMu.Unlock()
	return ch
}

// zcCompatStore maps session key → zcCompatSession.
type zcCompatStore struct {
	mu      sync.Mutex
	entries map[string]*zcCompatSession
	db      *queueStore
}

func newZCCompatStore(db *queueStore) *zcCompatStore {
	return &zcCompatStore{entries: make(map[string]*zcCompatSession), db: db}
}

func (s *zcCompatStore) getOrCreate(key string, getToken func() string, getWSURL func(string) string) *zcCompatSession {
	s.mu.Lock()
	defer s.mu.Unlock()
	if e, ok := s.entries[key]; ok {
		return e
	}
	ks := key
	if len(ks) > 16 {
		ks = ks[:8] + "\u2026" + ks[len(ks)-4:]
	}
	e := &zcCompatSession{
		key:      key,
		keyShort: ks,
		db:       s.db,
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
"version": "4",
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
queuedCount := c.server.db.count(c.session.id)
c.sendRaw(map[string]any{
"type":   "status",
"agents": map[string]string{"zc": zcH, "pc": pcH},
"queue":  map[string]any{"buffered": queuedCount, "max": c.server.store.maxQueue},
})
}

var _ = strings.Contains
