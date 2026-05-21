package main

// config.go — TOML config file support for clawproxy.
//
// clawproxy reads TTS (and future) provider settings from a TOML file whose
// schema mirrors zeroclaw's config.toml so that a shared deployment only needs
// one config file.
//
// Default lookup order (first file found wins):
//   1. --config <path>                (explicit CLI flag)
//   2. $ZEROCLAW_CONFIG               (env var)
//   3. ~/.zeroclaw/config.toml        (zeroclaw default)
//   4. ~/.config/zeroclaw/config.toml (XDG fallback)
//
// Within tts settings, priority is:
//   CLI flags > env vars > config file > built-in defaults

import (
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
	"golang.org/x/crypto/chacha20poly1305"
)

// ── TOML schema (mirrors zeroclaw's [tts] section) ────────────────────────────

// fileConfig is the top-level shape of the TOML file. Only the sections
// clawproxy cares about are decoded; everything else is silently ignored.
type fileConfig struct {
	Tts       fileTtsSection  `toml:"tts"`
	Providers fileProviders   `toml:"providers"`
}

// fileProviders mirrors zeroclaw's [providers] table.
// We only read [providers.models] to borrow API keys for TTS providers
// that share the same key (e.g. MiniMax uses the same key for LLM and TTS).
type fileProviders struct {
	Models map[string]fileModelProvider `toml:"models"`
}

// fileModelProvider holds the minimal fields clawproxy needs from a
// [providers.models.<alias>] entry.
type fileModelProvider struct {
	APIKey string `toml:"api_key"`
}

// fileTtsSection mirrors zeroclaw's [tts] table.
type fileTtsSection struct {
	Enabled         bool                `toml:"enabled"`
	DefaultProvider string              `toml:"default_provider"`
	DefaultVoice    string              `toml:"default_voice"`
	DefaultFormat   string              `toml:"default_format"`
	MaxTextLength   int                 `toml:"max_text_length"`
	OpenAI          *fileTtsOpenAI      `toml:"openai"`
	ElevenLabs      *fileTtsElevenLabs  `toml:"elevenlabs"`
	Google          *fileTtsGoogle      `toml:"google"`
	Edge            *fileTtsEdge        `toml:"edge"`
	Piper           *fileTtsPiper       `toml:"piper"`
	// [tts.minimax] is a clawproxy extension (zeroclaw uses minimax for LLM, not yet TTS).
	MiniMax         *fileTtsMiniMax     `toml:"minimax"`
}

// fileTtsOpenAI mirrors zeroclaw's [tts.openai] table.
type fileTtsOpenAI struct {
	APIKey string  `toml:"api_key"`
	Model  string  `toml:"model"`  // default: tts-1
	Speed  float64 `toml:"speed"`  // default: 1.0
}

// fileTtsElevenLabs mirrors zeroclaw's [tts.elevenlabs] table.
type fileTtsElevenLabs struct {
	APIKey          string  `toml:"api_key"`
	ModelID         string  `toml:"model_id"`        // default: eleven_monolingual_v1
	Stability       float64 `toml:"stability"`        // 0.0–1.0
	SimilarityBoost float64 `toml:"similarity_boost"` // 0.0–1.0
}

// fileTtsGoogle mirrors zeroclaw's [tts.google] table.
type fileTtsGoogle struct {
	APIKey       string `toml:"api_key"`
	LanguageCode string `toml:"language_code"` // default: en-US
}

// fileTtsEdge mirrors zeroclaw's [tts.edge] table.
type fileTtsEdge struct {
	BinaryPath string `toml:"binary_path"` // default: edge-tts
}

// fileTtsPiper mirrors zeroclaw's [tts.piper] table.
type fileTtsPiper struct {
	APIURL string `toml:"api_url"` // default: http://127.0.0.1:5000/v1/audio/speech
}

// fileTtsMiniMax is a clawproxy extension under [tts.minimax].
// Example config.toml snippet:
//
//	[tts.minimax]
//	api_key  = "your-minimax-key"
//	model    = "speech-2.8-hd"
//	base_url = "https://api.minimaxi.com/v1/t2a_v2"
type fileTtsMiniMax struct {
	APIKey  string `toml:"api_key"`
	Model   string `toml:"model"`    // default: speech-2.8-hd
	BaseURL string `toml:"base_url"` // default: https://api.minimaxi.com/v1/t2a_v2
}

