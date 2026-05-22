// testclient — clawproxy integration test client
//
// Tests every endpoint in both non-streaming and streaming TTS modes.
//
// Usage:
//
//	go run ./testclient [flags]
//
// Flags:
//
//	--host          proxy host (default: 127.0.0.1)
//	--port          proxy port (default: 18780)
//	--msg           text to send to the agent (default: short sentence)
//	--tts-provider  TTS provider to use (default: "edge")
//	--tts-voice     TTS voice override
//	--timeout       per-test timeout in seconds (default: 300)
//	--http-only     only run HTTP endpoint tests, skip WebSocket
//	--zc            include ZeroClaw WebSocket tests
//	--pc            include PicoClaw WebSocket tests

package main

import (
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// ── ANSI colours ──────────────────────────────────────────────────────────────

const (
	cReset  = "\033[0m"
	cBold   = "\033[1m"
	cGrey   = "\033[90m"
	cCyan   = "\033[1;36m"
	cGreen  = "\033[1;32m"
	cYellow = "\033[1;33m"
	cRed    = "\033[1;31m"
	cBlue   = "\033[1;34m"
)

// ── Config ────────────────────────────────────────────────────────────────────

type cfg struct {
	host        string
	port        int
	msg         string
	ttsProvider string
	ttsVoice    string
	timeout     time.Duration
	httpOnly    bool
	runZC       bool
	runPC       bool
}

// ── Result tracking ───────────────────────────────────────────────────────────

type result struct {
	name   string
	passed bool
	note   string
	dur    time.Duration
}

var results []result

func pass(name, note string, d time.Duration) {
	results = append(results, result{name, true, note, d})
	fmt.Printf("  %s✓%s %-40s %s%s%s  %s(%s)%s\n",
		cGreen, cReset, name, cGrey, note, cReset, cGrey, d.Round(time.Millisecond), cReset)
}

func fail(name, note string, d time.Duration) {
	results = append(results, result{name, false, note, d})
	fmt.Printf("  %s✗%s %-40s %s%s%s  %s(%s)%s\n",
		cRed, cReset, name, cRed, note, cReset, cGrey, d.Round(time.Millisecond), cReset)
}

func skip(name, note string) {
	results = append(results, result{name, true, "SKIP: " + note, 0})
	fmt.Printf("  %s-%s %-40s %s(skipped: %s)%s\n", cYellow, cReset, name, cGrey, note, cReset)
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

func httpBase(c *cfg) string {
	return fmt.Sprintf("http://%s:%d", c.host, c.port)
}

func wsBase(c *cfg) string {
	return fmt.Sprintf("ws://%s:%d", c.host, c.port)
}

func doGet(rawURL string, timeout time.Duration) (map[string]any, int, time.Duration, error) {
	client := &http.Client{Timeout: timeout}
	t0 := time.Now()
	resp, err := client.Get(rawURL) //nolint:gosec
	dur := time.Since(t0)
	if err != nil {
		return nil, 0, dur, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(body, &out)
	return out, resp.StatusCode, dur, nil
}

func doPost(rawURL string, body io.Reader, contentType string, timeout time.Duration) (map[string]any, int, time.Duration, error) {
	client := &http.Client{Timeout: timeout}
	t0 := time.Now()
	req, _ := http.NewRequest(http.MethodPost, rawURL, body)
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := client.Do(req)
	dur := time.Since(t0)
	if err != nil {
		return nil, 0, dur, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(respBody, &out)
	return out, resp.StatusCode, dur, nil
}

// ── HTTP endpoint tests ───────────────────────────────────────────────────────

func testHTTP(c *cfg) {
	banner("HTTP endpoints")

	// GET /health
	{
		data, code, dur, err := doGet(httpBase(c)+"/health", c.timeout)
		name := "GET /health"
		if err != nil {
			fail(name, err.Error(), dur)
		} else if code != 200 {
			fail(name, fmt.Sprintf("HTTP %d", code), dur)
		} else {
			pass(name, fmt.Sprintf("paired=%v", data["paired"]), dur)
		}
	}

	// POST /pair  (no-op — any code accepted)
	{
		req := strings.NewReader(``)
		data, code, dur, err := doPost(httpBase(c)+"/pair", req, "", c.timeout)
		name := "POST /pair"
		if err != nil {
			fail(name, err.Error(), dur)
		} else if code != 200 {
			fail(name, fmt.Sprintf("HTTP %d", code), dur)
		} else {
			pass(name, fmt.Sprintf("token=%v", data["token"] != nil), dur)
		}
	}

	// GET /api/pico/info
	{
		data, code, dur, err := doGet(httpBase(c)+"/api/pico/info", c.timeout)
		name := "GET /api/pico/info"
		if err != nil {
			fail(name, err.Error(), dur)
		} else if code != 200 {
			fail(name, fmt.Sprintf("HTTP %d", code), dur)
		} else {
			pass(name, fmt.Sprintf("ws_url=%v", data["ws_url"]), dur)
		}
	}

	// GET /proxy/status
	{
		data, code, dur, err := doGet(httpBase(c)+"/proxy/status", c.timeout)
		name := "GET /proxy/status"
		if err != nil {
			fail(name, err.Error(), dur)
		} else if code != 200 {
			fail(name, fmt.Sprintf("HTTP %d", code), dur)
		} else {
			agents, _ := data["agents"].(map[string]any)
			zcStatus, pcStatus := "?", "?"
			if agents != nil {
				if zc, ok := agents["zc"].(map[string]any); ok {
					zcStatus, _ = zc["status"].(string)
				}
				if pc, ok := agents["pc"].(map[string]any); ok {
					pcStatus, _ = pc["status"].(string)
				}
			}
			pass(name, fmt.Sprintf("zc=%s pc=%s", zcStatus, pcStatus), dur)
		}
	}

	// GET /tts/info
	{
		data, code, dur, err := doGet(httpBase(c)+"/tts/info", c.timeout)
		name := "GET /tts/info"
		if err != nil {
			fail(name, err.Error(), dur)
		} else if code != 200 {
			fail(name, fmt.Sprintf("HTTP %d", code), dur)
		} else {
			pass(name, fmt.Sprintf("provider=%v format=%v", data["default_provider"], data["default_format"]), dur)
		}
	}

	// POST /tts/synthesize  — synthesize c.msg (or a short fallback) and save audio
	{
		name := "POST /tts/synthesize"
		provider := c.ttsProvider
		if provider == "" {
			provider = "edge"
		}
		text := c.msg
		if text == "" {
			text = "Hi."
		}
		payload := fmt.Sprintf(`{"text":%q,"provider":%q}`, text, provider)
		if c.ttsVoice != "" {
			payload = fmt.Sprintf(`{"text":%q,"provider":%q,"voice":%q}`, text, provider, c.ttsVoice)
		}
		data, code, dur, err := doPost(httpBase(c)+"/tts/synthesize",
			strings.NewReader(payload), "application/json", c.timeout)
		if err != nil {
			fail(name, err.Error(), dur)
		} else if code != 200 {
			note := fmt.Sprintf("HTTP %d", code)
			if data != nil {
				if e, ok := data["error"].(string); ok {
					note += " — " + e
				}
			}
			fail(name, note, dur)
		} else {
			ab64, _ := data["audio_b64"].(string)
			decoded, _ := base64.StdEncoding.DecodeString(ab64)
			// Save audio to a file named tts_<provider>.<format>
			outFmt, _ := data["format"].(string)
			if outFmt == "" {
				outFmt = "wav"
			}
			outFile := fmt.Sprintf("tts_%s.%s", provider, outFmt)
			saveNote := ""
			if writeErr := os.WriteFile(outFile, decoded, 0o644); writeErr == nil {
				saveNote = "  saved→" + outFile
			}
			pass(name, fmt.Sprintf("provider=%v voice=%v bytes=%d%s",
				data["provider"], data["voice"], len(decoded), saveNote), dur)
		}
	}
}

// ── WebSocket helpers ─────────────────────────────────────────────────────────

type frame map[string]any

func (f frame) typ() string {
	t, _ := f["type"].(string)
	return t
}

func (f frame) content() string {
	if c, ok := f["content"].(string); ok {
		return c
	}
	if p, ok := f["payload"].(map[string]any); ok {
		if c, ok2 := p["content"].(string); ok2 {
			return c
		}
	}
	return ""
}

func dialWS(rawURL string, timeout time.Duration) (*websocket.Conn, time.Duration, error) {
	d := websocket.Dialer{HandshakeTimeout: timeout}
	t0 := time.Now()
	conn, _, err := d.Dial(rawURL, nil)
	return conn, time.Since(t0), err
}

// readFrames reads frames for plain-relay tests (no TTS).
// Stops on done, error, or timeout.
func readFrames(conn *websocket.Conn, deadline time.Time) []frame {
	var frames []frame
	for time.Now().Before(deadline) {
		_ = conn.SetReadDeadline(deadline)
		_, msg, err := conn.ReadMessage()
		if err != nil {
			break
		}
		var f frame
		if json.Unmarshal(msg, &f) == nil {
			frames = append(frames, f)
			t := f.typ()
			if t == "done" || t == "error" {
				break
			}
		}
	}
	return frames
}

// readFramesUntilDone reads frames until a "done" frame or timeout,
// collecting ALL frames (so we can count tts.chunk + tts.audio frames).
func readFramesUntilFinalAudio(conn *websocket.Conn, deadline time.Time) []frame {
	var frames []frame
	for time.Now().Before(deadline) {
		_ = conn.SetReadDeadline(deadline)
		_, msg, err := conn.ReadMessage()
		if err != nil {
			break
		}
		var f frame
		if json.Unmarshal(msg, &f) == nil {
			frames = append(frames, f)
			// tts.audio with is_final=true is the last frame in streaming mode
			if f.typ() == "tts.audio" {
				isFinal, _ := f["is_final"].(bool)
				if isFinal {
					break
				}
			}
			// plain done (non-TTS streaming) — keep going to catch tts.audio
		}
	}
	return frames
}

// framesSummary returns a short string: "chunk×3 done×1 tts.audio×1"
func framesSummary(frames []frame) string {
	counts := map[string]int{}
	for _, f := range frames {
		counts[f.typ()]++
	}
	var parts []string
	order := []string{"chunk", "done", "message", "message.create", "message.update",
		"typing.start", "typing.stop", "tts.chunk", "tts.audio", "error"}
	seen := map[string]bool{}
	for _, k := range order {
		if n, ok := counts[k]; ok {
			parts = append(parts, fmt.Sprintf("%s×%d", k, n))
			seen[k] = true
		}
	}
	for k, n := range counts {
		if !seen[k] {
			parts = append(parts, fmt.Sprintf("%s×%d", k, n))
		}
	}
	if len(parts) == 0 {
		return "(no frames)"
	}
	return strings.Join(parts, " ")
}

func hasTtsAudio(frames []frame) bool {
	for _, f := range frames {
		if f.typ() == "tts.audio" {
			return true
		}
	}
	return false
}

func countTtsChunks(frames []frame) int {
	n := 0
	for _, f := range frames {
		if f.typ() == "tts.chunk" {
			n++
		}
	}
	return n
}

func audioBytes(frames []frame) int {
	for _, f := range frames {
		if f.typ() == "tts.audio" || f.typ() == "tts.chunk" {
			if ab64, ok := f["audio_b64"].(string); ok {
				b, _ := base64.StdEncoding.DecodeString(ab64)
				return len(b) // return first one for quick display
			}
		}
	}
	return 0
}

// ── ZeroClaw WebSocket tests ──────────────────────────────────────────────────

func testZC(c *cfg) {
	banner("ZeroClaw WebSocket  (:42617 via /ws/chat)")

	sendMsg := func(conn *websocket.Conn) {
		_ = conn.WriteJSON(map[string]any{"type": "message", "content": c.msg})
	}

	// 1. Plain relay — no TTS
	{
		name := "WS /ws/chat (plain relay)"
		wsURL := wsBase(c) + "/ws/chat?session_id=tc-plain"
		conn, dialDur, err := dialWS(wsURL, c.timeout)
		if err != nil {
			fail(name, "dial: "+shortErr(err), dialDur)
		} else {
			sendMsg(conn)
			t0 := time.Now()
			frames := readFrames(conn, time.Now().Add(c.timeout))
			dur := dialDur + time.Since(t0)
			conn.Close()

			if len(frames) == 0 {
				fail(name, "no frames received", dur)
			} else {
				pass(name, framesSummary(frames), dur)
			}
		}
	}

	// 2. ?tts=1 — non-streaming TTS injection
	{
		name := "WS /ws/chat?tts=1 (non-stream TTS)"
		q := url.Values{}
		q.Set("session_id", "tc-tts1")
		q.Set("tts", "1")
		if c.ttsProvider != "" {
			q.Set("tts_provider", c.ttsProvider)
		}
		if c.ttsVoice != "" {
			q.Set("tts_voice", c.ttsVoice)
		}
		wsURL := wsBase(c) + "/ws/chat?" + q.Encode()
		conn, dialDur, err := dialWS(wsURL, c.timeout)
		if err != nil {
			fail(name, "dial: "+shortErr(err), dialDur)
		} else {
			sendMsg(conn)
			t0 := time.Now()
			frames := readFramesUntilFinalAudio(conn, time.Now().Add(c.timeout))
			dur := dialDur + time.Since(t0)
			conn.Close()

			if len(frames) == 0 {
				fail(name, "no frames received", dur)
			} else if !hasTtsAudio(frames) {
				fail(name, "tts.audio frame missing — "+framesSummary(frames), dur)
			} else {
				pass(name, framesSummary(frames)+fmt.Sprintf(" audio=%dB", audioBytes(frames)), dur)
			}
		}
	}

	// 3. /ws/chat/tts — streaming TTS
	{
		name := "WS /ws/chat/tts (streaming TTS)"
		q := url.Values{}
		q.Set("session_id", "tc-tts-stream")
		if c.ttsProvider != "" {
			q.Set("tts_provider", c.ttsProvider)
		}
		if c.ttsVoice != "" {
			q.Set("tts_voice", c.ttsVoice)
		}
		wsURL := wsBase(c) + "/ws/chat/tts?" + q.Encode()
		conn, dialDur, err := dialWS(wsURL, c.timeout)
		if err != nil {
			fail(name, "dial: "+shortErr(err), dialDur)
		} else {
			sendMsg(conn)
			t0 := time.Now()
			frames := readFramesUntilFinalAudio(conn, time.Now().Add(c.timeout))
			dur := dialDur + time.Since(t0)
			conn.Close()

			if len(frames) == 0 {
				fail(name, "no frames received", dur)
			} else {
				chunks := countTtsChunks(frames)
				hasAudio := hasTtsAudio(frames)
				summary := framesSummary(frames)
				if !hasAudio && chunks == 0 {
					fail(name, "no TTS frames — "+summary, dur)
				} else {
					pass(name, summary+fmt.Sprintf(" chunks=%d audio=%dB", chunks, audioBytes(frames)), dur)
				}
			}
		}
	}
}

// ── PicoClaw WebSocket tests ──────────────────────────────────────────────────

func testPC(c *cfg) {
	banner("PicoClaw WebSocket  (:18790 via /pico/ws)")

	sendMsg := func(conn *websocket.Conn) {
		_ = conn.WriteJSON(map[string]any{
			"type": "message.send",
			"id":   "testclient-1",
			"payload": map[string]any{
				"content": c.msg,
			},
		})
	}

	// readPC reads frames until message.create + typing.stop or timeout.
	// Also stops on tts.audio with is_final or plain error.
	readPC := func(conn *websocket.Conn, deadline time.Time) []frame {
		var frames []frame
		for time.Now().Before(deadline) {
			_ = conn.SetReadDeadline(deadline)
			_, msg, err := conn.ReadMessage()
			if err != nil {
				break
			}
			var f frame
			if json.Unmarshal(msg, &f) == nil {
				frames = append(frames, f)
				t := f.typ()
				if t == "error" {
					break
				}
				if t == "tts.audio" {
					if isFinal, _ := f["is_final"].(bool); isFinal {
						break
					}
				}
				// No single "done" in pico protocol — stop after typing.stop
				// BUT we need to keep going if TTS is expected.
				// We collect up to timeout after typing.stop to catch tts.audio.
			}
		}
		return frames
	}

	// 1. Plain relay — no TTS
	{
		name := "WS /pico/ws (plain relay)"
		wsURL := wsBase(c) + "/pico/ws"
		conn, dialDur, err := dialWS(wsURL, c.timeout)
		if err != nil {
			fail(name, "dial: "+shortErr(err), dialDur)
		} else {
			sendMsg(conn)
			t0 := time.Now()
			frames := readPC(conn, time.Now().Add(c.timeout))
			dur := dialDur + time.Since(t0)
			conn.Close()

			if len(frames) == 0 {
				fail(name, "no frames received", dur)
			} else {
				pass(name, framesSummary(frames), dur)
			}
		}
	}

	// 2. ?tts=1 — non-streaming TTS injection
	{
		name := "WS /pico/ws?tts=1 (non-stream TTS)"
		q := url.Values{}
		q.Set("tts", "1")
		if c.ttsProvider != "" {
			q.Set("tts_provider", c.ttsProvider)
		}
		if c.ttsVoice != "" {
			q.Set("tts_voice", c.ttsVoice)
		}
		wsURL := wsBase(c) + "/pico/ws?" + q.Encode()
		conn, dialDur, err := dialWS(wsURL, c.timeout)
		if err != nil {
			fail(name, "dial: "+shortErr(err), dialDur)
		} else {
			sendMsg(conn)
			t0 := time.Now()
			frames := readPC(conn, time.Now().Add(c.timeout))
			dur := dialDur + time.Since(t0)
			conn.Close()

			if len(frames) == 0 {
				fail(name, "no frames received", dur)
			} else if !hasTtsAudio(frames) {
				fail(name, "tts.audio frame missing — "+framesSummary(frames), dur)
			} else {
				pass(name, framesSummary(frames)+fmt.Sprintf(" audio=%dB", audioBytes(frames)), dur)
			}
		}
	}

	// 3. /pico/ws/tts — streaming TTS
	{
		name := "WS /pico/ws/tts (streaming TTS)"
		q := url.Values{}
		if c.ttsProvider != "" {
			q.Set("tts_provider", c.ttsProvider)
		}
		if c.ttsVoice != "" {
			q.Set("tts_voice", c.ttsVoice)
		}
		wsURL := wsBase(c) + "/pico/ws/tts?" + q.Encode()
		conn, dialDur, err := dialWS(wsURL, c.timeout)
		if err != nil {
			fail(name, "dial: "+shortErr(err), dialDur)
		} else {
			sendMsg(conn)
			t0 := time.Now()
			frames := readPC(conn, time.Now().Add(c.timeout))
			dur := dialDur + time.Since(t0)
			conn.Close()

			if len(frames) == 0 {
				fail(name, "no frames received", dur)
			} else {
				chunks := countTtsChunks(frames)
				hasAudio := hasTtsAudio(frames)
				summary := framesSummary(frames)
				if !hasAudio && chunks == 0 {
					fail(name, "no TTS frames — "+summary, dur)
				} else {
					pass(name, summary+fmt.Sprintf(" chunks=%d audio=%dB", chunks, audioBytes(frames)), dur)
				}
			}
		}
	}
}

// ── Verbose frame dump ────────────────────────────────────────────────────────

// printFrameDump dials a WS URL, sends one message, and prints every frame
// received with its full JSON (pretty-printed) until done/timeout.
// Used when --dump flag is set.
func printFrameDump(wsURL, sendJSON string, timeout time.Duration) {
	fmt.Printf("\n%s%s FRAME DUMP %s%s\n", cBold, cBlue, wsURL, cReset)
	conn, _, err := dialWS(wsURL, timeout)
	if err != nil {
		fmt.Printf("  %sdial error: %v%s\n", cRed, err, cReset)
		return
	}
	defer conn.Close()
	if err := conn.WriteMessage(websocket.TextMessage, []byte(sendJSON)); err != nil {
		fmt.Printf("  %swrite error: %v%s\n", cRed, err, cReset)
		return
	}
	_ = conn.SetReadDeadline(time.Now().Add(timeout))
	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			fmt.Printf("  %s(read end: %v)%s\n", cGrey, err, cReset)
			break
		}
		var f frame
		json.Unmarshal(msg, &f) //nolint:errcheck
		t := f.typ()

		// redact audio payload for readability
		redacted := make(frame)
		for k, v := range f {
			if k == "audio_b64" {
				if s, ok := v.(string); ok {
					b, _ := base64.StdEncoding.DecodeString(s)
					redacted[k] = fmt.Sprintf("<base64 %d bytes>", len(b))
				} else {
					redacted[k] = v
				}
			} else {
				redacted[k] = v
			}
		}
		pretty, _ := json.MarshalIndent(redacted, "    ", "  ")
		colour := cReset
		switch t {
		case "tts.audio":
			colour = cGreen
		case "tts.chunk":
			colour = cCyan
		case "chunk":
			colour = cGrey
		case "done", "message.create":
			colour = cYellow
		case "error":
			colour = cRed
		}
		fmt.Printf("  %s[%s]%s %s\n", colour, t, cReset, string(pretty))

		// Stop on error, or on the final tts.audio (server closes after is_final).
		// Do NOT stop on "done" — TTS frames arrive asynchronously after it.
		if t == "error" {
			break
		}
		if t == "tts.audio" {
			if isFinal, _ := f["is_final"].(bool); isFinal {
				break
			}
		}
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func banner(title string) {
	fmt.Printf("\n%s%s── %s %s──%s\n", cBold, cBlue, title,
		strings.Repeat("─", max(0, 50-len(title))), cReset)
}

func shortErr(err error) string {
	s := err.Error()
	// trim "dial tcp 127.0.0.1:NNNNN: connect: " prefix
	if i := strings.Index(s, "connect: "); i >= 0 {
		s = s[i+9:]
	}
	if len(s) > 80 {
		s = s[:80]
	}
	return s
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// ── Main ──────────────────────────────────────────────────────────────────────

func main() {
	host := flag.String("host", "127.0.0.1", "clawproxy host")
	port := flag.Int("port", 18780, "clawproxy port")
	msg := flag.String("msg", "Say exactly three sentences about the sky.", "message to send to agent")
	ttsProvider := flag.String("tts-provider", "edge", "TTS provider (openai|elevenlabs|google|edge|piper|minimax)")
	ttsVoice := flag.String("tts-voice", "", "TTS voice override (empty = server default)")
	timeoutSec := flag.Int("timeout", 300, "per-test timeout (seconds)")
	httpOnly := flag.Bool("http-only", false, "only run HTTP tests, skip WebSocket")
	runZC := flag.Bool("zc", false, "run ZeroClaw WS tests")
	runPC := flag.Bool("pc", false, "run PicoClaw WS tests")
	dump := flag.Bool("dump", false, "frame-dump mode: print every WS frame verbosely")
	dumpZC := flag.Bool("dump-zc", false, "dump ZC streaming TTS frames")
	dumpPC := flag.Bool("dump-pc", false, "dump PC streaming TTS frames")
	flag.Parse()

	c := &cfg{
		host:        *host,
		port:        *port,
		msg:         *msg,
		ttsProvider: *ttsProvider,
		ttsVoice:    *ttsVoice,
		timeout:     time.Duration(*timeoutSec) * time.Second,
		httpOnly:    *httpOnly,
		runZC:       *runZC || *dump || *dumpZC,
		runPC:       *runPC || *dump || *dumpPC,
	}

	fmt.Printf("%s%sclawproxy test client%s  →  %s:%d\n%s",
		cBold, cCyan, cReset, c.host, c.port, cGrey)
	fmt.Printf("msg: %q   tts-provider: %s   timeout: %s\n%s\n",
		c.msg, c.ttsProvider, c.timeout, cReset)

	// ── Dump mode (verbose, single endpoint) ─────────────────────────────────
	if *dumpZC || (*dump && c.runZC) {
		q := url.Values{}
		q.Set("session_id", "dump")
		if c.ttsProvider != "" {
			q.Set("tts_provider", c.ttsProvider)
		}
		if c.ttsVoice != "" {
			q.Set("tts_voice", c.ttsVoice)
		}
		zcStreamURL := wsBase(c) + "/ws/chat/tts?" + q.Encode()
		sendJSON := fmt.Sprintf(`{"type":"message","content":%q}`, c.msg)
		printFrameDump(zcStreamURL, sendJSON, c.timeout)
	}
	if *dumpPC || (*dump && c.runPC) {
		q := url.Values{}
		if c.ttsProvider != "" {
			q.Set("tts_provider", c.ttsProvider)
		}
		if c.ttsVoice != "" {
			q.Set("tts_voice", c.ttsVoice)
		}
		pcStreamURL := wsBase(c) + "/pico/ws/tts?" + q.Encode()
		sendJSON := fmt.Sprintf(`{"type":"message.send","id":"dump-1","payload":{"content":%q}}`, c.msg)
		printFrameDump(pcStreamURL, sendJSON, c.timeout)
	}
	if *dump || *dumpZC || *dumpPC {
		os.Exit(0)
	}

	// ── Normal test suite ─────────────────────────────────────────────────────
	testHTTP(c)
	if !c.httpOnly {
		if c.runZC {
			testZC(c)
		} else {
			banner("ZeroClaw WebSocket  (:42617 via /ws/chat)")
			skip("WS /ws/chat (plain relay)", "pass --zc to enable")
			skip("WS /ws/chat?tts=1 (non-stream TTS)", "pass --zc to enable")
			skip("WS /ws/chat/tts (streaming TTS)", "pass --zc to enable")
		}
		if c.runPC {
			testPC(c)
		} else {
			banner("PicoClaw WebSocket  (:18790 via /pico/ws)")
			skip("WS /pico/ws (plain relay)", "pass --pc to enable")
			skip("WS /pico/ws?tts=1 (non-stream TTS)", "pass --pc to enable")
			skip("WS /pico/ws/tts (streaming TTS)", "pass --pc to enable")
		}
	}

	// ── Summary ───────────────────────────────────────────────────────────────
	banner("Summary")
	passed, failed, skipped := 0, 0, 0
	for _, r := range results {
		if strings.HasPrefix(r.note, "SKIP:") {
			skipped++
		} else if r.passed {
			passed++
		} else {
			failed++
		}
	}
	total := passed + failed
	fmt.Printf("\n  %d / %d tests passed", passed, total)
	if skipped > 0 {
		fmt.Printf("  (%d skipped)", skipped)
	}
	if failed > 0 {
		fmt.Printf("  %s%d failed%s", cRed, failed, cReset)
		fmt.Println()
		os.Exit(1)
	}
	fmt.Println()
}
