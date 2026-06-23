package main

// debug.go — per-conversation debug logging for clawproxy.
//
// When --debug-dir is set, clawproxy writes one log file per compat session,
// capturing thinking content, tool calls, TTS timing, and config warnings.
//
// File format is human-readable plain text.  Each turn is separated by a
// header line; within a turn, sections show app messages, agent frames,
// tool invocations, TTS synthesis events, and anomaly warnings.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// debugWriter accumulates per-turn data and writes it to a session log file.
// All methods are safe for concurrent use (the relay loop and TTS goroutines
// may call from different goroutines).
type debugWriter struct {
	mu   sync.Mutex
	f    *os.File
	path string

	// Session info
	agent string // "zc" or "pc"
	key   string // session key short hash

	// Per-turn accumulator
	turnNum       int
	turnStart     time.Time
	appMessages   []debugAppMsg
	frameCounts   map[string]int
	thinkContent  []debugThinkBlock
	chunkChars    int    // chars accumulated from chunk frames
	chunkCharsPre int    // chars before chunk_reset
	toolCalls     []debugToolCall
	toolResults   []debugToolResult
	ttsEvents     []debugTTSEvent
	warnings      []string
	voiceInjected bool
	doneContent   *debugDoneContent // set on "done" frame
}

type debugAppMsg struct {
	content       string
	voiceInjected bool
}

type debugThinkBlock struct {
	content string // previewed
	chars   int
}

type debugToolCall struct {
	seq  int
	name string
	args string // previewed
}

type debugToolResult struct {
	seq        int
	name       string
	outputLen  int
	outputPrev string // first 200 chars
}

type debugTTSEvent struct {
	kind      string // "notify_start", "notify_done", "synthesis"
	text      string // previewed
	audioBytes int
	duration  time.Duration
	provider  string
	streaming bool
	err       string // non-empty if failed
}

type debugDoneContent struct {
	rawChars     int
	cleanChars   int
	thinkChars   int // chars stripped by stripThink
	sawMarkdown  bool
	markdownInfo []string // e.g. "12 bare URLs", "3 bullet lists"
}

// newDebugWriter creates a debug log file for one compat session.
// Returns nil if baseDir is empty (debug disabled).
func newDebugWriter(baseDir, agent, sessionKey string) *debugWriter {
	if baseDir == "" {
		return nil
	}
	short := sessionKey
	if len(short) > 12 {
		short = short[:12]
	}
	// Sanitise for filename: replace '/' and other risky chars.
	short = strings.Map(func(r rune) rune {
		if r == '/' || r == '\\' || r == ':' || r == '*' || r == '?' || r == '"' || r == '<' || r == '>' || r == '|' {
			return '_'
		}
		return r
	}, short)
	name := fmt.Sprintf("%s-%s-%s.log", agent, short, time.Now().Format("2006-01-02-150405"))
	path := filepath.Join(baseDir, name)

	if err := os.MkdirAll(baseDir, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "[clawproxy] debug: cannot create dir %s: %v\n", baseDir, err)
		return nil
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[clawproxy] debug: cannot open %s: %v\n", path, err)
		return nil
	}

	dw := &debugWriter{
		f:          f,
		path:       path,
		agent:      agent,
		key:        short,
		frameCounts: make(map[string]int),
	}
	fmt.Fprintf(f, "# clawproxy debug log — %s session %s\n", agent, short)
	fmt.Fprintf(f, "# started %s\n\n", time.Now().Format(time.RFC3339))
	return dw
}

// logAppMessage records a user message forwarded to the agent.
func (dw *debugWriter) logAppMessage(content string, voiceInjected bool) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()

	if dw.turnNum == 0 {
		dw.startTurn()
	}
	dw.appMessages = append(dw.appMessages, debugAppMsg{content: content, voiceInjected: voiceInjected})
	dw.voiceInjected = voiceInjected
}

