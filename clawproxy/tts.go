package main

// tts.go — Text-to-Speech synthesis endpoints for clawproxy.
//
// Mirrors the provider set from zeroclaw-channels/src/tts.rs so that any
// agent text reply can be converted to audio on demand without requiring a
// full channel integration.
//
// ── New HTTP endpoints (registered by runProxy) ───────────────────────────────
//
//   POST /tts/synthesize
//     Request body (JSON):
//       {
//         "text":     "Hello world",   // required, max 4096 chars by default
//         "provider": "openai",        // optional — openai|elevenlabs|google|edge|piper
//         "voice":    "alloy",         // optional, provider-specific voice ID/name
//         "format":   "mp3"            // optional, provider-specific output format
//       }
//     Response (JSON, default):
//       {
//         "text":      "Hello world",
//         "audio_b64": "<base64 audio>",
//         "format":    "mp3",
//         "provider":  "openai",
//         "voice":     "alloy"
//       }
//     Response (raw audio, when Accept: audio/* or application/octet-stream):
//       Content-Type: audio/mpeg
//       X-Tts-Text, X-Tts-Provider, X-Tts-Voice headers
//       <raw audio bytes>
//
//   GET /tts/info
//     Returns configured provider, default voice/format, and per-provider defaults.
//
// ── Provider configuration ────────────────────────────────────────────────────
//
//   API keys are read from environment variables (same as zeroclaw):
//     OPENAI_API_KEY        — OpenAI TTS
//     ELEVENLABS_API_KEY    — ElevenLabs TTS
//     GOOGLE_TTS_API_KEY    — Google Cloud TTS
//
//   Or via clawproxy flags (see main.go):
//     --tts-provider  openai|elevenlabs|google|edge|piper  (default: openai)
//     --tts-voice     provider voice ID                     (default: alloy)
//     --tts-format    mp3|opus|wav|…                        (default: mp3)
//     --tts-api-key   override API key for the default provider
//     --tts-model     openai model name                     (default: tts-1)
//     --tts-piper-url Piper TTS server URL                  (default: http://127.0.0.1:5000/v1/audio/speech)
//     --tts-edge-bin  edge-tts binary name                  (default: edge-tts)

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// ── Config ────────────────────────────────────────────────────────────────────

// TtsConfig holds runtime TTS settings populated from flags + env vars.
type TtsConfig struct {
	Provider    string // default provider: openai|elevenlabs|google|edge|piper|minimax
	Voice       string // default voice ID
	Format      string // default output format
	MaxTextLen  int    // character limit (0 → 4096)
	OpenAIKey   string
	OpenAIModel string // default: tts-1
	ElevenKey   string
	GoogleKey   string
	PiperURL    string // default: http://127.0.0.1:5000/v1/audio/speech
	EdgeBin     string // default: edge-tts
	// MiniMax TTS (https://platform.minimaxi.com/docs/api-reference/speech-t2a-http)
	MiniMaxKey     string
	MiniMaxModel   string // default: speech-2.8-hd
	MiniMaxBaseURL string // default: https://api.minimaxi.com/v1/t2a_v2
	// F5-TTS local/remote server (https://github.com/SWivid/F5-TTS)
	F5TTSKey     string  // Bearer token (F5_TTS_API_KEY env or --tts-f5tts-key)
	F5TTSBaseURL string  // default: http://apicn.aiworm.cn:8010
	F5TTSSpeed   float64 // speech speed: 0.5–2.0; 0 means use default (1.0)
	// Qwen3-TTS (OpenAI-compatible /v1/audio/speech API)
	Qwen3Key     string        // Bearer token (QWEN3_TTS_API_KEY env or --tts-qwen3-key)
	Qwen3BaseURL string        // default: http://apicn.aiworm.cn:8011
	Qwen3Model   string        // model name: qwen3-tts, tts-1, tts-1-zh, … (default: qwen3-tts)
	Qwen3Speed   float64       // speech speed: 0.5–2.0; 0 means use default (1.0)
	Qwen3Timeout time.Duration // HTTP timeout for one synthesis call; 0 = use default (10 min)
	// MiMo-V2.5-TTS (Xiaomi; chat-completions-based TTS API)
	// https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5
	MiMoKey     string // MIMO_API_KEY env or --tts-mimo-key
	MiMoBaseURL string // default: https://api.xiaomimimo.com/v1
	MiMoModel   string // mimo-v2.5-tts | mimo-v2.5-tts-voicedesign | mimo-v2.5-tts-voiceclone
	// Streaming enables SSE-based streaming TTS for providers that support it.
	// When true, MiMo-V2.5-TTS uses stream=true and collects PCM16 chunks
	// progressively via SSE instead of waiting for the full WAV response.
	Streaming bool
	// FallbackProvider is the TTS provider to try if the primary provider fails.
	// Empty (default) means no fallback — synthesis errors are returned as-is.
	FallbackProvider string
}

