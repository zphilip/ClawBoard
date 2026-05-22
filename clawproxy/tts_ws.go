package main

// tts_ws.go — TTS integration for clawproxy WebSocket endpoints.
//
// ── Non-streaming (?tts=1 on /ws/chat and /pico/ws) ──────────────────────────
//
//   Add ?tts=1 (plus optional overrides) to the existing compat WebSocket URL.
//   When the agent sends a final text frame, clawproxy synthesises audio and
//   injects a {"type":"tts.audio",...} frame immediately after it.
//
//   Query params:
//     tts=1                   — enable TTS for this connection (required)
//     tts_provider=openai     — override provider  (default: server default)
//     tts_voice=alloy         — override voice     (default: server default)
//     tts_format=mp3          — override format    (default: mp3)
//
//   Extra frame injected after each final agent reply:
//     {"type":"tts.audio","audio_b64":"<base64>","format":"mp3",
//      "provider":"openai","voice":"alloy"}
//
// ── Streaming (/ws/chat/tts and /pico/ws/tts) ────────────────────────────────
//
//   Dedicated streaming endpoints.  Text chunks are sentence-buffered; each
//   complete sentence is synthesised immediately and sent as a tts.chunk frame
//   alongside the original text stream.
//
//   Same query params as above (tts=1 is implicit — the endpoint always does TTS).
//
//   Frames sent to the app (in addition to normal agent frames):
//     {"type":"tts.chunk","seq":1,"text":"First sentence.","audio_b64":"...",
//      "format":"mp3","provider":"openai","voice":"alloy","is_final":false}
//     {"type":"tts.audio","seq":2,"text":"Last bit.","audio_b64":"...",
//      "format":"mp3","provider":"openai","voice":"alloy","is_final":true}
//
//   The "seq" field is a 1-based counter so the app can buffer and play in order
//   even if synthesis of later sentences finishes faster than earlier ones
//   (the server pipeline is sequential so frames always arrive in order).

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// ── Per-connection TTS options ─────────────────────────────────────────────────

// ttsConnOpts holds TTS settings for a single WebSocket connection.
// A nil value means TTS is disabled for this connection.
type ttsConnOpts struct {
	provider string
	voice    string
	format   string
}

// parseTtsConnOpts reads ?tts=1&tts_provider=X&tts_voice=Y&tts_format=Z and
// merges with the server-wide TtsConfig defaults.
// Returns nil if ?tts=1 (or ?tts=true) is not present in the URL.
func parseTtsConnOpts(r *http.Request, cfg *TtsConfig) *ttsConnOpts {
	q := r.URL.Query()
	if q.Get("tts") != "1" && q.Get("tts") != "true" {
		return nil
	}
	provider := canonicalProvider(q.Get("tts_provider"))
	if provider == "" {
		provider = cfg.Provider
	}
	voice := q.Get("tts_voice")
	if voice == "" {
		voice = cfg.Voice
	}
	if voice == "" {
		voice = defaultVoiceFor(provider)
	}
	format := q.Get("tts_format")
	if format == "" {
		format = cfg.Format
	}
	if format == "" {
		format = "mp3"
	}
	return &ttsConnOpts{provider: provider, voice: voice, format: format}
}

// defaultTtsConnOpts returns opts from server defaults (used by /ws/chat/tts
// and /pico/ws/tts where TTS is always on even without ?tts=1).
func defaultTtsConnOpts(r *http.Request, cfg *TtsConfig) *ttsConnOpts {
	if opts := parseTtsConnOpts(r, cfg); opts != nil {
		return opts
	}
	format := cfg.Format
	if format == "" {
		format = "mp3"
	}
	voice := cfg.Voice
	if voice == "" {
		voice = defaultVoiceFor(cfg.Provider)
	}
	return &ttsConnOpts{provider: cfg.Provider, voice: voice, format: format}
}

// ── Text extraction helpers ────────────────────────────────────────────────────

