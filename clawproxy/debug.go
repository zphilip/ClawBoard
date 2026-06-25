package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type debugWriter struct {
	mu   sync.Mutex
	f    *os.File
	path string

	agent string
	key   string

	turnNum       int
	turnStart     time.Time
	frameCounts   map[string]int
	chunkChars    int
	chunkCharsPre int
	voiceInjected bool
	thinkBuf      strings.Builder
	inThink       bool
}

func newDebugWriter(baseDir, agent, sessionKey string) *debugWriter {
	if baseDir == "" {
		return nil
	}
	short := sessionKey
	if len(short) > 12 {
		short = short[:12]
	}
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
		f:           f,
		path:        path,
		agent:       agent,
		key:         short,
		frameCounts: make(map[string]int),
	}
	dw.write("# clawproxy debug log — %s session %s\n", agent, short)
	dw.write("# started %s\n\n", time.Now().Format(time.RFC3339))
	return dw
}

// ── Public logging methods ─────────────────────────────────────────────────

func (dw *debugWriter) logAppMessage(content string, voiceInjected bool) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()

	dw.startTurnLocked()
	dw.voiceInjected = voiceInjected

	dw.write("  App→Agent:\n")
	dw.write("    %s\n\n", strings.ReplaceAll(content, "\n", "\n    "))
	dw.syncLocked()
}

func (dw *debugWriter) logAgentFrame(frameType, content string, extra map[string]any) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()

	if dw.turnNum == 0 {
		dw.startTurnLocked()
	}
	dw.frameCounts[frameType]++

	switch frameType {
	case "thinking":
		chars := len([]rune(content))
		dw.write("  ── Thinking (%d chars) ──\n", chars)
		dw.write("    %s\n\n", strings.ReplaceAll(content, "\n", "\n    "))
		dw.syncLocked()

	case "chunk":
		dw.chunkChars += len([]rune(content))

	case "chunk_reset":
		dw.chunkCharsPre = dw.chunkChars
		dw.chunkChars = 0
		dw.write("  ── chunk_reset (streamed %d chars so far) ──\n\n", dw.chunkCharsPre)
		dw.syncLocked()

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
		dw.write("  ── Tool call #%d ──\n", seq)
		dw.write("    %s  %s\n\n", name, args)
		dw.syncLocked()

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
		status := fmt.Sprintf("%d chars", outputLen)
		isErr := strings.HasPrefix(outputPrev, "Error")
		if isErr {
			status = outputPrev
			if len(status) > 200 {
				status = status[:200] + "…"
			}
		}
		dw.write("  ── Tool result #%d ──\n", seq)
		dw.write("    %s → %s\n", name, status)
		if !isErr && len(outputPrev) > 0 {
			if len(outputPrev) > 200 {
				outputPrev = outputPrev[:200] + "…"
			}
			dw.write("      %s\n", strings.ReplaceAll(outputPrev, "\n", "\n      "))
		}
		dw.write("\n")
		dw.syncLocked()

	case "done":
		// Flush any remaining think content.
		if dw.thinkBuf.Len() > 0 {
			text := dw.thinkBuf.String()
			dw.write("  ── Thinking (%d chars, remainder) ──\n", len([]rune(text)))
			dw.write("    %s\n\n", strings.ReplaceAll(text, "\n", "\n    "))
			dw.thinkBuf.Reset()
			dw.inThink = false
		}
		// Response stats.
		dw.write("  ── Response ──\n")
		if dw.chunkCharsPre > 0 || dw.chunkChars > 0 {
			total := dw.chunkCharsPre + dw.chunkChars
			dw.write("    streamed: %d chars total\n", total)
		}
		if extra != nil {
			if rc, ok := extra["raw_chars"].(int); ok && rc > 0 {
				dw.write("    full_response: %d chars\n", rc)
			}
			if tc, ok := extra["think_chars_stripped"].(int); ok && tc > 0 {
				dw.write("    think stripped: %d chars\n", tc)
			}
			if cc, ok := extra["clean_chars"].(int); ok && cc > 0 {
				dw.write("    after cleanForTTS: %d chars\n", cc)
			}
			if sm, ok := extra["saw_markdown"].(bool); ok && sm {
				if mi, ok := extra["markdown_info"].([]string); ok {
					dw.write("    markdown: %s\n", strings.Join(mi, ", "))
				}
			}
		}
		// Frame count summary.
		dw.write("  ── Frame counts ──\n")
		order := []string{"thinking", "chunk", "chunk_reset", "tool_call", "tool_result", "done", "error"}
		var parts []string
		for _, t := range order {
			if n, ok := dw.frameCounts[t]; ok && n > 0 {
				parts = append(parts, fmt.Sprintf("%s=%d", t, n))
			}
		}
		dw.write("    %s\n\n", strings.Join(parts, "  "))
		dw.syncLocked()

		dw.turnNum = 0
		dw.frameCounts = make(map[string]int)
		dw.chunkChars = 0
		dw.chunkCharsPre = 0
	}
}