// initTtsConfig builds a TtsConfig from CLI flags, env vars, and optionally a
// TOML config file (same path as zeroclaw's config.toml).
//
// Priority (highest → lowest):
//   1. CLI flags  (non-empty string passed in)
//   2. Env vars   (OPENAI_API_KEY, ELEVENLABS_API_KEY, …)
//   3. clawproxy's own config  (~/.clawproxy/config.toml)
//   4. zeroclaw config.toml [tts] section
//   5. picoclaw config.json model_list api_key fields
//   6. openclaw openclaw.json models/messages.tts providers
//   7. Built-in defaults
//
// Any configPath="" means auto-discover; pass "-" to disable that source.
func initTtsConfig(provider, voice, format, apiKey, model, piperURL, edgeBin,
	mmKey, mmModel, mmBaseURL,
	f5Key, f5BaseURL string, f5Speed float64,
	q3Key, q3BaseURL, q3Model string, q3Speed float64, q3Timeout time.Duration,
	mimoKey, mimoBaseURL, mimoModel string,
	streaming bool,
		fallback string,
	clawproxyConfigPath, configPath, picoConfigPath, openConfigPath string) *TtsConfig {
	cfg := &TtsConfig{
		Provider:       provider,
		Voice:          voice,
		Format:         format,
		OpenAIKey:      apiKey,
		OpenAIModel:    model,
		PiperURL:       piperURL,
		EdgeBin:        edgeBin,
		MiniMaxKey:     mmKey,
		MiniMaxModel:   mmModel,
		MiniMaxBaseURL: mmBaseURL,
		F5TTSKey:       f5Key,
		F5TTSBaseURL:   f5BaseURL,
		F5TTSSpeed:     f5Speed,
		Qwen3Key:       q3Key,
		Qwen3BaseURL:   q3BaseURL,
		Qwen3Model:     q3Model,
		Qwen3Speed:     q3Speed,
		Qwen3Timeout:   q3Timeout,
		MiMoKey:        mimoKey,
		MiMoBaseURL:    mimoBaseURL,
		MiMoModel:      mimoModel,
		Streaming:      streaming,
		FallbackProvider: fallback,
	}

	// Env var fallbacks (same names as zeroclaw uses).
	if cfg.OpenAIKey == "" {
		cfg.OpenAIKey = os.Getenv("OPENAI_API_KEY")
	}
	if cfg.ElevenKey == "" {
		cfg.ElevenKey = os.Getenv("ELEVENLABS_API_KEY")
	}
	if cfg.GoogleKey == "" {
		cfg.GoogleKey = os.Getenv("GOOGLE_TTS_API_KEY")
	}
	if cfg.MiniMaxKey == "" {
		cfg.MiniMaxKey = os.Getenv("MINIMAX_API_KEY")
	}
	if cfg.F5TTSKey == "" {
		cfg.F5TTSKey = os.Getenv("F5_TTS_API_KEY")
	}
	if cfg.Qwen3Key == "" {
		cfg.Qwen3Key = os.Getenv("QWEN3_TTS_API_KEY")
	}
	if cfg.MiMoKey == "" {
		cfg.MiMoKey = os.Getenv("MIMO_API_KEY")
	}

	// clawproxy's own config (highest-priority config file; overrides upstream daemons).
	if clawproxyConfigPath != "-" {
		if clawproxyConfigPath == "" {
			clawproxyConfigPath = discoverClawproxyConfigPath()
		}
		if fc, err := loadFileConfig(clawproxyConfigPath); err != nil {
			fmt.Fprintf(os.Stderr, "[clawproxy] warning: could not read clawproxy config %s: %v\n", clawproxyConfigPath, err)
		} else if fc != nil {
			applyFileTtsConfig(cfg, fc)
			fmt.Printf("%sLoaded TTS config from clawproxy config %s  streaming=%v  fallback=%s\n", prefixSYS(), clawproxyConfigPath, cfg.Streaming, cfg.FallbackProvider)
		}
	}

	// zeroclaw config.toml fallback.
	if configPath != "-" {
		if configPath == "" {
			configPath = discoverConfigPath()
		}
		if fc, err := loadFileConfig(configPath); err != nil {
			fmt.Fprintf(os.Stderr, "[clawproxy] warning: could not read config %s: %v\n", configPath, err)
		} else if fc != nil {
			applyFileTtsConfig(cfg, fc)
			if configPath != "" {
				fmt.Printf("%sLoaded TTS config from %s  streaming=%v\n", prefixSYS(), configPath, cfg.Streaming)
			}
		}
	}

	// picoclaw config.json fallback (model_list api_key entries).
	if picoConfigPath != "-" {
		if picoConfigPath == "" {
			picoConfigPath = discoverPicoClawConfigPath()
		}
		if pkeys := loadPicoClawTtsKeys(picoConfigPath); len(pkeys) > 0 {
			applyExternalTtsKeys(cfg, pkeys)
			fmt.Printf("%sLoaded TTS keys from picoclaw config %s\n", prefixSYS(), picoConfigPath)
		}
	}

	// openclaw openclaw.json fallback (models.providers / messages.tts.providers).
	if openConfigPath != "-" {
		if openConfigPath == "" {
			openConfigPath = discoverOpenClawConfigPath()
		}
		if okeys := loadOpenClawTtsKeys(openConfigPath); len(okeys) > 0 {
			applyExternalTtsKeys(cfg, okeys)
			fmt.Printf("%sLoaded TTS keys from openclaw config %s\n", prefixSYS(), openConfigPath)
		}
	}

	// Built-in defaults (lowest priority).
	if cfg.Provider == "" {
		cfg.Provider = "openai"
	}
	if cfg.Voice == "" {
		cfg.Voice = defaultVoiceFor(cfg.Provider)
	}
	if cfg.Format == "" {
		cfg.Format = "mp3"
	}
	if cfg.OpenAIModel == "" {
		cfg.OpenAIModel = "tts-1"
	}
	if cfg.PiperURL == "" {
		cfg.PiperURL = "http://127.0.0.1:5000/v1/audio/speech"
	}
	if cfg.EdgeBin == "" {
		cfg.EdgeBin = "edge-tts"
	}
	if cfg.MiniMaxModel == "" {
		cfg.MiniMaxModel = "speech-2.8-hd"
	}
	if cfg.MiniMaxBaseURL == "" {
		cfg.MiniMaxBaseURL = "https://api.minimaxi.com/v1/t2a_v2"
	}
	if cfg.F5TTSBaseURL == "" {
		cfg.F5TTSBaseURL = "http://apicn.aiworm.cn:8010"
	}
	if cfg.Qwen3BaseURL == "" {
		cfg.Qwen3BaseURL = "http://apicn.aiworm.cn:8011"
	}
	if cfg.Qwen3Model == "" {
		cfg.Qwen3Model = "qwen3-tts"
	}
	if cfg.Qwen3Timeout == 0 {
		cfg.Qwen3Timeout = 10 * time.Minute
	}
	if cfg.MiMoBaseURL == "" {
		cfg.MiMoBaseURL = "https://api.xiaomimimo.com/v1"
	}
	if cfg.MiMoModel == "" {
		cfg.MiMoModel = "mimo-v2.5-tts"
	}
	cfg.MaxTextLen = 4096

	// Warn at startup if the configured provider has no API key.
	switch cfg.Provider {
	case "mimotts":
		if cfg.MiMoKey == "" {
			fmt.Fprintf(os.Stderr, "[clawproxy] WARNING: TTS provider is mimotts but no API key found — set MIMO_API_KEY or --tts-mimo-key\n")
		}
	case "openai":
		if cfg.OpenAIKey == "" {
			fmt.Fprintf(os.Stderr, "[clawproxy] WARNING: TTS provider is openai but no API key found — set OPENAI_API_KEY or --tts-api-key\n")
		}
	case "elevenlabs":
		if cfg.ElevenKey == "" {
			fmt.Fprintf(os.Stderr, "[clawproxy] WARNING: TTS provider is elevenlabs but no API key found — set ELEVENLABS_API_KEY\n")
		}
	case "minimax":
		if cfg.MiniMaxKey == "" {
			fmt.Fprintf(os.Stderr, "[clawproxy] WARNING: TTS provider is minimax but no API key found — set MINIMAX_API_KEY\n")
		}
	}

	return cfg
}

// applyFileTtsConfig applies values from a parsed TOML [tts] section into cfg,
// skipping any field already set by a CLI flag or env var (non-empty string).
func applyFileTtsConfig(cfg *TtsConfig, fc *fileTtsSection) {
	if cfg.Provider == "" && fc.DefaultProvider != "" {
		cfg.Provider = canonicalProvider(fc.DefaultProvider)
	}
	if cfg.Voice == "" && fc.DefaultVoice != "" {
		cfg.Voice = fc.DefaultVoice
	}
	if cfg.Format == "" && fc.DefaultFormat != "" {
		cfg.Format = fc.DefaultFormat
	}
	if fc.MaxTextLength > 0 && cfg.MaxTextLen == 0 {
		cfg.MaxTextLen = fc.MaxTextLength
	}
	if fc.Streaming {
		cfg.Streaming = true
	}
	if cfg.FallbackProvider == "" && fc.FallbackProvider != "" {
		cfg.FallbackProvider = canonicalProvider(fc.FallbackProvider)
	}
	if fc.OpenAI != nil {
		if cfg.OpenAIKey == "" {
			cfg.OpenAIKey = fc.OpenAI.APIKey
		}
		if cfg.OpenAIModel == "" {
			cfg.OpenAIModel = fc.OpenAI.Model
		}
	}
	if fc.ElevenLabs != nil && cfg.ElevenKey == "" {
		cfg.ElevenKey = fc.ElevenLabs.APIKey
	}
	if fc.Google != nil && cfg.GoogleKey == "" {
		cfg.GoogleKey = fc.Google.APIKey
	}
	if fc.Edge != nil && cfg.EdgeBin == "" && fc.Edge.BinaryPath != "" {
		cfg.EdgeBin = fc.Edge.BinaryPath
	}
	if fc.Piper != nil && cfg.PiperURL == "" && fc.Piper.APIURL != "" {
		cfg.PiperURL = fc.Piper.APIURL
	}
	if fc.MiniMax != nil {
		if cfg.MiniMaxKey == "" {
			cfg.MiniMaxKey = fc.MiniMax.APIKey
		}
		if cfg.MiniMaxModel == "" {
			cfg.MiniMaxModel = fc.MiniMax.Model
		}
		if cfg.MiniMaxBaseURL == "" {
			cfg.MiniMaxBaseURL = fc.MiniMax.BaseURL
		}
	}
	if fc.F5TTS != nil {
		if cfg.F5TTSKey == "" {
			cfg.F5TTSKey = fc.F5TTS.APIKey
		}
		if cfg.F5TTSBaseURL == "" {
			cfg.F5TTSBaseURL = fc.F5TTS.BaseURL
		}
		if cfg.F5TTSSpeed == 0 && fc.F5TTS.Speed != 0 {
			cfg.F5TTSSpeed = fc.F5TTS.Speed
		}
	}
	if fc.Qwen3 != nil {
		if cfg.Qwen3Key == "" {
			cfg.Qwen3Key = fc.Qwen3.APIKey
		}
		if cfg.Qwen3BaseURL == "" {
			cfg.Qwen3BaseURL = fc.Qwen3.BaseURL
		}
		if cfg.Qwen3Model == "" {
			cfg.Qwen3Model = fc.Qwen3.Model
		}
		if cfg.Qwen3Speed == 0 && fc.Qwen3.Speed != 0 {
			cfg.Qwen3Speed = fc.Qwen3.Speed
		}
		if cfg.Qwen3Timeout == 0 && fc.Qwen3.TimeoutSecs > 0 {
			cfg.Qwen3Timeout = time.Duration(fc.Qwen3.TimeoutSecs) * time.Second
		}
	}
	if fc.MiMo != nil {
		if cfg.MiMoKey == "" {
			cfg.MiMoKey = fc.MiMo.APIKey
		}
		if cfg.MiMoBaseURL == "" {
			cfg.MiMoBaseURL = fc.MiMo.BaseURL
		}
		if cfg.MiMoModel == "" {
			cfg.MiMoModel = fc.MiMo.Model
		}
	}
}