// extractZCFinalText returns the complete agent text from a zeroclaw agent→app
// frame if it is a final frame ("done" or non-streaming "message").
// Returns ("", false) for streaming chunk frames and all other types.
func extractZCFinalText(data []byte) (string, bool) {
	var m struct {
		Type         string `json:"type"`
		Content      string `json:"content"`
		FullResponse string `json:"full_response"`
	}
	if json.Unmarshal(data, &m) != nil {
		return "", false
	}
	switch m.Type {
	case "done":
		t := m.FullResponse
		if t == "" {
			t = m.Content
		}
		return t, t != ""
	case "message":
		return m.Content, m.Content != ""
	}
	return "", false
}

// extractPCFinalText returns the complete agent text from a picoclaw agent→app
// frame if it is a "message.create" that carries a speakable response.
// Skips thought messages (kind=="thought" or thought==true) and tool_call frames.
func extractPCFinalText(data []byte) (string, bool) {
	var m struct {
		Type    string `json:"type"`
		Payload struct {
			Content string `json:"content"`
			Kind    string `json:"kind"`
			Thought bool   `json:"thought"`
		} `json:"payload"`
	}
	if json.Unmarshal(data, &m) != nil {
		return "", false
	}
	if m.Type != "message.create" {
		return "", false
	}
	// Skip thought messages and tool invocations.
	if m.Payload.Thought || m.Payload.Kind == "thought" || m.Payload.Kind == "tool_calls" {
		return "", false
	}
	return m.Payload.Content, m.Payload.Content != ""
}

// ── TTS frame builders ─────────────────────────────────────────────────────────

// buildTtsAudioFrame synthesises text and returns a JSON-encoded "tts.audio"
// frame.  Returns nil if synthesis fails (non-fatal — caller skips the frame).
func buildTtsAudioFrame(text, provider, voice, format string, cfg *TtsConfig) []byte {
	fmt.Printf("%s[tts] synthesising %d chars via %s\n", prefixSYS(), len(text), provider)
	audio, outFmt, err := synthesize(text, provider, voice, format, cfg)
	if err != nil {
		fmt.Printf("%s[tts] synthesis FAILED: %v\n", prefixERR(), err)
		return nil
	}
	fmt.Printf("%s[tts] synthesis OK  %d bytes (%s)\n", prefixSYS(), len(audio), outFmt)
	frame, _ := json.Marshal(map[string]any{
		"type":      "tts.audio",
		"audio_b64": base64.StdEncoding.EncodeToString(audio),
		"format":    outFmt,
		"provider":  provider,
		"voice":     voice,
		"is_final":  true,
	})
	return frame
}

// buildTtsChunkFrame synthesises text and returns a JSON-encoded "tts.chunk"
// (isFinal=false) or "tts.audio" (isFinal=true) frame with a sequence number.
func buildTtsChunkFrame(text string, seq int, isFinal bool, opts *ttsConnOpts, cfg *TtsConfig) []byte {
	fmt.Printf("%s[tts] seq=%d synthesising %d chars via %s\n", prefixSYS(), seq, len(text), opts.provider)
	audio, outFmt, err := synthesize(text, opts.provider, opts.voice, opts.format, cfg)
	if err != nil {
		fmt.Printf("%s[tts] seq=%d synthesis FAILED: %v\n", prefixERR(), seq, err)
		return nil
	}
	fmt.Printf("%s[tts] seq=%d synthesis OK  %d bytes (%s)\n", prefixSYS(), seq, len(audio), outFmt)
	typ := "tts.chunk"
	if isFinal {
		typ = "tts.audio"
	}
	frame, _ := json.Marshal(map[string]any{
		"type":      typ,
		"seq":       seq,
		"text":      text,
		"audio_b64": base64.StdEncoding.EncodeToString(audio),
		"format":    outFmt,
		"provider":  opts.provider,
		"voice":     opts.voice,
		"is_final":  isFinal,
	})
	return frame
}

// ── Sentence splitter ──────────────────────────────────────────────────────────

// sentenceRe matches sentence-ending punctuation followed by whitespace/newline,
// or standalone Chinese/Japanese full-stop punctuation.
var sentenceRe = regexp.MustCompile(`[.!?]+[\s\n]+|[。！？]`)