// ── Config discovery ──────────────────────────────────────────────────────────

// discoverConfigPath returns the first config.toml found in the standard
// zeroclaw locations, or "" if none exists.
func discoverConfigPath() string {
	if v := os.Getenv("ZEROCLAW_CONFIG"); v != "" {
		return v
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	candidates := []string{
		filepath.Join(home, ".zeroclaw", "config.toml"),
		filepath.Join(home, ".config", "zeroclaw", "config.toml"),
	}
	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// loadFileConfig parses a TOML config file and returns its [tts] section with
// API keys decrypted using zeroclaw's secret store (key file at
// <config_dir>/.secret_key).
//
// When a [tts.<provider>] api_key is absent, clawproxy also looks in
// [providers.models.*] for a matching provider alias and borrows its key —
// this is how MiniMax (and OpenAI) work: the same API key is used for both
// LLM chat and TTS synthesis.
//
// Returns nil (no error) when the file does not exist so callers can treat a
// missing config as "use defaults".
func loadFileConfig(path string) (*fileTtsSection, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path) //nolint:gosec
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var fc fileConfig
	if _, err := toml.Decode(string(data), &fc); err != nil {
		return nil, err
	}

	keyFile := filepath.Join(filepath.Dir(path), ".secret_key")

	// Decrypt secrets in [tts.*] sections.
	decryptSecretsInTtsConfig(&fc.Tts, keyFile)

	// Scan [tts.*] sub-tables by alias name.
	// The fixed struct only captures exact keys ("minimax", "openai", …);
	// users may write [tts.minimax-cn] or [tts.minimaxi] which go unmatched.
	scanTtsAliases(&fc.Tts, data, keyFile)

	// Decrypt secrets in [providers.models.*] and borrow keys into [tts.*]
	// for providers that share the same key (e.g. MiniMax uses the same key for LLM and TTS).
	borrowModelProviderKeys(&fc.Tts, fc.Providers.Models, keyFile)

	// Normalize default_provider to its canonical name (e.g. "minimax-cn" → "minimax").
	fc.Tts.DefaultProvider = canonicalProvider(fc.Tts.DefaultProvider)

	return &fc.Tts, nil
}

// scanTtsAliases performs a raw-map decode of the full TOML file and looks for
// any [tts.<alias>] sub-table whose name resolves to a known provider.  This
// supplements the fixed-struct decode which only matches exact TOML keys.
func scanTtsAliases(tts *fileTtsSection, raw []byte, keyFile string) {
	var m map[string]interface{}
	if _, err := toml.Decode(string(raw), &m); err != nil {
		return
	}
	ttsMap, _ := m["tts"].(map[string]interface{})
	for alias, val := range ttsMap {
		sub, ok := val.(map[string]interface{})
		if !ok {
			continue
		}
		getStr := func(key string) string {
			v, _ := sub[key].(string)
			return v
		}
		switch canonicalProvider(strings.ToLower(alias)) {
		case "minimax":
			if tts.MiniMax == nil {
				tts.MiniMax = &fileTtsMiniMax{}
			}
			if tts.MiniMax.APIKey == "" {
				tts.MiniMax.APIKey = decryptSecret(getStr("api_key"), keyFile)
			}
			if tts.MiniMax.Model == "" {
				tts.MiniMax.Model = getStr("model")
			}
			if tts.MiniMax.BaseURL == "" {
				tts.MiniMax.BaseURL = getStr("base_url")
			}
		case "openai":
			if tts.OpenAI == nil {
				tts.OpenAI = &fileTtsOpenAI{}
			}
			if tts.OpenAI.APIKey == "" {
				tts.OpenAI.APIKey = decryptSecret(getStr("api_key"), keyFile)
			}
			if tts.OpenAI.Model == "" {
				tts.OpenAI.Model = getStr("model")
			}
		case "elevenlabs":
			if tts.ElevenLabs == nil {
				tts.ElevenLabs = &fileTtsElevenLabs{}
			}
			if tts.ElevenLabs.APIKey == "" {
				tts.ElevenLabs.APIKey = decryptSecret(getStr("api_key"), keyFile)
			}
		case "google":
			if tts.Google == nil {
				tts.Google = &fileTtsGoogle{}
			}
			if tts.Google.APIKey == "" {
				tts.Google.APIKey = decryptSecret(getStr("api_key"), keyFile)
			}
			if tts.Google.LanguageCode == "" {
				tts.Google.LanguageCode = getStr("language_code")
			}
		case "edge":
			if tts.Edge == nil {
				tts.Edge = &fileTtsEdge{}
			}
			if tts.Edge.BinaryPath == "" {
				tts.Edge.BinaryPath = getStr("binary_path")
			}
		case "piper":
			if tts.Piper == nil {
				tts.Piper = &fileTtsPiper{}
			}
			if tts.Piper.APIURL == "" {
				tts.Piper.APIURL = getStr("api_url")
			}
		}
	}
}

// borrowModelProviderKeys looks through [providers.models.*] entries and
// copies API keys into the [tts.*] sections when those sections either don't
// exist or have no key set.  This handles the common case where the user has
// configured e.g. [providers.models.minimax] for LLM use and expects the same
// key to be picked up for TTS without a separate [tts.minimax] block.
func borrowModelProviderKeys(tts *fileTtsSection, models map[string]fileModelProvider, keyFile string) {
	for alias, mp := range models {
		if mp.APIKey == "" {
			continue
		}
		key := decryptSecret(mp.APIKey, keyFile)
		lower := strings.ToLower(alias)

		switch {
		case isMiniMaxAlias(lower):
			if tts.MiniMax == nil {
				tts.MiniMax = &fileTtsMiniMax{}
			}
			if tts.MiniMax.APIKey == "" {
				tts.MiniMax.APIKey = key
			}
		case isOpenAIAlias(lower):
			if tts.OpenAI == nil {
				tts.OpenAI = &fileTtsOpenAI{}
			}
			if tts.OpenAI.APIKey == "" {
				tts.OpenAI.APIKey = key
			}
		case isElevenLabsAlias(lower):
			if tts.ElevenLabs == nil {
				tts.ElevenLabs = &fileTtsElevenLabs{}
			}
			if tts.ElevenLabs.APIKey == "" {
				tts.ElevenLabs.APIKey = key
			}
		case isGoogleAlias(lower):
			if tts.Google == nil {
				tts.Google = &fileTtsGoogle{}
			}
			if tts.Google.APIKey == "" {
				tts.Google.APIKey = key
			}
		}
	}
}

// isMiniMaxAlias mirrors zeroclaw's is_minimax_alias() in provider_aliases.rs.
func isMiniMaxAlias(name string) bool {
	switch name {
	case "minimax", "minimax-intl", "minimax-io", "minimax-global",
		"minimax-oauth", "minimax-portal", "minimax-oauth-global", "minimax-portal-global",
		"minimax-cn", "minimaxi", "minimax-oauth-cn", "minimax-portal-cn":
		return true
	}
	return false
}

// isOpenAIAlias returns true for common zeroclaw OpenAI model alias names.
func isOpenAIAlias(name string) bool {
	switch name {
	case "openai", "gpt", "openai-compat":
		return true
	}
	return strings.HasPrefix(name, "openai-") || strings.HasPrefix(name, "gpt-")
}

// isGoogleAlias returns true for common Google TTS / Gemini alias names.
func isGoogleAlias(name string) bool {
	switch name {
	case "google", "google-tts", "google-cloud", "google-cloud-tts", "gemini", "gcloud":
		return true
	}
	return strings.HasPrefix(name, "google-") || strings.HasPrefix(name, "gemini-")
}

// isElevenLabsAlias returns true for common ElevenLabs alias names.
func isElevenLabsAlias(name string) bool {
	return name == "elevenlabs" || strings.HasPrefix(name, "elevenlabs-")
}

// ── Secret decryption (mirrors zeroclaw's SecretStore) ────────────────────────
//
// zeroclaw stores API keys in config.toml as:
//   enc2:<hex(12-byte-nonce ‖ ciphertext ‖ 16-byte-poly1305-tag)>  (ChaCha20-Poly1305)
//   enc:<hex(xor(plaintext, key))>                                  (legacy XOR)
//   <plaintext>                                                       (no encryption)
//
// The 32-byte encryption key is stored as a hex string in .secret_key,
// located in the same directory as config.toml (e.g. ~/.zeroclaw/.secret_key).

// decryptSecret decrypts a single config value using zeroclaw's format.
// Returns the value unchanged if it is already plaintext or if decryption fails
// (with a warning printed to stderr).
func decryptSecret(value, keyFile string) string {
	if value == "" {
		return value
	}
	if strings.HasPrefix(value, "enc2:") {
		plain, err := decryptChaCha20(value[5:], keyFile)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[clawproxy] warning: could not decrypt enc2: secret: %v\n", err)
			return value
		}
		return plain
	}
	if strings.HasPrefix(value, "enc:") {
		plain, err := decryptXOR(value[4:], keyFile)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[clawproxy] warning: could not decrypt legacy enc: secret: %v\n", err)
			return value
		}
		return plain
	}
	return value // plaintext
}