// applyExternalTtsKeys copies provider API keys from a canonical-name→key map
// (produced by loadPicoClawTtsKeys or loadOpenClawTtsKeys) into cfg, skipping
// any key that is already set by a higher-priority source.
func applyExternalTtsKeys(cfg *TtsConfig, keys map[string]string) {
	if cfg.OpenAIKey == "" {
		cfg.OpenAIKey = keys["openai"]
	}
	if cfg.ElevenKey == "" {
		cfg.ElevenKey = keys["elevenlabs"]
	}
	if cfg.GoogleKey == "" {
		cfg.GoogleKey = keys["google"]
	}
	if cfg.MiniMaxKey == "" {
		cfg.MiniMaxKey = keys["minimax"]
	}
	if cfg.F5TTSKey == "" {
		cfg.F5TTSKey = keys["f5tts"]
	}
	if cfg.Qwen3Key == "" {
		cfg.Qwen3Key = keys["qwen3tts"]
	}
	if cfg.MiMoKey == "" {
		cfg.MiMoKey = keys["mimotts"]
	}
}

// canonicalProvider normalises a provider name or alias to the internal
// canonical identifier used in switch statements.
//
//	MiniMax:    "minimax-cn", "minimaxi", "minimax-io", … → "minimax"
//	OpenAI:     "gpt", "openai-compat", "gpt-4o", …      → "openai"
//	ElevenLabs: "elevenlabs-v2", …                        → "elevenlabs"
//	Google:     "google-cloud", "google-tts", …           → "google"
//	Edge:       "edge-tts"                                → "edge"
//	Piper:      "piper-tts"                               → "piper"
func canonicalProvider(name string) string {
	name = strings.ToLower(strings.TrimSpace(name))
	if isMiniMaxAlias(name) {
		return "minimax"
	}
	switch name {
	case "gpt", "openai-compat":
		return "openai"
	case "google-tts", "google-cloud", "google-cloud-tts", "gcloud":
		return "google"
	case "edge-tts":
		return "edge"
	case "piper-tts":
		return "piper"
	case "f5tts", "f5-tts", "f5tts-local", "f5tts_local", "f5_tts":
		return "f5tts"
	case "qwen3tts", "qwen3-tts", "qwen3_tts", "qwen-tts", "qwen3":
		return "qwen3tts"
	case "mimotts", "mimo-tts", "mimo_tts", "mimo", "xiaomimimo", "mimo-v2.5-tts",
		"mimo-v2.5-tts-voicedesign", "mimo-v2.5-tts-voiceclone":
		return "mimotts"
	}
	if strings.HasPrefix(name, "openai-") || strings.HasPrefix(name, "gpt-") {
		return "openai"
	}
	if name == "elevenlabs" || strings.HasPrefix(name, "elevenlabs-") {
		return "elevenlabs"
	}
	return name
}

func defaultVoiceFor(provider string) string {
	switch canonicalProvider(provider) {
	case "openai":
		return "alloy"
	case "elevenlabs":
		return "21m00Tcm4TlvDq8ikWAM" // ElevenLabs default voice ID
	case "google":
		return "en-US-Standard-A"
	case "edge":
		return "en-US-AriaNeural"
	case "piper":
		return "en_US-lessac-medium"
	case "minimax":
		return "male-qn-qingse" // MiniMax default; see voice list in platform docs
	case "f5tts":
		return "demo_speaker0" // F5-TTS default demo voice
	case "qwen3tts":
		return "Vivian" // Qwen3-TTS default voice
	case "mimotts":
		return "mimo_default" // MiMo default preset voice (冰糖 on CN cluster)
	default:
		return "alloy"
	}
}

// ── Request / Response types ──────────────────────────────────────────────────

type ttsRequest struct {
	Text     string `json:"text"`
	Provider string `json:"provider"`
	Voice    string `json:"voice"`
	Format   string `json:"format"`
}

type ttsResponse struct {
	Text     string `json:"text"`
	AudioB64 string `json:"audio_b64"`
	Format   string `json:"format"`
	Provider string `json:"provider"`
	Voice    string `json:"voice"`
}

type ttsInfoResponse struct {
	DefaultProvider string            `json:"default_provider"`
	DefaultVoice    string            `json:"default_voice"`
	DefaultFormat   string            `json:"default_format"`
	MaxTextLength   int               `json:"max_text_length"`
	Providers       map[string]any    `json:"providers"`
}

// ── HTTP handlers ─────────────────────────────────────────────────────────────

// POST /tts/synthesize — convert text to audio using a configured TTS provider.
func handleTTS(getCfg func() *TtsConfig) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cfg := getCfg()
		if r.Method != http.MethodPost {
			jsonError(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		body, err := io.ReadAll(io.LimitReader(r.Body, 128*1024))
		if err != nil {
			jsonError(w, "cannot read request body", http.StatusBadRequest)
			return
		}

		var req ttsRequest
		if err := json.Unmarshal(body, &req); err != nil {
			jsonError(w, "invalid JSON body", http.StatusBadRequest)
			return
		}

		text := strings.TrimSpace(req.Text)
		if text == "" {
			jsonError(w, "text is required", http.StatusBadRequest)
			return
		}
		maxLen := cfg.MaxTextLen
		if maxLen <= 0 {
			maxLen = 4096
		}
		if len([]rune(text)) > maxLen {
			jsonError(w, fmt.Sprintf("text too long (%d chars, max %d)", len([]rune(text)), maxLen), http.StatusBadRequest)
			return
		}

		// Resolve effective provider/voice/format (request overrides config default)
			provider := canonicalProvider(req.Provider)
		if provider == "" {
			provider = cfg.Provider
		}
		voice := req.Voice
		if voice == "" {
			voice = cfg.Voice
		}
		// If the default voice was set for a different provider, use the
		// per-provider default instead.
		if req.Voice == "" && req.Provider != "" && canonicalProvider(req.Provider) != cfg.Provider {
			voice = defaultVoiceFor(req.Provider)
		}
		format := req.Format
		if format == "" {
			format = cfg.Format
		}

		fmt.Printf("%sTTS synthesize  provider=%s  voice=%s  format=%s  len=%d\n",
			prefixSYS(), provider, voice, format, len([]rune(text)))

		audio, outFormat, err := synthesize(r.Context(), text, provider, voice, format, cfg)
		if err != nil {
			fmt.Printf("%sTTS error: %v\n", prefixERR(), err)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()}) //nolint:errcheck
			return
		}

		// If client explicitly wants raw audio, stream bytes.
		accept := r.Header.Get("Accept")
		if strings.HasPrefix(accept, "audio/") || accept == "application/octet-stream" {
			w.Header().Set("Content-Type", audioMIME(outFormat))
			w.Header().Set("X-Tts-Text", text)
			w.Header().Set("X-Tts-Provider", provider)
			w.Header().Set("X-Tts-Voice", voice)
			w.Header().Set("X-Tts-Format", outFormat)
			w.Write(audio) //nolint:errcheck
			return
		}

		// Default: JSON envelope with base64 audio.
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(ttsResponse{ //nolint:errcheck
			Text:     text,
			AudioB64: base64.StdEncoding.EncodeToString(audio),
			Format:   outFormat,
			Provider: provider,
			Voice:    voice,
		})
	}
}

