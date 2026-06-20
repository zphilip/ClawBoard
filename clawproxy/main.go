package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"regexp"
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

// ── zeroclaw pair-code helpers ────────────────────────────────────────────────

// getPairCodeFromAPI fetches the active pair code from the zeroclaw gateway
// admin endpoints (localhost-only, no auth required):
//
//	GET  /admin/paircode      → returns current active code (if any)
//	POST /admin/paircode/new  → generates + returns a new code
//
// Falls back to the zeroclaw CLI if the HTTP calls fail (e.g. gateway not yet
// fully started) so the user can still pair interactively.
func getPairCodeFromAPI(base string) string {
	// Step 1: try to read the existing active code.
	if data, err := httpGet(base + "/admin/paircode"); err == nil {
		if code, _ := data["pairing_code"].(string); code != "" {
			fmt.Printf("%sAuto-fetched pair code from gateway API: %s%s%s\n", prefixZC(), colYellow, code, colReset)
			return code
		}
	}

	// Step 2: no active code — generate a new one via the API.
	fmt.Printf("%sNo active pair code — generating new one via API…\n", prefixZC())
	if data, err := httpPost(base+"/admin/paircode/new", nil); err == nil {
		if code, _ := data["pairing_code"].(string); code != "" {
			fmt.Printf("%sGenerated new pair code: %s%s%s\n", prefixZC(), colYellow, code, colReset)
			return code
		}
	}

	// Step 3: API unavailable — fall back to CLI.
	fmt.Printf("%sAdmin API unavailable — trying zeroclaw CLI…\n", prefixZC())
	return getPairCodeFromCLI()
}

// pairCodeRe matches the code inside the box printed by `zeroclaw gateway get-paircode`:
//
//	│  156155  │
var pairCodeRe = regexp.MustCompile(`│\s*(\d{4,8})\s*│`)

// getPairCodeFromCLI runs `zeroclaw gateway get-paircode` (and --new if needed)
// as a last-resort fallback when the admin HTTP endpoints are unavailable.
func getPairCodeFromCLI() string {
	tryGet := func(args ...string) string {
		cmd := exec.Command("zeroclaw", args...)
		out, _ := cmd.CombinedOutput()
		outStr := string(out)
		if m := pairCodeRe.FindStringSubmatch(outStr); len(m) == 2 {
			return strings.TrimSpace(m[1])
		}
		// Fallback: bare 4-8 digit line
		for _, line := range strings.Split(outStr, "\n") {
			if t := strings.TrimSpace(line); regexp.MustCompile(`^\d{4,8}$`).MatchString(t) {
				return t
			}
		}
		return ""
	}

	// First try: existing active code
	if code := tryGet("gateway", "get-paircode"); code != "" {
		fmt.Printf("%sAuto-fetched pair code from CLI: %s%s%s\n", prefixZC(), colYellow, code, colReset)
		return code
	}
	// Second try: generate a new one
	fmt.Printf("%sNo active pair code — generating new one via --new …\n", prefixZC())
	if code := tryGet("gateway", "get-paircode", "--new"); code != "" {
		fmt.Printf("%sGenerated new pair code: %s%s%s\n", prefixZC(), colYellow, code, colReset)
		return code
	}
	fmt.Printf("%sCould not auto-fetch pair code from CLI\n", prefixZC())
	return ""
}

