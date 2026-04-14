package main

// session.go — v3 offline queue: per-client session store.
//
// A clientSession outlives individual WebSocket connections.  It owns the
// upstream agentConns (ZC + PC) and an in-memory queue of agent replies that
// accumulate while the app is disconnected.  On reconnect the proxy drains the
// queue before entering normal relay mode.
//
// Lifetime
//   sessionStore.getOrCreate(clientID)   — called from handleWS on every connect
//   clientSession.attach(app)             — sets active app, returns queued msgs
//   clientSession.relay(agent, sid, raw)  — called by drain goroutines; writes to
//                                            app if connected, else buffers
//   clientSession.detach()               — called when the WebSocket closes

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"
)

// ── Queued message ────────────────────────────────────────────────────────────

// queuedMsg is one agent reply buffered while the app was offline.
type queuedMsg struct {
	agent     string
	sessionID string
	raw       []byte
}

// ── Client session ────────────────────────────────────────────────────────────

// clientSession persists in sessionStore across WebSocket reconnections.
// It owns upstream agentConns and the offline message queue.
type clientSession struct {
	id       string
	maxQueue int // 0 = buffering disabled

	// upstream ZC connections, one per ZC session_id
	zcMu    sync.RWMutex
	zcConns map[string]*agentConn

	// upstream PC connection (single mux over all PC session_ids)
	pcMu   sync.Mutex
	pcConn *agentConn

	// offline queue
	qMu   sync.Mutex
	queue []queuedMsg

	// currently attached app (nil = offline)
	appMu sync.RWMutex
	app   *proxyClient

	lastSeen time.Time
}

// attach sets the active app connection and returns any queued messages.
// The caller must relay those messages to the app before handling new ones.
func (cs *clientSession) attach(app *proxyClient) []queuedMsg {
	cs.appMu.Lock()
	cs.app = app
	cs.lastSeen = time.Now()
	cs.appMu.Unlock()

	cs.qMu.Lock()
	q := cs.queue
	cs.queue = nil
	cs.qMu.Unlock()
	return q
}

// detach removes the active app connection (called on WebSocket close).
func (cs *clientSession) detach() {
	cs.appMu.Lock()
	cs.app = nil
	cs.lastSeen = time.Now()
	cs.appMu.Unlock()
}

// relay sends raw bytes to the connected app, or buffers them if offline.
func (cs *clientSession) relay(agent, sessionID string, raw []byte) {
	cs.appMu.RLock()
	app := cs.app
	cs.appMu.RUnlock()

	if app != nil {
		// App may disconnect between the check and the write; WriteMessage
		// will return an error (ignored), which is acceptable at this edge.
		app.relayRaw(agent, sessionID, raw)
		return
	}

	if cs.maxQueue <= 0 {
		return // buffering disabled
	}

	cs.qMu.Lock()
	defer cs.qMu.Unlock()
	if len(cs.queue) >= cs.maxQueue {
		cs.queue = cs.queue[1:] // drop oldest on overflow
	}
	cs.queue = append(cs.queue, queuedMsg{agent: agent, sessionID: sessionID, raw: raw})
}

// ── Upstream connection management ───────────────────────────────────────────

// getOrCreateZC returns an existing ZC upstream for sessionID or dials a new one.
func (cs *clientSession) getOrCreateZC(srv *proxyServer, sessionID string) (*agentConn, error) {
	cs.zcMu.RLock()
	conn := cs.zcConns[sessionID]
	cs.zcMu.RUnlock()
	if conn != nil {
		return conn, nil
	}

	zca := srv.zcAuth
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
		if conn.lastDialHTTPStatus == 401 || conn.lastDialHTTPStatus == 403 {
			srv.setHealth("zc", "auth_error")
		} else {
			srv.setHealth("zc", "offline")
		}
		return nil, fmt.Errorf("zeroclaw connect: %w", err)
	}
	srv.setHealth("zc", "online")
	go conn.reconnectLoop()
	go cs.drainZC(sessionID, recv, stop)

	cs.zcMu.Lock()
	cs.zcConns[sessionID] = conn
	cs.zcMu.Unlock()
	fmt.Printf("%ssession %s: ZC session=%s\n", prefixSYS(), cs.id, sessionID)
	return conn, nil
}