func (dw *debugWriter) logTTS(kind, text string, audioBytes int, d time.Duration, provider string, streaming bool, err error) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()

	mode := "single-shot"
	if streaming {
		mode = "streaming"
	}
	if len(text) > 80 {
		text = text[:80] + "…"
	}
	if err != nil {
		dw.write("  TTS %s: FAILED — %v\n\n", mode, err)
	} else {
		audioKB := float64(audioBytes) / 1024
		dw.write("  TTS %s: %q → %.0f KB  %s  [%s]\n\n", mode, text, audioKB, d.Round(time.Millisecond), provider)
	}
	dw.syncLocked()
}

func (dw *debugWriter) logWarning(msg string) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()
	dw.write("  ⚠ %s\n\n", msg)
	dw.syncLocked()
}

// logThinkChunk handles think content embedded in ZeroClaw chunk frames.
// Accumulates content between <think> and </think> tags across frames,
// and writes one block when </think> closes.
func (dw *debugWriter) logThinkChunk(content string) {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()

	s := content
	for {
		if dw.inThink {
			end := strings.Index(s, "</think>")
			if end == -1 {
				dw.thinkBuf.WriteString(s)
				return
			}
			dw.thinkBuf.WriteString(s[:end])
			s = s[end+len("</think>"):]
			dw.inThink = false
			// Flush accumulated think content.
			text := dw.thinkBuf.String()
			chars := len([]rune(text))
			dw.write("  ── Thinking (%d chars) ──\n", chars)
			dw.write("    %s\n\n", strings.ReplaceAll(text, "\n", "\n    "))
			dw.syncLocked()
			dw.thinkBuf.Reset()
		} else {
			start := strings.Index(s, "<think>")
			if start == -1 {
				return // no think block in this chunk
			}
			s = s[start+len("<think>"):]
			dw.inThink = true
		}
	}
}

// ── Internal helpers ────────────────────────────────────────────────────────

func (dw *debugWriter) startTurnLocked() {
	dw.turnNum++
	dw.turnStart = time.Now()
	dw.frameCounts = make(map[string]int)
	dw.chunkChars = 0
	dw.chunkCharsPre = 0
	dw.thinkBuf.Reset()
	dw.inThink = false
	dw.write("=== Turn %d | %s | voice: %v ===\n\n",
		dw.turnNum, dw.turnStart.Format("15:04:05"), dw.voiceInjected)
}

func (dw *debugWriter) write(format string, args ...any) {
	if dw.f == nil {
		return
	}
	fmt.Fprintf(dw.f, format, args...)
}

func (dw *debugWriter) syncLocked() {
	if dw.f != nil {
		dw.f.Sync() //nolint:errcheck
	}
}

func (dw *debugWriter) close() {
	if dw == nil {
		return
	}
	dw.mu.Lock()
	defer dw.mu.Unlock()
	if dw.f != nil {
		dw.write("# closed %s\n", time.Now().Format(time.RFC3339))
		dw.f.Close()
		dw.f = nil
	}
	fmt.Printf("%s[debug] session log: %s\n", prefixSYS(), dw.path)
}