// acquireToken obtains a zeroclaw bearer token by:
//  1. Using a code supplied via --zc-pair-code flag
//  2. Fetching via the gateway admin API (GET /admin/paircode, POST /admin/paircode/new)
//  3. Falling back to the zeroclaw CLI
//  4. Last resort: interactive prompt
//
// The base URL is the HTTP gateway base (e.g. http://127.0.0.1:42617).
func (z *zcAuth) acquireToken(base string) error {
	code := z.pairCode
	if code == "" {
		code = getPairCodeFromAPI(base)
	}
	if code == "" {
		// Last resort: interactive prompt
		fmt.Printf("%sRun manually:  %szeroclaw gateway get-paircode --new%s\n", prefixZC(), colYellow, colReset)
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
	fmt.Printf("%sPaired!  token saved to /opt/clawproxy/zc_token\n", prefixZC())
	return nil
}

func (z *zcAuth) setup() error {
	base := fmt.Sprintf("http://%s:%d", z.host, z.port)

	// load saved token if none supplied
	if z.token == "" {
		z.token = loadToken("zc_token")
		if z.token != "" {
			fmt.Printf("%szeroclaw token loaded from /opt/clawproxy/zc_token\n", prefixZC())
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

	// need to pair — auto-fetch pair code from CLI
	return z.acquireToken(base)
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

// ── Token store (/opt/clawproxy/) ────────────────────────────────────────────
//
// Tokens are persisted as plain text files so auth survives restarts:
//   /opt/clawproxy/zc_token   — zeroclaw bearer token
//   /opt/clawproxy/pc_token   — picoclaw bearer token

func clawproxyDir() (string, error) {
	dir := "/opt/clawproxy"
	if err := os.MkdirAll(dir, 0o755); err != nil {
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

// sudoReadFile reads a file as root via 'sudo /usr/bin/cat'.
// Requires a NOPASSWD sudoers rule and no NoNewPrivileges in the service.
func sudoReadFile(path string) ([]byte, error) {
	out, err := exec.Command("sudo", "/usr/bin/cat", path).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
			return nil, fmt.Errorf("sudo cat %s: %s", path, strings.TrimSpace(string(ee.Stderr)))
		}
		return nil, fmt.Errorf("sudo cat %s: %w", path, err)
	}
	return out, nil
}

// readPicoTokenFromConfig reads the raw pico channel bearer token from the
// picoclaw security config file.
//
// Token location:  /var/lib/picoclaw/.picoclaw/.security.yml
// YAML path:       channel_list.pico.settings.token
//
// Since picoclaw commit 4b76196e ("secure Pico websocket access behind launcher
// auth") the gateway no longer overrides the pico channel token with a combined
// pid+channel form.  The token is now the plain 32-char hex string written by
// generateSecureToken() and stored only in .security.yml.
func readPicoTokenFromConfig() (string, error) {
	const (
		ymlFile = "/var/lib/picoclaw/.picoclaw/.security.yml"
	)

	ymlRaw, err := sudoReadFile(ymlFile)
	if err != nil {
		return "", fmt.Errorf("cannot read %s: %w", ymlFile, err)
	}
	picoToken, parseErr := extractYAMLPicoToken(string(ymlRaw))
	if parseErr != nil {
		return "", fmt.Errorf("cannot parse pico token from %s: %w", ymlFile, parseErr)
	}

	return picoToken, nil
}

// extractYAMLPicoToken is a minimal parser for the .security.yml file.
// Canonical path is channel_list.pico.settings.token.
// It also accepts legacy channels.pico.token-style layouts.
func extractYAMLPicoToken(yml string) (string, error) {
	lines := strings.Split(yml, "\n")
	inChannelList := false
	inPico := false
	inSettings := false
	for _, raw := range lines {
		line := strings.TrimRight(raw, "\r")
		trimmed := strings.TrimSpace(line)
		indent := len(raw) - len(strings.TrimLeft(raw, " \t"))
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		// Top-level key
		if indent == 0 {
			key := strings.TrimSuffix(trimmed, ":")
			inChannelList = (key == "channel_list" || key == "channels")
			if !inChannelList {
				inPico = false
				inSettings = false
			}
			continue
		}
		// One level under channel_list/channels
		if inChannelList && indent == 2 {
			inPico = strings.TrimSuffix(trimmed, ":") == "pico"
			inSettings = false
			continue
		}
		// Under pico: either legacy token: or settings:
		if inPico && indent == 4 {
			if strings.HasPrefix(trimmed, "token:") {
				val := strings.TrimSpace(strings.TrimPrefix(trimmed, "token:"))
				val = strings.Trim(val, "\"'")
				if val != "" && val != "[NOT_HERE]" {
					return val, nil
				}
			}
			inSettings = strings.TrimSuffix(trimmed, ":") == "settings"
			continue
		}
		// Canonical token location under pico.settings
		if inSettings && indent == 6 && strings.HasPrefix(trimmed, "token:") {
			val := strings.TrimSpace(strings.TrimPrefix(trimmed, "token:"))
			val = strings.Trim(val, "\"'")
			if val != "" && val != "[NOT_HERE]" {
				return val, nil
			}
		}
	}
	return "", fmt.Errorf("channel_list.pico.settings.token not found in YAML")
}

func (p *pcAuth) setup() error {
	if p.direct {
		if p.token == "" {
			// 1. try /opt/clawproxy/pc_token (previously entered)
			if saved := loadToken("pc_token"); saved != "" {
				p.token = saved
				fmt.Printf("%spicoclaw token loaded from /opt/clawproxy/pc_token\n", prefixPC())
			} else if tok, err := readPicoTokenFromConfig(); err == nil {
				// 2. try /var/lib/picoclaw/.picoclaw/.security.yml
				p.token = tok
				fmt.Printf("%spicoclaw token loaded from .security.yml\n", prefixPC())
			} else {
				// 3. prompt interactively
				fmt.Printf("%sToken not found in runtime files: %v\n", prefixPC(), err)
				fmt.Printf("%sExpected: /var/lib/picoclaw/.picoclaw/.security.yml (channel_list.pico.settings.token)\n", prefixPC())
				fmt.Print(prefixPC() + "Picoclaw token: ")
				fmt.Scanln(&p.token)
				p.token = strings.TrimSpace(p.token)
				if p.token == "" {
					return fmt.Errorf("picoclaw token is required")
				}
				saveToken("pc_token", p.token)
				fmt.Printf("%spicoclaw token saved to /opt/clawproxy/pc_token\n", prefixPC())
			}
		}
		p.wsURL = fmt.Sprintf("ws://%s:%d/pico/ws", p.host, p.gwPort)
		fmt.Printf("%spicoclaw direct %s\n", prefixPC(), p.wsURL)
		return nil
	}
	// Web-backend mode: use /api/pico/info for host/status discovery.
	// The raw token is no longer exposed by the web API (see picoclaw commit
	// 4b76196e); read it from .security.yml as in direct mode.
	base := fmt.Sprintf("http://%s:%d", p.host, p.webPort)
	data, infoErr := httpGet(base + "/api/pico/info")
	if infoErr != nil {
		fmt.Printf("%swarn: cannot reach picoclaw-web at %s (%v) — using gateway directly\n", prefixPC(), base, infoErr)
	}

	// Token: try cache → runtime files → interactive prompt
	if p.token == "" {
		if saved := loadToken("pc_token"); saved != "" {
			p.token = saved
			fmt.Printf("%spicoclaw token loaded from /opt/clawproxy/pc_token\n", prefixPC())
		} else if tok, err := readPicoTokenFromConfig(); err == nil {
			p.token = tok
			fmt.Printf("%spicoclaw token assembled from .security.yml\n", prefixPC())
		} else {
			fmt.Printf("%sToken not found in runtime files: %v\n", prefixPC(), err)
			fmt.Printf("%sExpected: /var/lib/picoclaw/.picoclaw/.security.yml (channel_list.pico.settings.token)\n", prefixPC())
			fmt.Print(prefixPC() + "Picoclaw token: ")
			fmt.Scanln(&p.token)
			p.token = strings.TrimSpace(p.token)
			if p.token == "" {
				return fmt.Errorf("picoclaw token is required")
			}
			saveToken("pc_token", p.token)
			fmt.Printf("%spicoclaw token saved to /opt/clawproxy/pc_token\n", prefixPC())
		}
	}

	// Build the gateway WS URL.  The web backend's /pico/ws requires a launcher
	// dashboard session cookie; always connect directly to the gateway instead.
	var wsHost string
	if wsURL, _ := data["ws_url"].(string); wsURL != "" {
		// Extract host from the ws_url returned by the info endpoint.
		u := wsURL
		if len(u) > 5 {
			// Strip scheme (ws:// or wss://)
			for _, prefix := range []string{"wss://", "ws://"} {
				if strings.HasPrefix(u, prefix) {
					u = u[len(prefix):]
					break
				}
			}
			// host[:port]/...
			if idx := strings.IndexByte(u, '/'); idx >= 0 {
				u = u[:idx]
			}
			if idx := strings.LastIndexByte(u, ':'); idx >= 0 {
				wsHost = u[:idx]
			} else {
				wsHost = u
			}
		}
	}
	if wsHost == "" {
		wsHost = p.host
	}
	p.wsURL = fmt.Sprintf("ws://%s:%d/pico/ws", wsHost, p.gwPort)

	enabled, _ := data["enabled"].(bool)
	fmt.Printf("%spicoclaw web-backend %s  enabled=%v  gateway=%s\n", prefixPC(), base, enabled, p.wsURL)
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

	zcaRef *zcAuth // non-nil for ZC conns; used for re-pair on 401/403

	mu        sync.Mutex
	conn      *websocket.Conn
	connected atomic.Bool

	lastDialHTTPStatus int             // HTTP status from last failed dial (0 = none)
	zcBuf              strings.Builder // CLI mode: accumulate ZC stream chunks
	recv               chan []byte     // proxy mode: raw gateway messages (nil = CLI mode)
	stop               chan struct{}   // close to stop reconnectLoop
}

func (a *agentConn) dial() error {
	// Snapshot token under lock — getOrCreatePC may update it concurrently
	// when picoclaw restarts and the proxy server refreshes pcAuth.token.
	a.mu.Lock()
	tok := a.token
	a.mu.Unlock()

	header := http.Header{}
	var dialer *websocket.Dialer
	if a.kind == kindPC && tok != "" {
		header.Set("Authorization", "Bearer "+tok)
		dialer = &websocket.Dialer{
			HandshakeTimeout: 10 * time.Second,
			Subprotocols:     []string{"token." + tok},
		}
	} else {
		if tok != "" {
			header.Set("Authorization", "Bearer "+tok)
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
				// On ZC auth rejection, try to re-pair before the next retry.
				if a.kind == kindZC && a.zcaRef != nil &&
					(a.lastDialHTTPStatus == http.StatusUnauthorized || a.lastDialHTTPStatus == http.StatusForbidden) {
					fmt.Printf("%sZC dial auth error (HTTP %d) — clearing token, re-pairing\n",
						prefixSYS(), a.lastDialHTTPStatus)
					deleteToken("zc_token")
					a.zcaRef.token = ""
					base := fmt.Sprintf("http://%s:%d", a.zcaRef.host, a.zcaRef.port)
					if pErr := a.zcaRef.acquireToken(base); pErr != nil {
						fmt.Printf("%sZC re-pair failed: %v — retry in 15s\n", prefixERR(), pErr)
						if a.sleepOrStop(15 * time.Second) {
							return
						}
						continue
					}
					// Update connection token + wsURL with fresh token.
					a.mu.Lock()
					a.token = a.zcaRef.token
					a.wsURL = a.zcaRef.wsURL(a.sessionID)
					a.mu.Unlock()
					fmt.Printf("%sZC re-paired, retrying connection\n", prefixSYS())
					continue
				}
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
	zcHost := flag.String("zc-host", "127.0.0.1", "zeroclaw host")
	zcPort := flag.Int("zc-port", 42617, "zeroclaw gateway port")
	zcToken := flag.String("zc-token", "", "zeroclaw bearer token (skips pairing)")
	zcPairCode := flag.String("zc-pair-code", "", "zeroclaw pair code (non-interactive)")
	zcSID := flag.String("zc-sid", "", "zeroclaw session ID (CLI mode)")
	zcEnable := flag.Bool("zc", false, "enable zeroclaw")

	// picoclaw flags
	pcHost := flag.String("pc-host", "127.0.0.1", "picoclaw host")
	pcPort := flag.Int("pc-port", 18800, "picoclaw web-backend port")
	pcGWPort := flag.Int("pc-gw-port", 18790, "picoclaw gateway port")
	pcDirect := flag.Bool("pc-direct", true, "connect directly to gateway (default; skips web-backend)")
	pcToken := flag.String("pc-token", "", "picoclaw token (default: read from ~/.picoclaw/config.json)")
	pcSID := flag.String("pc-sid", "", "picoclaw session ID (CLI mode)")
	pcEnable := flag.Bool("pc", false, "enable picoclaw")

	// mode
	active := flag.String("active", "zc", "default agent in CLI mode: zc|pc")
	proxyMode := flag.Bool("proxy", false, "run as proxy server (v2)")
	proxyPort := flag.Int("proxy-port", 18780, "proxy server listen port")
	queueDepth := flag.Int("queue-depth", 1024, "offline queue depth per session (0 = disabled)")
	queueDB := flag.String("queue-db", "", "SQLite queue file path ('' = /opt/clawproxy/queue.db, ':memory:' = no persistence)")
	queueTTL := flag.Int("queue-ttl", 86400, "buffered message TTL in seconds (0 = no expiry)")

	// TTS flags (proxy mode only — registers /tts/synthesize and /tts/info)
	ttsProvider := flag.String("tts-provider", "", "default TTS provider: openai|elevenlabs|google|edge|piper|minimax|f5tts|qwen3tts|mimotts (default from config or openai)")
	ttsVoice    := flag.String("tts-voice", "", "default TTS voice ID (provider-specific)")
	ttsFormat   := flag.String("tts-format", "", "default TTS output format (mp3|opus|wav|…, default from config or mp3)")
	ttsAPIKey   := flag.String("tts-api-key", "", "TTS API key (overrides OPENAI_API_KEY / ELEVENLABS_API_KEY / GOOGLE_TTS_API_KEY)")
	ttsModel    := flag.String("tts-model", "", "OpenAI TTS model name (default from config or tts-1)")
	ttsPiperURL := flag.String("tts-piper-url", "", "Piper TTS server URL (default: http://127.0.0.1:5000/v1/audio/speech)")
	ttsEdgeBin  := flag.String("tts-edge-bin", "", "edge-tts binary name (default: edge-tts)")
	// MiniMax TTS flags (env var: MINIMAX_API_KEY)
	ttsMMKey     := flag.String("tts-minimax-key", "", "MiniMax TTS API key (overrides MINIMAX_API_KEY)")
	ttsMMModel   := flag.String("tts-minimax-model", "", "MiniMax TTS model (default from config or speech-2.8-hd)")
	ttsMMBaseURL := flag.String("tts-minimax-url", "", "MiniMax TTS base URL (default: https://api.minimaxi.com/v1/t2a_v2)")
	// F5-TTS flags (env var: F5_TTS_API_KEY)
	ttsF5Key   := flag.String("tts-f5tts-key", "", "F5-TTS Bearer token (overrides F5_TTS_API_KEY; leave empty for open servers)")
	ttsF5URL   := flag.String("tts-f5tts-url", "", "F5-TTS server base URL (default: http://apicn.aiworm.cn:8010)")
	ttsF5Speed := flag.Float64("tts-f5tts-speed", 0, "F5-TTS speech speed 0.5–2.0 (0 = use config/default 1.0; try 0.8 if too fast)")
	// Qwen3-TTS flags (env var: QWEN3_TTS_API_KEY)
	ttsQ3Key   := flag.String("tts-qwen3-key", "", "Qwen3-TTS Bearer token (overrides QWEN3_TTS_API_KEY; leave empty for open servers)")
	ttsQ3URL   := flag.String("tts-qwen3-url", "", "Qwen3-TTS server base URL (default: http://apicn.aiworm.cn:8011)")
	ttsQ3Model := flag.String("tts-qwen3-model", "", "Qwen3-TTS model name (default: qwen3-tts; also: tts-1, tts-1-zh)")
	ttsQ3Speed := flag.Float64("tts-qwen3-speed", 0, "Qwen3-TTS speech speed 0.5–2.0 (0 = use config/default 1.0)")
	ttsQ3TimeoutSecs := flag.Int("tts-qwen3-timeout", 0, "Qwen3-TTS HTTP timeout in seconds (0 = default 600)")
	// MiMo-V2.5-TTS flags (env var: MIMO_API_KEY)
	ttsMiMoKey   := flag.String("tts-mimo-key", "", "MiMo TTS API key (overrides MIMO_API_KEY)")
	ttsMiMoURL   := flag.String("tts-mimo-url", "", "MiMo TTS base URL (default: https://api.xiaomimimo.com/v1)")
	ttsMiMoModel := flag.String("tts-mimo-model", "", "MiMo TTS model: mimo-v2.5-tts | mimo-v2.5-tts-voicedesign | mimo-v2.5-tts-voiceclone (default: mimo-v2.5-tts)")
	// Config file flags — path discovery for each supported config ecosystem.
	// Pass '-' to disable a source entirely.
	clawproxyConfigPath := flag.String("clawproxy-config", "", "clawproxy config.toml path (default: /opt/clawproxy/config.toml; '-' = disable)")
	configPath          := flag.String("config", "", "zeroclaw config.toml path (default: auto-discover; '-' = disable)")
	picoConfigPath      := flag.String("picoclaw-config", "", "picoclaw config.json path (default: auto-discover; '-' = disable)")
	openConfigPath      := flag.String("openclaw-config", "", "openclaw openclaw.json path (default: auto-discover; '-' = disable)")

	flag.Parse()

	// Build a refreshTtsCfg closure that captures the parsed CLI/env values.
	// Calling it re-reads all config files from disk (same priority rules apply),
	// enabling hot-reload via SIGHUP or POST /admin/reload without a restart.
	refreshTtsCfg := func() *TtsConfig {
		return initTtsConfig(*ttsProvider, *ttsVoice, *ttsFormat, *ttsAPIKey, *ttsModel, *ttsPiperURL, *ttsEdgeBin,
			*ttsMMKey, *ttsMMModel, *ttsMMBaseURL,
			*ttsF5Key, *ttsF5URL, *ttsF5Speed,
			*ttsQ3Key, *ttsQ3URL, *ttsQ3Model, *ttsQ3Speed,
			time.Duration(*ttsQ3TimeoutSecs)*time.Second,
			*ttsMiMoKey, *ttsMiMoURL, *ttsMiMoModel,
			*clawproxyConfigPath, *configPath, *picoConfigPath, *openConfigPath)
	}

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
			if a.wsURL == "" {
				// wsURL is needed for probes; fill it even without a token so
				// probeAll() can keep retrying until picoclaw starts writing its
				// runtime files.
				a.wsURL = fmt.Sprintf("ws://%s:%d/pico/ws", a.host, a.gwPort)
			}
			// Keep pca non-nil so probeAll() retries token assembly every 30s.
			pca = a
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
		resolvedDB := *queueDB
		if resolvedDB == "" {
			if dir, err := clawproxyDir(); err == nil {
				resolvedDB = dir + "/queue.db"
			} else {
				resolvedDB = ":memory:"
			}
		}
		ttl := time.Duration(*queueTTL) * time.Second
		runProxy(*proxyPort, zca, pca, *queueDepth, resolvedDB, ttl, refreshTtsCfg)
	}

	// ── CLI mode: connect ─────────────────────────────────────────────
	activeAgent := agentKind(*active)
	if activeAgent != kindZC && activeAgent != kindPC {
		fmt.Fprintf(os.Stderr, "Error: --active must be 'zc' or 'pc'\n")
		os.Exit(1)
	}

	var zc *agentConn
	if zca != nil {
		zc = &agentConn{kind: kindZC, wsURL: zca.wsURL(*zcSID), token: zca.token, sessionID: *zcSID, zcaRef: zca}
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