// loadSecretKey reads the 32-byte key from a hex-encoded key file.
func loadSecretKey(keyFile string) ([]byte, error) {
	raw, err := os.ReadFile(keyFile) //nolint:gosec
	if err != nil {
		return nil, fmt.Errorf("read secret key %s: %w", keyFile, err)
	}
	key, err := hex.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil {
		return nil, fmt.Errorf("parse secret key: %w", err)
	}
	if len(key) != 32 {
		return nil, fmt.Errorf("secret key must be 32 bytes, got %d", len(key))
	}
	return key, nil
}

// decryptChaCha20 decrypts an enc2: payload (hex-encoded nonce+ciphertext+tag).
func decryptChaCha20(hexPayload, keyFile string) (string, error) {
	blob, err := hex.DecodeString(hexPayload)
	if err != nil {
		return "", fmt.Errorf("decode enc2 hex: %w", err)
	}
	const nonceLen = 12
	if len(blob) <= nonceLen {
		return "", fmt.Errorf("enc2: payload too short")
	}
	key, err := loadSecretKey(keyFile)
	if err != nil {
		return "", err
	}
	aead, err := chacha20poly1305.New(key)
	if err != nil {
		return "", fmt.Errorf("create chacha20poly1305: %w", err)
	}
	plaintext, err := aead.Open(nil, blob[:nonceLen], blob[nonceLen:], nil)
	if err != nil {
		return "", fmt.Errorf("chacha20poly1305 decrypt: %w (wrong .secret_key?)", err)
	}
	return string(plaintext), nil
}