// GET /tts/info — describe the configured TTS setup.
func handleTTSInfo(getCfg func() *TtsConfig) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cfg := getCfg()
		if r.Method != http.MethodGet {
			jsonError(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		providers := map[string]any{
			"openai": map[string]any{
				"configured": cfg.OpenAIKey != "",
				"model":      cfg.OpenAIModel,
				"voices":     []string{"alloy", "echo", "fable", "onyx", "nova", "shimmer"},
				"formats":    []string{"mp3", "opus", "aac", "flac", "wav", "pcm"},
			},
			"elevenlabs": map[string]any{
				"configured": cfg.ElevenKey != "",
				"voices":     []string{"(dynamic — use ElevenLabs voice IDs)"},
				"formats":    []string{"mp3", "pcm", "ulaw"},
			},
			"google": map[string]any{
				"configured": cfg.GoogleKey != "",
				"voices":     []string{"en-US-Standard-A", "en-US-Standard-B", "en-US-Standard-C", "en-US-Standard-D"},
				"formats":    []string{"mp3", "wav", "ogg"},
			},
			"edge": map[string]any{
				"configured": true, // subprocess, no key required
				"binary":     cfg.EdgeBin,
				"voices":     []string{"en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural", "en-GB-SoniaNeural"},
				"formats":    []string{"mp3"},
			},
			"piper": map[string]any{
				"configured": true, // local server, no key required
				"api_url":    cfg.PiperURL,
				"voices":     []string{"(dynamic — depends on installed Piper models)"},
				"formats":    []string{"mp3", "wav", "opus"},
			},
			"minimax": map[string]any{
				"configured": cfg.MiniMaxKey != "",
				"model":      cfg.MiniMaxModel,
				"base_url":   cfg.MiniMaxBaseURL,
				// Common built-in voices; custom cloned voices are account-specific.
				"voices": []string{
					"male-qn-qingse", "female-shaonv", "male-qn-jingying",
					"audiobook_male_1", "audiobook_female_1",
					"English_Ethan", "English_Olivia",
				},
				"formats": []string{"mp3", "wav", "flac"},
				"note":    "audio returned as hex-encoded string in data.audio field (decoded automatically)",
			},
			"f5tts": map[string]any{
				"configured": true, // no API key required for open deployments
				"base_url":   cfg.F5TTSBaseURL,
				"auth":       cfg.F5TTSKey != "",
				// Demo voices available without upload.
				"voices": []string{
					"demo_speaker0", "demo_speaker1", "demo_speaker2",
					"(custom — upload a reference .wav to obtain a voice name)",
				},
				"formats": []string{"wav"},
				"note":    "voice = ref_audio_orig; bare names (demo_speaker0) auto-prefixed as resources/{name}.wav",
			},
			"qwen3tts": map[string]any{
				"configured": true, // local server, no key required for open deployments
				"base_url":   cfg.Qwen3BaseURL,
				"model":      cfg.Qwen3Model,
				"auth":       cfg.Qwen3Key != "",
				// Built-in voices; use /v1/audio/voices to enumerate all.
				"voices":  []string{"Vivian", "Ryan", "aiden", "dylan", "eric", "ono_anna", "serena", "sohee", "uncle_fu"},
				"formats": []string{"mp3", "wav", "opus", "flac"},
				"note":    "OpenAI-compatible /v1/audio/speech; also accepts alloy/echo OpenAI voice aliases",
			},
			"mimotts": map[string]any{
				"configured": cfg.MiMoKey != "",
				"base_url":   cfg.MiMoBaseURL,
				"model":      cfg.MiMoModel,
				// Preset voices (mimo-v2.5-tts only).
				"voices": []string{
					"mimo_default", "冰糖", "茉莉", "苏打", "白桦",
					"Mia", "Chloe", "Milo", "Dean",
				},
				"formats": []string{"wav"},
				"models": []string{
					"mimo-v2.5-tts",             // preset voices + singing mode
					"mimo-v2.5-tts-voicedesign", // voice from text description (voice = style prompt in user msg)
					"mimo-v2.5-tts-voiceclone",  // voice from base64 audio sample (voice = data:audio/...;base64,...)
				},
				"note": "Uses chat/completions endpoint; audio in choices[0].message.audio.data (base64 WAV). " +
					"For voicedesign, set voice to a text description. " +
					"For voiceclone, set voice to data:audio/mpeg;base64,<b64>.",
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(ttsInfoResponse{ //nolint:errcheck
			DefaultProvider: cfg.Provider,
			DefaultVoice:    cfg.Voice,
			DefaultFormat:   cfg.Format,
			MaxTextLength:   cfg.MaxTextLen,
			Providers:       providers,
		})
	}
}

// jsonError writes a JSON {"error":"..."} response.
func jsonError(w http.ResponseWriter, msg string, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]string{"error": msg}) //nolint:errcheck
}

// audioMIME maps a format name to its MIME type.
func audioMIME(format string) string {
	switch format {
	case "mp3":
		return "audio/mpeg"
	case "opus":
		return "audio/ogg; codecs=opus"
	case "aac":
		return "audio/aac"
	case "flac":
		return "audio/flac"
	case "wav":
		return "audio/wav"
	case "pcm":
		return "audio/pcm"
	default:
		return "audio/mpeg"
	}
}

// ── Synthesis dispatcher ──────────────────────────────────────────────────────

// synthesize converts text to audio using the named provider.
// Returns: raw audio bytes, actual format string, error.
func synthesize(ctx context.Context, text, provider, voice, format string, cfg *TtsConfig) ([]byte, string, error) {
	audio, outFmt, err := synthesizeOne(ctx, text, provider, voice, format, cfg)
	if err != nil && cfg.FallbackProvider != "" && canonicalProvider(cfg.FallbackProvider) != canonicalProvider(provider) {
		fallback := canonicalProvider(cfg.FallbackProvider)
		fmt.Printf("%s[tts] primary provider %s failed (%v) — trying fallback %s\n",
			prefixSYS(), provider, err, fallback)
		// Primary voice is provider-specific; use fallback provider's default.
		fbVoice := defaultVoiceFor(fallback)
		audio2, outFmt2, err2 := synthesizeOne(ctx, text, fallback, fbVoice, format, cfg)
		if err2 != nil {
			fmt.Printf("%s[tts] fallback %s also failed: %v\n", prefixERR(), fallback, err2)
			return nil, "", fmt.Errorf("primary (%s) and fallback (%s) both failed: %w / %v", provider, fallback, err, err2)
		}
		fmt.Printf("%s[tts] fallback %s succeeded (%d bytes, %s)\n", prefixSYS(), fallback, len(audio2), outFmt2)
		return audio2, outFmt2, nil
	}
	return audio, outFmt, err
}

func synthesizeOne(ctx context.Context, text, provider, voice, format string, cfg *TtsConfig) ([]byte, string, error) {
	switch canonicalProvider(provider) {
	case "openai":
		return synthOpenAI(text, voice, format, cfg)
	case "elevenlabs":
		return synthElevenLabs(text, voice, cfg)
	case "google":
		return synthGoogle(text, voice, cfg)
	case "edge":
		return synthEdge(text, voice, cfg)
	case "piper":
		return synthPiper(text, voice, cfg)
	case "minimax":
		return synthMiniMax(text, voice, format, cfg)
	case "f5tts":
		return synthF5TTS(ctx, text, voice, cfg)
	case "qwen3tts":
		return synthQwen3TTS(ctx, text, voice, cfg)
	case "mimotts":
		if cfg.Streaming {
			return synthMiMoTTSStreaming(ctx, text, voice, cfg)
		}
		return synthMiMoTTS(ctx, text, voice, cfg)
	default:
		return nil, "", fmt.Errorf("unknown TTS provider %q (valid: openai, elevenlabs, google, edge, piper, minimax, f5tts, qwen3tts, mimotts)", provider)
	}
}

// ── OpenAI TTS ────────────────────────────────────────────────────────────────
// Mirrors: zeroclaw-channels/src/tts.rs OpenAiTtsProvider::synthesize
// Endpoint: POST https://api.openai.com/v1/audio/speech

func synthOpenAI(text, voice, format string, cfg *TtsConfig) ([]byte, string, error) {
	if cfg.OpenAIKey == "" {
		return nil, "", fmt.Errorf("OpenAI TTS: no API key — set OPENAI_API_KEY or --tts-api-key")
	}
	if voice == "" {
		voice = "alloy"
	}
	if format == "" {
		format = "mp3"
	}

	payload := map[string]any{
		"model":           cfg.OpenAIModel,
		"input":           text,
		"voice":           voice,
		"response_format": format,
		"speed":           1.0,
	}
	bodyJSON, _ := json.Marshal(payload)

	req, err := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/audio/speech", bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("OpenAI TTS: build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+cfg.OpenAIKey)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("OpenAI TTS: request failed: %w", err)
	}
	defer resp.Body.Close()

	audio, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		var errBody map[string]any
		json.Unmarshal(audio, &errBody) //nolint:errcheck
		msg := "unknown"
		if e, ok := errBody["error"].(map[string]any); ok {
			if m, ok := e["message"].(string); ok {
				msg = m
			}
		}
		return nil, "", fmt.Errorf("OpenAI TTS error (%d): %s", resp.StatusCode, msg)
	}
	return audio, format, nil
}

// ── ElevenLabs TTS ────────────────────────────────────────────────────────────
// Mirrors: zeroclaw-channels/src/tts.rs ElevenLabsTtsProvider::synthesize
// Endpoint: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}