// stripThink removes <think>…</think> blocks that may span multiple calls.
// inThink tracks whether we are currently inside a think block.
// Returns (cleaned text, updated inThink state).
func stripThink(s string, inThink bool) (string, bool) {
	var out strings.Builder
	for {
		if inThink {
			end := strings.Index(s, "</think>")
			if end == -1 {
				return out.String(), true // still inside block
			}
			s = s[end+len("</think>"):]
			inThink = false
		} else {
			start := strings.Index(s, "<think>")
			if start == -1 {
				out.WriteString(s)
				return out.String(), false
			}
			out.WriteString(s[:start])
			s = s[start+len("<think>"):]
			inThink = true
		}
	}
}

// splitSentences splits buf on sentence boundaries.
// Returns the list of complete sentences and the trailing partial (may be empty).
func splitSentences(buf string) (sentences []string, remainder string) {
	locs := sentenceRe.FindAllStringIndex(buf, -1)
	if len(locs) == 0 {
		return nil, buf
	}
	last := 0
	for _, loc := range locs {
		end := loc[1]
		s := strings.TrimSpace(buf[last:end])
		if s != "" {
			sentences = append(sentences, s)
		}
		last = end
	}
	remainder = buf[last:]
	return
}

// ── Streaming TTS pipeline (shared by both handlers) ──────────────────────────

// ttsPipeline is a sequential synthesis-and-send pipeline.
// Sentences are synthesised in submission order so audio frames always arrive
// in the correct playback order.
type ttsPipeline struct {
	sentCh   chan ttsWorkItem
	resultCh chan []byte
	wg       sync.WaitGroup
}

type ttsWorkItem struct {
	text    string
	seq     int
	isFinal bool
}

// newTtsPipeline creates and starts the synthesis + sender goroutines.
// sendFn is called for each synthesised audio frame (nil frames are skipped).
func newTtsPipeline(opts *ttsConnOpts, cfg *TtsConfig, sendFn func([]byte)) *ttsPipeline {
	p := &ttsPipeline{
		sentCh:   make(chan ttsWorkItem, 16),
		resultCh: make(chan []byte, 16),
	}
	// Synthesis goroutine — sequential to preserve frame order.
	p.wg.Add(1)
	go func() {
		defer p.wg.Done()
		defer close(p.resultCh)
		for work := range p.sentCh {
			f := buildTtsChunkFrame(work.text, work.seq, work.isFinal, opts, cfg)
			p.resultCh <- f // nil values propagated; sender skips them
		}
	}()
	// Sender goroutine.
	p.wg.Add(1)
	go func() {
		defer p.wg.Done()
		for f := range p.resultCh {
			if f != nil {
				sendFn(f)
			}
		}
	}()
	return p
}

// submit enqueues a sentence for synthesis.
func (p *ttsPipeline) submit(text string, seq int, isFinal bool) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	p.sentCh <- ttsWorkItem{text: text, seq: seq, isFinal: isFinal}
}

// close shuts down the pipeline and waits for all goroutines to finish.
func (p *ttsPipeline) close() {
	close(p.sentCh)
	p.wg.Wait()
}

// ── Streaming ZC TTS handler ───────────────────────────────────────────────────