// getOrCreatePC returns the shared PC upstream or dials a new one.
func (cs *clientSession) getOrCreatePC(srv *proxyServer) (*agentConn, error) {
	cs.pcMu.Lock()
	defer cs.pcMu.Unlock()
	if cs.pcConn != nil {
		return cs.pcConn, nil
	}

	pca := srv.pcAuth
	if pca == nil {
		return nil, fmt.Errorf("picoclaw not configured on this proxy")
	}
	sid := "proxy-" + cs.id
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
		if conn.lastDialHTTPStatus == 401 || conn.lastDialHTTPStatus == 403 {
			srv.setHealth("pc", "auth_error")
		} else {
			srv.setHealth("pc", "offline")
		}
		return nil, fmt.Errorf("picoclaw connect: %w", err)
	}
	srv.setHealth("pc", "online")
	cs.pcConn = conn
	go conn.reconnectLoop()
	go cs.drainPC(recv, stop)
	go func() {
		t := time.NewTicker(30 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-stop:
				return
			case <-t.C:
				conn.pingPC()
			}
		}
	}()
	fmt.Printf("%ssession %s: PC connected\n", prefixSYS(), cs.id)
	return conn, nil
}

// ── Drain goroutines ──────────────────────────────────────────────────────────
//
// These run for the lifetime of the upstream connection (until stop is closed),
// NOT just while the app is connected.  When the app is offline, relay() buffers
// the frames instead of discarding them.

// drainZC routes ZC frames to the app (or to the queue while offline).
func (cs *clientSession) drainZC(sessionID string, recv chan []byte, stop chan struct{}) {
	for {
		select {
		case <-stop:
			return
		case raw, ok := <-recv:
			if !ok {
				return
			}
			cs.relay("zc", sessionID, raw)
		}
	}
}

// drainPC routes PC frames to the app (or to the queue while offline).
func (cs *clientSession) drainPC(recv chan []byte, stop chan struct{}) {
	for {
		select {
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
			cs.relay("pc", peek.SessionID, raw)
		}
	}
}

// closeUpstreams tears down all upstream connections (called on session eviction).
func (cs *clientSession) closeUpstreams() {
	cs.zcMu.Lock()
	for _, conn := range cs.zcConns {
		conn.close()
	}
	cs.zcMu.Unlock()

	cs.pcMu.Lock()
	if cs.pcConn != nil {
		cs.pcConn.close()
		cs.pcConn = nil
	}
	cs.pcMu.Unlock()
}

// ── Session store ─────────────────────────────────────────────────────────────

// sessionStore maps client_id → clientSession.  Sessions are never evicted
// in v3 (v4 will add TTL purge).
type sessionStore struct {
	mu       sync.RWMutex
	sessions map[string]*clientSession
	maxQueue int
}

func newSessionStore(maxQueue int) *sessionStore {
	return &sessionStore{
		sessions: make(map[string]*clientSession),
		maxQueue: maxQueue,
	}
}

// getOrCreate returns the existing session or creates a new one.
func (ss *sessionStore) getOrCreate(clientID string) *clientSession {
	ss.mu.Lock()
	defer ss.mu.Unlock()
	if cs, ok := ss.sessions[clientID]; ok {
		return cs
	}
	cs := &clientSession{
		id:       clientID,
		maxQueue: ss.maxQueue,
		zcConns:  make(map[string]*agentConn),
		lastSeen: time.Now(),
	}
	ss.sessions[clientID] = cs
	return cs
}

// get returns an existing session or nil.
func (ss *sessionStore) get(clientID string) *clientSession {
	ss.mu.RLock()
	defer ss.mu.RUnlock()
	return ss.sessions[clientID]
}

// size returns the number of tracked sessions.
func (ss *sessionStore) size() int {
	ss.mu.RLock()
	defer ss.mu.RUnlock()
	return len(ss.sessions)
}