// elevenLabsVoiceRe validates ElevenLabs voice IDs (same rule as the Rust code).
var elevenLabsVoiceRe = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)

func synthElevenLabs(text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	if cfg.ElevenKey == "" {
		return nil, "", fmt.Errorf("ElevenLabs TTS: no API key — set ELEVENLABS_API_KEY or --tts-api-key")
	}
	if voice == "" {
		voice = "21m00Tcm4TlvDq8ikWAM"
	}
	if !elevenLabsVoiceRe.MatchString(voice) {
		return nil, "", fmt.Errorf("ElevenLabs TTS: voice ID contains invalid characters: %s", voice)
	}

	url := fmt.Sprintf("https://api.elevenlabs.io/v1/text-to-speech/%s", voice)
	payload := map[string]any{
		"text":     text,
		"model_id": "eleven_monolingual_v1",
		"voice_settings": map[string]any{
			"stability":        0.5,
			"similarity_boost": 0.5,
		},
	}
	bodyJSON, _ := json.Marshal(payload)

	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("ElevenLabs TTS: build request: %w", err)
	}
	req.Header.Set("xi-api-key", cfg.ElevenKey)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("ElevenLabs TTS: request failed: %w", err)
	}
	defer resp.Body.Close()

	audio, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		var errBody map[string]any
		json.Unmarshal(audio, &errBody) //nolint:errcheck
		msg := "unknown"
		if d, ok := errBody["detail"].(map[string]any); ok {
			if m, ok := d["message"].(string); ok {
				msg = m
			}
		} else if m, ok := errBody["detail"].(string); ok {
			msg = m
		}
		return nil, "", fmt.Errorf("ElevenLabs TTS error (%d): %s", resp.StatusCode, msg)
	}
	return audio, "mp3", nil
}

// ── Google Cloud TTS ──────────────────────────────────────────────────────────
// Mirrors: zeroclaw-channels/src/tts.rs GoogleTtsProvider::synthesize
// Endpoint: POST https://texttospeech.googleapis.com/v1/text:synthesize
// Note: Google returns base64-encoded audio in the response body — we decode it.

func synthGoogle(text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	if cfg.GoogleKey == "" {
		return nil, "", fmt.Errorf("Google TTS: no API key — set GOOGLE_TTS_API_KEY or --tts-api-key")
	}
	if voice == "" {
		voice = "en-US-Standard-A"
	}

	// Infer language code from voice name (e.g. "en-US-Standard-A" → "en-US")
	langCode := "en-US"
	parts := strings.SplitN(voice, "-", 3)
	if len(parts) >= 2 {
		langCode = parts[0] + "-" + parts[1]
	}

	payload := map[string]any{
		"input": map[string]any{"text": text},
		"voice": map[string]any{
			"languageCode": langCode,
			"name":         voice,
		},
		"audioConfig": map[string]any{
			"audioEncoding": "MP3",
		},
	}
	bodyJSON, _ := json.Marshal(payload)

	req, err := http.NewRequest(http.MethodPost, "https://texttospeech.googleapis.com/v1/text:synthesize", bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("Google TTS: build request: %w", err)
	}
	req.Header.Set("x-goog-api-key", cfg.GoogleKey)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("Google TTS: request failed: %w", err)
	}
	defer resp.Body.Close()

	var respBody map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&respBody); err != nil {
		return nil, "", fmt.Errorf("Google TTS: invalid response JSON")
	}
	if resp.StatusCode >= 400 {
		msg := "unknown"
		if e, ok := respBody["error"].(map[string]any); ok {
			if m, ok := e["message"].(string); ok {
				msg = m
			}
		}
		return nil, "", fmt.Errorf("Google TTS error (%d): %s", resp.StatusCode, msg)
	}

	// Google returns base64-encoded audio in "audioContent" field.
	b64, ok := respBody["audioContent"].(string)
	if !ok {
		return nil, "", fmt.Errorf("Google TTS: response missing 'audioContent' field")
	}
	audio, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return nil, "", fmt.Errorf("Google TTS: base64 decode: %w", err)
	}
	return audio, "mp3", nil
}

// ── Edge TTS (subprocess) ─────────────────────────────────────────────────────
// Mirrors: zeroclaw-channels/src/tts.rs EdgeTtsProvider::synthesize
// Uses the `edge-tts` CLI subprocess; writes audio to a temp file then reads it.

// edgeTTSAllowedBinaries matches the allowlist in the Rust source.
var edgeTTSAllowedBinaries = map[string]bool{
	"edge-tts":      true,
	"edge-playback": true,
}

func synthEdge(text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	bin := cfg.EdgeBin
	if bin == "" {
		bin = "edge-tts"
	}
	// Security: only allow bare command names matching the allowlist
	// (prevents path traversal like /tmp/malicious/edge-tts passing the check).
	if strings.ContainsAny(bin, "/\\") {
		return nil, "", fmt.Errorf("edge-tts: binary path must not contain path separators, got: %s", bin)
	}
	if !edgeTTSAllowedBinaries[bin] {
		return nil, "", fmt.Errorf("edge-tts: binary must be 'edge-tts' or 'edge-playback', got: %s", bin)
	}
	if voice == "" {
		voice = "en-US-AriaNeural"
	}

	tmpFile := filepath.Join(os.TempDir(), fmt.Sprintf("clawproxy_tts_%d.mp3", time.Now().UnixNano()))
	defer os.Remove(tmpFile) //nolint:errcheck

	cmd := exec.Command(bin, "--text", text, "--voice", voice, "--write-media", tmpFile)
	if out, err := cmd.CombinedOutput(); err != nil {
		return nil, "", fmt.Errorf("edge-tts error: %s", strings.TrimSpace(string(out)))
	}

	audio, err := os.ReadFile(tmpFile)
	if err != nil {
		return nil, "", fmt.Errorf("edge-tts: cannot read output file: %w", err)
	}
	return audio, "mp3", nil
}

// ── Piper TTS (local, OpenAI-compatible) ──────────────────────────────────────
// Mirrors: zeroclaw-channels/src/tts.rs PiperTtsProvider::synthesize
// Piper runs a local HTTP server with the OpenAI /v1/audio/speech endpoint.

func synthPiper(text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	url := cfg.PiperURL
	if url == "" {
		url = "http://127.0.0.1:5000/v1/audio/speech"
	}
	payload := map[string]any{
		"model": "tts-1",
		"input": text,
		"voice": voice,
	}
	bodyJSON, _ := json.Marshal(payload)

	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Post(url, "application/json", bytes.NewReader(bodyJSON)) //nolint:gosec
	if err != nil {
		return nil, "", fmt.Errorf("Piper TTS: request failed: %w", err)
	}
	defer resp.Body.Close()

	audio, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, "", fmt.Errorf("Piper TTS error (%d)", resp.StatusCode)
	}
	return audio, "wav", nil
}

// ── MiniMax TTS ───────────────────────────────────────────────────────────────
// API docs: https://platform.minimaxi.com/docs/api-reference/speech-t2a-http
//
// Key differences from other providers that require custom handling:
//
//   1. Audio encoding: response body is JSON; audio lives in data.audio as a
//      HEX string (not raw bytes, not base64) — must be decoded with hex.DecodeString.
//
//   2. Voice field: not a top-level parameter; lives in voice_setting.voice_id.
//
//   3. Format field: not top-level; lives in audio_setting.format.
//
//   4. Model: required field, unlike other providers.
//
//   5. Errors: indicated by base_resp.status_code (non-zero = error), not HTTP status.
//
//   6. Dual base URL:
//        Primary: https://api.minimaxi.com/v1/t2a_v2
//        Backup:  https://api-bj.minimaxi.com/v1/t2a_v2
//      We use the primary; override with --tts-minimax-url or MINIMAX_BASE_URL.