// logAgentFrame records one frame from the agent.  For streaming frames
// (chunk) only chars are accumulated; for significant frames (thinking,
// tool_call, tool_result) the content is captured.
func (dw *debugWriter) logAgentFrame(frameType, content string, extra map[string]any) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()

	if dw.turnNum == 0 {
		dw.startTurn()
	}
	dw.frameCounts[frameType]++

	switch frameType {
	case "thinking":
		chars := len([]rune(content))
		prev := content
		if len(prev) > 500 {
			prev = prev[:500] + "…"
		}
		dw.thinkContent = append(dw.thinkContent, debugThinkBlock{content: prev, chars: chars})

	case "chunk":
		dw.chunkChars += len([]rune(content))

	case "chunk_reset":
		dw.chunkCharsPre = dw.chunkChars
		dw.chunkChars = 0

	case "tool_call":
		seq := dw.frameCounts["tool_call"]
		name := ""
		args := ""
		if extra != nil {
			if n, ok := extra["name"].(string); ok {
				name = n
			}
			if a, ok := extra["args"].(string); ok {
				args = a
				if len(args) > 300 {
					args = args[:300] + "…"
				}
			}
		}
		dw.toolCalls = append(dw.toolCalls, debugToolCall{seq: seq, name: name, args: args})

	case "tool_result":
		seq := dw.frameCounts["tool_result"]
		name := ""
		outputLen := len(content)
		outputPrev := content
		if extra != nil {
			if n, ok := extra["name"].(string); ok {
				name = n
			}
		}
		if len(outputPrev) > 200 {
			outputPrev = outputPrev[:200] + "…"
		}
		dw.toolResults = append(dw.toolResults, debugToolResult{
			seq: seq, name: name, outputLen: outputLen, outputPrev: outputPrev,
		})

	case "done":
		// Capture done-content stats.
		dc := &debugDoneContent{}
		if extra != nil {
			if rc, ok := extra["raw_chars"].(int); ok {
				dc.rawChars = rc
			}
			if cc, ok := extra["clean_chars"].(int); ok {
				dc.cleanChars = cc
			}
			if tc, ok := extra["think_chars_stripped"].(int); ok {
				dc.thinkChars = tc
			}
			if sm, ok := extra["saw_markdown"].(bool); ok {
				dc.sawMarkdown = sm
			}
			if mi, ok := extra["markdown_info"].([]string); ok {
				dc.markdownInfo = mi
			}
		}
		dw.doneContent = dc

		// Flush the completed turn to disk.
		dw.flushTurnLocked()
	}
}

// logTTS records a TTS synthesis event.
func (dw *debugWriter) logTTS(kind, text string, audioBytes int, duration time.Duration, provider string, streaming bool, err error) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()

	if dw.turnNum == 0 {
		dw.startTurn()
	}
	errStr := ""
	if err != nil {
		errStr = err.Error()
	}
	if len(text) > 80 {
		text = text[:80] + "…"
	}
	dw.ttsEvents = append(dw.ttsEvents, debugTTSEvent{
		kind: kind, text: text, audioBytes: audioBytes,
		duration: duration, provider: provider, streaming: streaming, err: errStr,
	})
}

// logWarning records an anomaly or configuration hint for this turn.
func (dw *debugWriter) logWarning(msg string) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()
	dw.warnings = append(dw.warnings, msg)
}

// ── Internal helpers ──────────────────────────────────────────────────────────

func (dw *debugWriter) startTurn() {
	dw.turnNum++
	dw.turnStart = time.Now()
	dw.appMessages = nil
	dw.frameCounts = make(map[string]int)
	dw.thinkContent = nil
	dw.chunkChars = 0
	dw.chunkCharsPre = 0
	dw.toolCalls = nil
	dw.toolResults = nil
	dw.ttsEvents = nil
	dw.warnings = nil
	dw.doneContent = nil
}