// WS /ws/chat/tts — streaming TTS relay to zeroclaw.
//
// Acts like /ws/chat but additionally synthesises audio per sentence and sends
// tts.chunk / tts.audio frames alongside the normal text stream.
//
// The ?session_id=<id> query param is forwarded to the zeroclaw upstream so the
// agent can maintain conversation context across reconnects.
func (s *proxyServer) handleZCTTSStream(w http.ResponseWriter, r *http.Request) {
	if s.zcAuth == nil {
		http.Error(w, `{"type":"error","message":"zeroclaw not configured"}`, http.StatusServiceUnavailable)
		return
	}
	opts := defaultTtsConnOpts(r, s.ttsCfg)

	sessionID := r.URL.Query().Get("session_id")
	if sessionID == "" {
		sessionID = fmt.Sprintf("ztts-%d", time.Now().UnixNano())
	}

	appConn, err := s.upgrader.Upgrade(w, r, http.Header{
		"X-Proxy-Session-Id": []string{sessionID},
	})
	if err != nil {
		fmt.Printf("%s[zc-tts] upgrade: %v\n", prefixERR(), err)
		return
	}
	defer appConn.Close()

	// Dial zeroclaw.
	wsURL := s.zcAuth.wsURL(sessionID)
	h := http.Header{}
	if s.zcAuth.token != "" {
		h.Set("Authorization", "Bearer "+s.zcAuth.token)
	}
	d := &websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
		Subprotocols:     []string{"zeroclaw.v1"},
	}
	upConn, resp, err := d.Dial(wsURL, h)
	if resp != nil && resp.Body != nil {
		resp.Body.Close()
	}
	if err != nil {
		appConn.WriteMessage(websocket.TextMessage, //nolint:errcheck
			[]byte(`{"type":"error","message":"cannot connect to agent"}`))
		return
	}
	defer upConn.Close()
	fmt.Printf("%s[zc-tts] session=%s  provider=%s  voice=%s\n",
		prefixSYS(), sessionID, opts.provider, opts.voice)

	var appWrMu sync.Mutex
	sendToApp := func(data []byte) {
		appWrMu.Lock()
		appConn.WriteMessage(websocket.TextMessage, data) //nolint:errcheck
		appWrMu.Unlock()
	}

	pipe := newTtsPipeline(opts, s.ttsCfg, sendToApp)

	// App → upstream relay goroutine.
	go func() {
		for {
			mt, data, err := appConn.ReadMessage()
			if err != nil {
				return
			}
			upConn.WriteMessage(mt, data) //nolint:errcheck
		}
	}()

	// Upstream → app + TTS pipeline main loop.
	textBuf := ""
	seq := 0
	inThink := false // tracks whether we are inside a <think> block
	submitSentence := func(text string, isFinal bool) {
		if strings.TrimSpace(text) == "" {
			return
		}
		seq++
		fmt.Printf("%s[zc-tts] submit seq=%d isFinal=%v text=%q\n", prefixSYS(), seq, isFinal, text)
		pipe.submit(text, seq, isFinal)
	}

	for {
		_, data, err := upConn.ReadMessage()
		if err != nil {
			break
		}
		// Always forward the original text frame unchanged.
		sendToApp(data)

		var msg struct {
			Type         string `json:"type"`
			Content      string `json:"content"`
			FullResponse string `json:"full_response"`
		}
		if json.Unmarshal(data, &msg) != nil {
			continue
		}

		switch msg.Type {
		case "chunk":
			// Strip <think>...</think> blocks before accumulating.
			clean, newInThink := stripThink(msg.Content, inThink)
			inThink = newInThink
			if clean == "" {
				continue
			}
			textBuf += clean
			sentences, remainder := splitSentences(textBuf)
			textBuf = remainder
			for _, sent := range sentences {
				submitSentence(sent, false)
			}

		case "chunk_reset":
			// The agent reset its output buffer (e.g. after a think block).
			// Flush any partial sentence we accumulated so the pipeline can
			// finish, then clear state for the next segment.
			if rem := strings.TrimSpace(textBuf); rem != "" {
				submitSentence(rem, false)
			}
			textBuf = ""
			inThink = false

		case "done":
			// Flush any trailing partial sentence.
			if rem := strings.TrimSpace(textBuf); rem != "" {
				submitSentence(rem, true)
			} else if seq == 0 {
				// Agent replied without streaming — use full_response, strip think.
				full := msg.FullResponse
				if full == "" {
					full = msg.Content
				}
				clean, _ := stripThink(full, false)
				submitSentence(clean, true)
			}
			textBuf = ""

		case "message":
			// Non-streaming agent response — strip think just in case.
			clean, _ := stripThink(msg.Content, false)
			submitSentence(clean, true)
		}
	}

	pipe.close()
}

// ── Streaming PC TTS handler ───────────────────────────────────────────────────