func synthMiniMax(text, voice, format string, cfg *TtsConfig) ([]byte, string, error) {
	if cfg.MiniMaxKey == "" {
		return nil, "", fmt.Errorf("MiniMax TTS: no API key — set MINIMAX_API_KEY or --tts-minimax-key")
	}
	if voice == "" {
		voice = "male-qn-qingse"
	}
	if format == "" {
		format = "mp3"
	}
	// MiniMax only supports mp3, wav, flac for non-streaming.
	switch format {
	case "mp3", "wav", "flac":
		// ok
	default:
		return nil, "", fmt.Errorf("MiniMax TTS: unsupported format %q (valid: mp3, wav, flac)", format)
	}

	model := cfg.MiniMaxModel
	if model == "" {
		model = "speech-2.8-hd"
	}
	baseURL := cfg.MiniMaxBaseURL
	if baseURL == "" {
		baseURL = "https://api.minimaxi.com/v1/t2a_v2"
	}

	payload := map[string]any{
		"model":         model,
		"text":          text,
		"stream":        false,
		"output_format": "hex", // always hex so we get raw bytes in JSON
		"voice_setting": map[string]any{
			"voice_id": voice,
			"speed":    1,
			"vol":      1,
			"pitch":    0,
		},
		"audio_setting": map[string]any{
			"format":  format,
			"channel": 1,
		},
	}
	bodyJSON, _ := json.Marshal(payload)

	req, err := http.NewRequest(http.MethodPost, baseURL, bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("MiniMax TTS: build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+cfg.MiniMaxKey)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 120 * time.Second} // MiniMax can be slower
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("MiniMax TTS: request failed: %w", err)
	}
	defer resp.Body.Close()

	var respBody map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&respBody); err != nil {
		return nil, "", fmt.Errorf("MiniMax TTS: invalid response JSON")
	}

	// Check HTTP-level errors first.
	if resp.StatusCode >= 400 {
		return nil, "", fmt.Errorf("MiniMax TTS: HTTP %d", resp.StatusCode)
	}

	// Check MiniMax application-level errors (base_resp.status_code != 0).
	if br, ok := respBody["base_resp"].(map[string]any); ok {
		if code, _ := br["status_code"].(float64); code != 0 {
			msg, _ := br["status_msg"].(string)
			return nil, "", fmt.Errorf("MiniMax TTS error (code %d): %s", int(code), msg)
		}
	}

	// Extract hex-encoded audio from data.audio.
	dataObj, ok := respBody["data"].(map[string]any)
	if !ok {
		return nil, "", fmt.Errorf("MiniMax TTS: response missing 'data' field")
	}
	audioHex, ok := dataObj["audio"].(string)
	if !ok || audioHex == "" {
		return nil, "", fmt.Errorf("MiniMax TTS: response missing 'data.audio' field")
	}

	// Decode hex → raw audio bytes.
	audio, err := hex.DecodeString(audioHex)
	if err != nil {
		return nil, "", fmt.Errorf("MiniMax TTS: hex decode failed: %w", err)
	}
	return audio, format, nil
}

// ── F5-TTS (local / self-hosted) ──────────────────────────────────────────────
// Reference: voice_cloning.py + test_voice_cloning.py
// API (F5-TTS server, default port 8010):
//
//   POST /voice-clone/synthesize_speech[?need_credit=false]
//     JSON body: { "ref_audio_orig": "<voice>", "gen_text": "...", "ref_text": "",
//                  "model": "F5TTS_v1_Base", "speed": 1.0, ... }
//     Returns: { "task_id": "..." }
//
//   GET /voice-clone/status?task_id=<id>
//     Returns: { "status": "processing"|"completed"|"failed", "audio_urls": [...], ... }
//
//   GET /voice-clone/result?task_id=<id>
//     Returns full result JSON if status endpoint doesn't embed audio directly.
//
// Voice name conventions:
//   - Bare names (e.g. "demo_speaker0") → auto-prefixed as "resources/demo_speaker0.wav"
//   - Names already containing "/" or ending in ".wav" → passed through unchanged
//   - Uploaded custom voice names (returned by /voice-clone/upload_audio) → passed as-is

// synthQwen3TTS calls the OpenAI-compatible /v1/audio/speech endpoint on a
// Qwen3-TTS server.  The response is raw audio bytes (synchronous, no polling).
//
// Supported voices: Vivian, Ryan, aiden, dylan, eric, ono_anna, serena, sohee, uncle_fu
// (and OpenAI aliases: alloy→Vivian, echo→Ryan, …)
// Supported models: qwen3-tts, tts-1, tts-1-zh, tts-1-<lang>, …
func synthQwen3TTS(ctx context.Context, text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	baseURL := cfg.Qwen3BaseURL
	if baseURL == "" {
		baseURL = "http://apicn.aiworm.cn:8011"
	}
	if voice == "" {
		voice = "Vivian"
	}
	modelName := cfg.Qwen3Model
	if modelName == "" {
		modelName = "qwen3-tts"
	}
	speed := cfg.Qwen3Speed
	if speed <= 0 {
		speed = 1.0
	}

	payload := map[string]any{
		"model":           modelName,
		"input":           text,
		"voice":           voice,
		"response_format": "mp3",
		"speed":           speed,
	}
	bodyJSON, _ := json.Marshal(payload)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/v1/audio/speech", bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("qwen3-tts: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "*/*")
	if cfg.Qwen3Key != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.Qwen3Key)
	}

	client := &http.Client{Timeout: cfg.Qwen3Timeout} // configurable; default 10 min
	if client.Timeout <= 0 {
		client.Timeout = 10 * time.Minute
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("qwen3-tts: request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return nil, "", fmt.Errorf("qwen3-tts: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	audio, _ := io.ReadAll(resp.Body)
	if len(audio) == 0 {
		return nil, "", fmt.Errorf("qwen3-tts: empty response body")
	}

	// Infer format from Content-Type header.
	format := "mp3"
	ct := resp.Header.Get("Content-Type")
	switch {
	case strings.Contains(ct, "wav"):
		format = "wav"
	case strings.Contains(ct, "flac"):
		format = "flac"
	case strings.Contains(ct, "opus"):
		format = "opus"
	}
	fmt.Printf("%s[qwen3tts] synthesised %d chars → %d bytes (%s)  voice=%s  model=%s\n",
		prefixSYS(), len([]rune(text)), len(audio), format, voice, modelName)
	return audio, format, nil
}

func synthF5TTS(ctx context.Context, text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	baseURL := cfg.F5TTSBaseURL
	if baseURL == "" {
		baseURL = "http://apicn.aiworm.cn:8010"
	}
	// Resolve voice → ref_audio_orig.
	refAudio := voice
	if refAudio == "" {
		refAudio = "demo_speaker0"
	}
	// Auto-prefix bare demo/custom names with the server's resources/ path.
	if !strings.Contains(refAudio, "/") && !strings.HasSuffix(refAudio, ".wav") {
		refAudio = "resources/" + refAudio + ".wav"
	}

	speed := cfg.F5TTSSpeed
	if speed <= 0 {
		speed = 1.0
	}
	payload := map[string]any{
		"ref_audio_orig":      refAudio,
		"gen_text":            text,
		"ref_text":            "",
		"model":               "F5TTS_v1_Base",
		"remove_silence":      false,
		"seed":                -1,
		"cross_fade_duration": 0.15,
		"nfe_step":            32,
		"speed":               speed,
	}
	bodyJSON, _ := json.Marshal(payload)

	// Submit synthesis task.
	submitURL := baseURL + "/voice-clone/synthesize_speech?need_credit=false"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, submitURL, bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("F5-TTS: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if cfg.F5TTSKey != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.F5TTSKey)
	}

	submitClient := &http.Client{Timeout: 30 * time.Second}
	sresp, err := submitClient.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("F5-TTS: submit failed: %w", err)
	}
	defer sresp.Body.Close()

	if sresp.StatusCode >= 400 {
		body, _ := io.ReadAll(sresp.Body)
		return nil, "", fmt.Errorf("F5-TTS: submit HTTP %d: %s", sresp.StatusCode, strings.TrimSpace(string(body)))
	}

	var submitResult map[string]any
	if err := json.NewDecoder(sresp.Body).Decode(&submitResult); err != nil {
		return nil, "", fmt.Errorf("F5-TTS: invalid submit response JSON")
	}
	taskID, _ := submitResult["task_id"].(string)
	if taskID == "" {
		return nil, "", fmt.Errorf("F5-TTS: submit response missing task_id")
	}
	fmt.Printf("%s[f5tts] task_id=%s  ref=%s\n", prefixSYS(), taskID, refAudio)

	// Poll for completion (up to 10 minutes; F5-TTS can be slow on CPU).
	// The poll loop respects ctx cancellation so the upstream request is
	// abandoned immediately when the HTTP client disconnects.
	pollClient := &http.Client{Timeout: 15 * time.Second}
	pollHeaders := map[string]string{"Accept": "application/json"}
	if cfg.F5TTSKey != "" {
		pollHeaders["Authorization"] = "Bearer " + cfg.F5TTSKey
	}

	deadline := time.Now().Add(10 * time.Minute)
	for time.Now().Before(deadline) {
		// Respect context cancellation between polls.
		select {
		case <-ctx.Done():
			return nil, "", fmt.Errorf("F5-TTS: cancelled (task_id=%s): %w", taskID, ctx.Err())
		case <-time.After(2 * time.Second):
		}

		statusURL := fmt.Sprintf("%s/voice-clone/status/%s", baseURL, taskID)
		sreq, _ := http.NewRequestWithContext(ctx, http.MethodGet, statusURL, nil)
		for k, v := range pollHeaders {
			sreq.Header.Set(k, v)
		}
		pr, err := pollClient.Do(sreq)
		if err != nil {
			if ctx.Err() != nil {
				return nil, "", fmt.Errorf("F5-TTS: cancelled (task_id=%s): %w", taskID, ctx.Err())
			}
			continue // transient network error — keep polling
		}
		var statusResp map[string]any
		json.NewDecoder(pr.Body).Decode(&statusResp) //nolint:errcheck
		pr.Body.Close()

		// Response: {success: bool, task: {status, progress, ...}, message: str}
		// Fall back to flat map if server uses a different layout.
		taskInfo, _ := statusResp["task"].(map[string]any)
		if taskInfo == nil {
			taskInfo = statusResp
		}
		status, _ := taskInfo["status"].(string)
		progress, _ := taskInfo["progress"].(float64)
		fmt.Printf("%s[f5tts] task_id=%s  status=%s  progress=%d%%\n", prefixSYS(), taskID, status, int(progress*100))

		switch status {
		case "failed", "cancelled", "timeout":
			errMsg, _ := taskInfo["error_message"].(string)
			if errMsg == "" {
				errMsg, _ = statusResp["message"].(string)
			}
			return nil, "", fmt.Errorf("F5-TTS: synthesis %s: %s", status, errMsg)
		case "succeeded", "completed":
			// Try to extract audio: first from inline fields in status body,
			// then fetch /result/{task_id} which returns raw audio bytes.
			if audio, fmt, err := f5ttsExtractAudio(ctx, taskInfo, baseURL, taskID, pollHeaders, pollClient); err == nil {
				return audio, fmt, nil
			}
		}
		// "processing" / "queued" / unknown — keep polling.
	}
	return nil, "", fmt.Errorf("F5-TTS: synthesis timed out after 10 minutes (task_id=%s)", taskID)
}

