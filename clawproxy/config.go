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
	Tts fileTtsSection `toml:"tts"`
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

	// Decrypt any enc2:/enc: prefixed API keys using the secret key stored
	// alongside the config file (same algorithm as zeroclaw's SecretStore).
	keyFile := filepath.Join(filepath.Dir(path), ".secret_key")
	decryptSecretsInTtsConfig(&fc.Tts, keyFile)

	return &fc.Tts, nil
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