// WS /pico/ws/tts — streaming TTS relay to picoclaw.
//
// Acts like /pico/ws but additionally synthesises audio per sentence and sends
// tts.chunk / tts.audio frames alongside the normal picoclaw message stream.
func (s *proxyServer) handlePCTTSStream(w http.ResponseWriter, r *http.Request) {
	if s.pcAuth == nil {
		http.Error(w, `{"type":"error","message":"picoclaw not configured"}`, http.StatusServiceUnavailable)
		return
	}
	opts := defaultTtsConnOpts(r, s.ttsCfg)

	sessionID := r.URL.Query().Get("session_id")
	if sessionID == "" {
		sessionID = fmt.Sprintf("ptts-%d", time.Now().UnixNano())
	}

	appConn, err := s.upgrader.Upgrade(w, r, http.Header{
		"X-Proxy-Session-Id": []string{sessionID},
	})
	if err != nil {
		fmt.Printf("%s[pc-tts] upgrade: %v\n", prefixERR(), err)
		return
	}
	defer appConn.Close()

	// Dial picoclaw.
	wsURL := appendSessionID(s.pcAuth.wsURL, sessionID)
	tok := s.pcAuth.token
	h := http.Header{}
	if tok != "" {
		h.Set("Authorization", "Bearer "+tok)
	}
	d := &websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
		Subprotocols:     []string{"token." + tok},
	}
	upConn, resp, err := d.Dial(wsURL, h)
	if resp != nil && resp.Body != nil {
		resp.Body.Close()
	}
	if err != nil {
		appConn.WriteMessage(websocket.TextMessage, //nolint:errcheck
			[]byte(`{"type":"error","message":"cannot connect to agent"}`))
		return
	}
	defer upConn.Close()
	fmt.Printf("%s[pc-tts] session=%s  provider=%s  voice=%s\n",
		prefixSYS(), sessionID, opts.provider, opts.voice)

	var appWrMu sync.Mutex
	sendToApp := func(data []byte) {
		appWrMu.Lock()
		appConn.WriteMessage(websocket.TextMessage, data) //nolint:errcheck
		appWrMu.Unlock()
	}

	pipe := newTtsPipeline(opts, s.ttsCfg, sendToApp)

	// App → upstream relay goroutine.
	go func() {
		for {
			mt, data, err := appConn.ReadMessage()
			if err != nil {
				return
			}
			upConn.WriteMessage(mt, data) //nolint:errcheck
		}
	}()

	// Upstream → app + TTS pipeline main loop.
	seq := 0
	submitSentence := func(text string, isFinal bool) {
		if strings.TrimSpace(text) == "" {
			return
		}
		seq++
		fmt.Printf("%s[pc-tts] submit seq=%d isFinal=%v text=%q\n", prefixSYS(), seq, isFinal, text)
		pipe.submit(text, seq, isFinal)
	}

	for {
		_, data, err := upConn.ReadMessage()
		if err != nil {
			break
		}
		sendToApp(data)

		var msg struct {
			Type    string `json:"type"`
			Payload struct {
				Content string `json:"content"`
				Kind    string `json:"kind"`
				Thought bool   `json:"thought"`
			} `json:"payload"`
		}
		if json.Unmarshal(data, &msg) != nil {
			continue
		}

		switch msg.Type {
		case "message.create":
			// Skip thought messages and tool invocations — only speak the
			// final visible response to the user.
			if msg.Payload.Thought || msg.Payload.Kind == "thought" || msg.Payload.Kind == "tool_calls" {
				continue
			}
			content := msg.Payload.Content
			if content == "" {
				continue
			}
			// Sentence-split the full reply (picoclaw doesn't stream tokens).
			sentences, remainder := splitSentences(content)
			n := len(sentences)
			for i, sent := range sentences {
				isFinal := i == n-1 && strings.TrimSpace(remainder) == ""
				submitSentence(sent, isFinal)
			}
			if rem := strings.TrimSpace(remainder); rem != "" {
				submitSentence(rem, true)
			}
		}
	}

	pipe.close()
}