// f5ttsExtractAudio pulls raw audio bytes out of the status/result response.
// It tries three extraction paths in priority order:
//  1. audio_urls[0]  — download from URL
//  2. audio_b64      — base64-decode inline audio
//  3. /result endpoint — re-fetch and repeat
func f5ttsExtractAudio(ctx context.Context, body map[string]any, baseURL, taskID string,
	headers map[string]string, client *http.Client) ([]byte, string, error) {

	// 1. audio_urls list.
	if urls, ok := body["audio_urls"].([]any); ok && len(urls) > 0 {
		if u, ok := urls[0].(string); ok && u != "" {
			return f5ttsDownload(ctx, u, headers, client)
		}
	}
	// 2. Inline base64 audio.
	if b64, ok := body["audio_b64"].(string); ok && b64 != "" {
		audio, err := base64.StdEncoding.DecodeString(b64)
		if err != nil {
			return nil, "", fmt.Errorf("F5-TTS: base64 decode: %w", err)
		}
		return audio, "wav", nil
	}
	// 3. /result/{task_id} endpoint — returns raw audio bytes.
	resultURL := fmt.Sprintf("%s/voice-clone/result/%s", baseURL, taskID)
	rreq, _ := http.NewRequestWithContext(ctx, http.MethodGet, resultURL, nil)
	for k, v := range headers {
		rreq.Header.Set(k, v)
	}
	rr, err := client.Do(rreq)
	if err != nil {
		return nil, "", fmt.Errorf("F5-TTS: result fetch failed: %w", err)
	}
	defer rr.Body.Close()
	if rr.StatusCode >= 400 {
		return nil, "", fmt.Errorf("F5-TTS: result HTTP %d", rr.StatusCode)
	}
	audio, _ := io.ReadAll(rr.Body)
	format := "wav"
	ct := rr.Header.Get("Content-Type")
	switch {
	case strings.Contains(ct, "mpeg") || strings.HasSuffix(resultURL, ".mp3"):
		format = "mp3"
	case strings.Contains(ct, "flac") || strings.HasSuffix(resultURL, ".flac"):
		format = "flac"
	}
	if len(audio) == 0 {
		return nil, "", fmt.Errorf("F5-TTS: empty result body")
	}
	return audio, format, nil
}