func (dw *debugWriter) flushTurnLocked() {
	if dw.f == nil {
		return
	}
	w := dw.f

	header := fmt.Sprintf("=== Turn %d | %s | voice_instruction: %v ===",
		dw.turnNum, dw.turnStart.Format("15:04:05"), dw.voiceInjected)
	fmt.Fprintln(w, header)
	fmt.Fprintln(w, "")

	// ── App messages
	if len(dw.appMessages) > 0 {
		fmt.Fprintln(w, "  App→Agent:")
		for _, am := range dw.appMessages {
			prev := am.content
			if len(prev) > 1000 {
				prev = prev[:1000] + "…"
			}
			fmt.Fprintf(w, "    %s\n", strings.ReplaceAll(prev, "\n", "\n    "))
		}
		fmt.Fprintln(w, "")
	}

	// ── Thinking
	if len(dw.thinkContent) > 0 {
		fmt.Fprintln(w, "  ── Agent thinking ──")
		for _, t := range dw.thinkContent {
			fmt.Fprintf(w, "    %s\n", strings.ReplaceAll(t.content, "\n", "\n    "))
			fmt.Fprintf(w, "    (%d chars)\n", t.chars)
		}
		fmt.Fprintln(w, "")
	}

	// ── Tool calls
	if len(dw.toolCalls) > 0 {
		fmt.Fprintln(w, "  ── Tool calls ──")
		for _, tc := range dw.toolCalls {
			fmt.Fprintf(w, "    #%d  %s  %s\n", tc.seq, tc.name, tc.args)
		}
		fmt.Fprintln(w, "")
	}

	// ── Tool results
	if len(dw.toolResults) > 0 {
		fmt.Fprintln(w, "  ── Tool results ──")
		for _, tr := range dw.toolResults {
			status := fmt.Sprintf("%d chars", tr.outputLen)
			if strings.HasPrefix(tr.outputPrev, "HTTP error") || strings.HasPrefix(tr.outputPrev, "Error") {
				status = tr.outputPrev
			}
			fmt.Fprintf(w, "    #%d  %s  → %s\n", tr.seq, tr.name, status)
			if !strings.HasPrefix(tr.outputPrev, "HTTP error") && !strings.HasPrefix(tr.outputPrev, "Error") && len(tr.outputPrev) > 0 {
				fmt.Fprintf(w, "      %s\n", strings.ReplaceAll(tr.outputPrev, "\n", "\n      "))
			}
		}
		fmt.Fprintln(w, "")
	}

	// ── Agent response
	if dw.chunkCharsPre > 0 || dw.chunkChars > 0 || dw.doneContent != nil {
		fmt.Fprintln(w, "  ── Agent response ──")
		if dw.chunkCharsPre > 0 {
			fmt.Fprintf(w, "    streaming (before chunk_reset): %d chars\n", dw.chunkCharsPre)
		}
		if dw.chunkChars > 0 {
			fmt.Fprintf(w, "    streaming (after chunk_reset):  %d chars\n", dw.chunkChars)
		}
		if dw.doneContent != nil {
			dc := dw.doneContent
			fmt.Fprintf(w, "    full_response: %d chars\n", dc.rawChars)
			if dc.thinkChars > 0 {
				fmt.Fprintf(w, "    think stripped: %d chars\n", dc.thinkChars)
			}
			fmt.Fprintf(w, "    after cleanForTTS: %d chars\n", dc.cleanChars)
			if dc.sawMarkdown {
				fmt.Fprintf(w, "    markdown detected: %s\n", strings.Join(dc.markdownInfo, ", "))
			}
		}
		fmt.Fprintln(w, "")
	}

	// ── TTS synthesis
	if len(dw.ttsEvents) > 0 {
		fmt.Fprintln(w, "  ── TTS synthesis ──")
		for _, te := range dw.ttsEvents {
			switch te.kind {
			case "notify_start", "notify_done":
				fmt.Fprintf(w, "    %-14s %q   %s\n", te.kind, te.text, te.duration.Round(time.Millisecond))
			case "synthesis":
				mode := "single-shot"
				if te.streaming {
					mode = "streaming"
				}
				if te.err != "" {
					fmt.Fprintf(w, "    %-14s FAILED: %s\n", mode, te.err)
				} else {
					audioKB := float64(te.audioBytes) / 1024
					fmt.Fprintf(w, "    %-14s %d chars → %.0f KB  %s  [%s]\n",
						mode, len([]rune(te.text)), audioKB, te.duration.Round(time.Millisecond), te.provider)
				}
			}
		}
		fmt.Fprintln(w, "")
	}

	// ── Warnings
	if len(dw.warnings) > 0 {
		fmt.Fprintln(w, "  ── Warnings ──")
		for _, warn := range dw.warnings {
			fmt.Fprintf(w, "    ⚠ %s\n", warn)
		}
		fmt.Fprintln(w, "")
	}

	// ── Summary
	toolCount := len(dw.toolCalls)
	ttsChars := 0
	var ttsTotal time.Duration
	for _, te := range dw.ttsEvents {
		if te.kind == "synthesis" {
			ttsChars += len([]rune(te.text))
			ttsTotal += te.duration
		}
	}
	fmt.Fprintf(w, "  Total: %d tools | %d TTS chars | %d syntheses | %s TTS time\n\n",
		toolCount, ttsChars, len(dw.ttsEvents), ttsTotal.Round(time.Millisecond))

	// Reset for next turn.
	dw.startTurn()
}

// close flushes and closes the debug log file.
func (dw *debugWriter) close() {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()
	if dw.f != nil {
		fmt.Fprintf(dw.f, "# closed %s\n", time.Now().Format(time.RFC3339))
		dw.f.Close()
		dw.f = nil
	}
	fmt.Printf("%s[debug] session log written: %s\n", prefixSYS(), dw.path)
}