// decryptXOR decrypts a legacy enc: payload (hex-encoded XOR-with-key).
func decryptXOR(hexPayload, keyFile string) (string, error) {
	ciphertext, err := hex.DecodeString(hexPayload)
	if err != nil {
		return "", fmt.Errorf("decode enc hex: %w", err)
	}
	key, err := loadSecretKey(keyFile)
	if err != nil {
		return "", err
	}
	out := make([]byte, len(ciphertext))
	for i, b := range ciphertext {
		out[i] = b ^ key[i%len(key)]
	}
	return string(out), nil
}

// decryptSecretsInTtsConfig decrypts all API key fields in-place.
func decryptSecretsInTtsConfig(fc *fileTtsSection, keyFile string) {
	if fc.OpenAI != nil {
		fc.OpenAI.APIKey = decryptSecret(fc.OpenAI.APIKey, keyFile)
	}
	if fc.ElevenLabs != nil {
		fc.ElevenLabs.APIKey = decryptSecret(fc.ElevenLabs.APIKey, keyFile)
	}
	if fc.Google != nil {
		fc.Google.APIKey = decryptSecret(fc.Google.APIKey, keyFile)
	}
	if fc.MiniMax != nil {
		fc.MiniMax.APIKey = decryptSecret(fc.MiniMax.APIKey, keyFile)
	}
}