// f5ttsDownload fetches an audio file URL and returns the raw bytes + format.
func f5ttsDownload(ctx context.Context, url string, headers map[string]string, client *http.Client) ([]byte, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, "", fmt.Errorf("F5-TTS: build download request: %w", err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("F5-TTS: audio download failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, "", fmt.Errorf("F5-TTS: audio download HTTP %d", resp.StatusCode)
	}
	audio, _ := io.ReadAll(resp.Body)
	// Infer format from Content-Type or URL extension.
	format := "wav"
	ct := resp.Header.Get("Content-Type")
	switch {
	case strings.Contains(ct, "mpeg") || strings.HasSuffix(url, ".mp3"):
		format = "mp3"
	case strings.Contains(ct, "flac") || strings.HasSuffix(url, ".flac"):
		format = "flac"
	}
	return audio, format, nil
}

// ── MiMo-V2.5-TTS (Xiaomi) ───────────────────────────────────────────────────
// API docs: https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5
//
// MiMo uses the chat completions endpoint, NOT /v1/audio/speech.
// The text to speak goes in the `assistant` message; an optional `user`
// message carries a style/emotion instruction or (for voicedesign) the voice
// description prompt.
//
// voice parameter semantics per model:
//   - mimo-v2.5-tts          → preset voice name ("mimo_default","冰糖","Chloe",…)
//   - mimo-v2.5-tts-voicedesign → voice is a text description of the desired voice;
//                               if non-empty it is placed in the user message;
//                               set model explicitly via cfg.MiMoModel
//   - mimo-v2.5-tts-voiceclone  → voice is a data-URI string:
//                               "data:audio/mpeg;base64,<base64-encoded mp3/wav>"
//
// Audio is returned as base64 WAV in choices[0].message.audio.data.
// Auth header is "api-key: <key>" (not "Authorization: Bearer …").
func synthMiMoTTS(ctx context.Context, text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	if cfg.MiMoKey == "" {
		return nil, "", fmt.Errorf("MiMo TTS: no API key — set MIMO_API_KEY or --tts-mimo-key")
	}

	baseURL := cfg.MiMoBaseURL
	if baseURL == "" {
		baseURL = "https://api.xiaomimimo.com/v1"
	}
	modelName := cfg.MiMoModel
	if modelName == "" {
		modelName = "mimo-v2.5-tts"
	}
	if voice == "" {
		voice = "mimo_default"
	}

	// Build messages array.  Text to synthesise always goes in the assistant
	// message.  For voicedesign the voice string is a style description that
	// goes in the user message.  For voiceclone the data-URI goes in audio.voice.
	messages := []map[string]any{
		{"role": "assistant", "content": text},
	}

	audioParams := map[string]any{
		"format": "wav",
	}

	switch {
	case modelName == "mimo-v2.5-tts-voicedesign":
		// voice is a text description; pass as user instruction.
		if voice != "" && voice != "mimo_default" {
			messages = append([]map[string]any{{"role": "user", "content": voice}}, messages...)
		}
		audioParams["optimize_text_preview"] = true

	case modelName == "mimo-v2.5-tts-voiceclone":
		// voice is a data-URI base64-encoded audio sample.
		if voice != "" && voice != "mimo_default" {
			audioParams["voice"] = voice
		}
		// user message is optional for voiceclone; add empty string to satisfy API.
		messages = append([]map[string]any{{"role": "user", "content": ""}}, messages...)

	default:
		// mimo-v2.5-tts — preset voice in audio.voice.
		audioParams["voice"] = voice
	}

	payload := map[string]any{
		"model":    modelName,
		"messages": messages,
		"audio":    audioParams,
		"stream":   false,
	}
	bodyJSON, _ := json.Marshal(payload)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		baseURL+"/chat/completions", bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("MiMo TTS: build request: %w", err)
	}
	req.Header.Set("api-key", cfg.MiMoKey)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 120 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("MiMo TTS: request failed: %w", err)
	}
	defer resp.Body.Close()

	respBytes, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return nil, "", fmt.Errorf("MiMo TTS: invalid API key (HTTP %d) — check MIMO_API_KEY or --tts-mimo-key", resp.StatusCode)
	}
	if resp.StatusCode >= 400 {
		return nil, "", fmt.Errorf("MiMo TTS: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(respBytes)))
	}

	// Parse: choices[0].message.audio.data (base64 WAV)
	var respBody struct {
		Choices []struct {
			Message struct {
				Audio *struct {
					Data string `json:"data"`
				} `json:"audio"`
			} `json:"message"`
		} `json:"choices"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(respBytes, &respBody); err != nil {
		return nil, "", fmt.Errorf("MiMo TTS: invalid response JSON: %w", err)
	}
	if respBody.Error != nil && respBody.Error.Message != "" {
		return nil, "", fmt.Errorf("MiMo TTS error: %s", respBody.Error.Message)
	}
	if len(respBody.Choices) == 0 || respBody.Choices[0].Message.Audio == nil {
		return nil, "", fmt.Errorf("MiMo TTS: response missing choices[0].message.audio")
	}
	audioData := respBody.Choices[0].Message.Audio.Data
	if audioData == "" {
		return nil, "", fmt.Errorf("MiMo TTS: empty audio data in response")
	}

	audio, err := base64.StdEncoding.DecodeString(audioData)
	if err != nil {
		return nil, "", fmt.Errorf("MiMo TTS: base64 decode failed: %w", err)
	}

	fmt.Printf("%s[mimotts] synthesised %d chars → %d bytes (wav)  voice=%s  model=%s\n",
		prefixSYS(), len([]rune(text)), len(audio), voice, modelName)
	return audio, "wav", nil
}

// synthMiMoTTSStreaming is the SSE streaming variant of synthMiMoTTS.
// It uses stream=true and collects PCM16LE chunks (24 kHz, mono, 16-bit)
// delivered via Server-Sent Events, then wraps them in a WAV header.
func synthMiMoTTSStreaming(ctx context.Context, text, voice string, cfg *TtsConfig) ([]byte, string, error) {
	if cfg.MiMoKey == "" {
		return nil, "", fmt.Errorf("MiMo TTS (streaming): no API key — set MIMO_API_KEY or --tts-mimo-key")
	}

	baseURL := cfg.MiMoBaseURL
	if baseURL == "" {
		baseURL = "https://api.xiaomimimo.com/v1"
	}
	modelName := cfg.MiMoModel
	if modelName == "" {
		modelName = "mimo-v2.5-tts"
	}
	if voice == "" {
		voice = "mimo_default"
	}

	// Build messages — same structure as the non-streaming variant.
	messages := []map[string]any{
		{"role": "assistant", "content": text},
	}

	audioParams := map[string]any{
		"format": "pcm16",
	}

	switch {
	case modelName == "mimo-v2.5-tts-voicedesign":
		if voice != "" && voice != "mimo_default" {
			messages = append([]map[string]any{{"role": "user", "content": voice}}, messages...)
		}
		audioParams["optimize_text_preview"] = true
	case modelName == "mimo-v2.5-tts-voiceclone":
		if voice != "" && voice != "mimo_default" {
			audioParams["voice"] = voice
		}
		messages = append([]map[string]any{{"role": "user", "content": ""}}, messages...)
	default:
		audioParams["voice"] = voice
	}

	payload := map[string]any{
		"model":    modelName,
		"messages": messages,
		"audio":    audioParams,
		"stream":   true,
	}
	bodyJSON, _ := json.Marshal(payload)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		baseURL+"/chat/completions", bytes.NewReader(bodyJSON))
	if err != nil {
		return nil, "", fmt.Errorf("MiMo TTS (streaming): build request: %w", err)
	}
	req.Header.Set("api-key", cfg.MiMoKey)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")

	client := &http.Client{Timeout: 120 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("MiMo TTS (streaming): request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return nil, "", fmt.Errorf("MiMo TTS (streaming): invalid API key (HTTP %d)", resp.StatusCode)
	}
	if resp.StatusCode >= 400 {
		errBody, _ := io.ReadAll(resp.Body)
		return nil, "", fmt.Errorf("MiMo TTS (streaming): HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(errBody)))
	}

	// ── Parse SSE stream, collect PCM16 chunks ──────────────────────
	const sampleRate = 24000
	const channels = 1
	const bitsPerSample = 16

	var allSamples []int16
	scanner := bufio.NewScanner(resp.Body)
	// Increase scanner buffer — audio chunks can be larger than the default 64 KB.
	scanner.Buffer(make([]byte, 0, 256*1024), 8*1024*1024)

	for scanner.Scan() {
		line := scanner.Text()

		// SSE data lines: "data: <json>" or "data: [DONE]"
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		dataStr := strings.TrimPrefix(line, "data: ")
		if dataStr == "[DONE]" {
			break
		}

		// Parse the chunk JSON.
		type chunkDelta struct {
			Audio *struct {
				Data string `json:"data"`
			} `json:"audio"`
		}
		type chunkChoice struct {
			Delta chunkDelta `json:"delta"`
		}
		var chunk struct {
			Choices []chunkChoice `json:"choices"`
		}
		if err := json.Unmarshal([]byte(dataStr), &chunk); err != nil {
			continue // skip malformed chunks (e.g. keep-alive comments)
		}
		if len(chunk.Choices) == 0 || chunk.Choices[0].Delta.Audio == nil {
			continue
		}

		pcmBytes, err := base64.StdEncoding.DecodeString(chunk.Choices[0].Delta.Audio.Data)
		if err != nil {
			continue
		}
		// PCM16LE → []int16 samples
		samples := make([]int16, len(pcmBytes)/2)
		for i := range samples {
			samples[i] = int16(binary.LittleEndian.Uint16(pcmBytes[i*2 : (i+1)*2]))
		}
		allSamples = append(allSamples, samples...)
	}

	if err := scanner.Err(); err != nil {
		return nil, "", fmt.Errorf("MiMo TTS (streaming): SSE read error: %w", err)
	}

	if len(allSamples) == 0 {
		return nil, "", fmt.Errorf("MiMo TTS (streaming): received no audio data")
	}

	// ── Wrap PCM16 in WAV header ───────────────────────────────────
	wav := pcm16ToWAV(allSamples, sampleRate, channels, bitsPerSample)

	fmt.Printf("%s[mimotts] streaming synthesised %d chars → %d bytes (wav, %.1fs audio)  voice=%s  model=%s\n",
		prefixSYS(), len([]rune(text)), len(wav),
		float64(len(allSamples))/float64(sampleRate), voice, modelName)
	return wav, "wav", nil
}

// pcm16ToWAV wraps raw PCM16LE samples in a RIFF/WAV header.
func pcm16ToWAV(samples []int16, sampleRate uint32, numChannels, bitsPerSample uint16) []byte {
	dataLen := uint32(len(samples) * int(bitsPerSample/8))
	byteRate := sampleRate * uint32(numChannels) * uint32(bitsPerSample/8)
	blockAlign := numChannels * bitsPerSample / 8

	// Build PCM data bytes (little-endian).
	pcmData := make([]byte, dataLen)
	for i, s := range samples {
		binary.LittleEndian.PutUint16(pcmData[i*2:], uint16(s))
	}

	var buf bytes.Buffer
	// RIFF header
	buf.WriteString("RIFF")
	binary.Write(&buf, binary.LittleEndian, uint32(36+dataLen)) // chunk size
	buf.WriteString("WAVE")
	// fmt  sub-chunk
	buf.WriteString("fmt ")
	binary.Write(&buf, binary.LittleEndian, uint32(16))           // sub-chunk size (PCM)
	binary.Write(&buf, binary.LittleEndian, uint16(1))            // audio format (PCM = 1)
	binary.Write(&buf, binary.LittleEndian, numChannels)
	binary.Write(&buf, binary.LittleEndian, sampleRate)
	binary.Write(&buf, binary.LittleEndian, byteRate)
	binary.Write(&buf, binary.LittleEndian, blockAlign)
	binary.Write(&buf, binary.LittleEndian, bitsPerSample)
	// data sub-chunk
	buf.WriteString("data")
	binary.Write(&buf, binary.LittleEndian, dataLen)
	buf.Write(pcmData)

	return buf.Bytes()
}
