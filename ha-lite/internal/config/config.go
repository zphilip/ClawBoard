package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// Config holds all configuration for HA Lite.
type Config struct {
	Server   ServerConfig   `yaml:"server"`
	Xiaomi   XiaomiConfig   `yaml:"xiaomi"`
	Devices  []DeviceConfig `yaml:"devices"`
	Registry RegistryConfig `yaml:"registry"`
}

// ServerConfig holds HTTP server settings.
type ServerConfig struct {
	Host string `yaml:"host"`
	Port int    `yaml:"port"`
}

// XiaomiConfig holds Xiaomi cloud account credentials.
type XiaomiConfig struct {
	Username string `yaml:"username"` // Optional: for password login
	Password string `yaml:"password"` // Optional: for password login (use QR login if empty)
	Region   string `yaml:"region"`   // "cn" (mainland China), "sg", "de", "us", etc.
}

// HasPasswordAuth returns true if password-based login is configured.
func (x XiaomiConfig) HasPasswordAuth() bool {
	return x.Username != "" && x.Password != ""
}

// DeviceConfig holds a static device entry (optional fallback).
type DeviceConfig struct {
	Name  string `yaml:"name"`
	DID   string `yaml:"did"`
	IP    string `yaml:"ip"`
	Token string `yaml:"token"`
	Model string `yaml:"model"`
}

// RegistryConfig holds device registry persistence settings.
type RegistryConfig struct {
	CacheFile string `yaml:"cache_file"`
}

// DefaultConfig returns a Config with sensible defaults.
func DefaultConfig() Config {
	return Config{
		Server: ServerConfig{
			Host: "0.0.0.0",
			Port: 8090,
		},
		Xiaomi: XiaomiConfig{
			Region: "cn",
		},
		Registry: RegistryConfig{
			CacheFile: "cache/mi_tokens.json",
		},
	}
}

// Load reads configuration from the given path, falling back to environment
// variables and defaults.
func Load(path string) (*Config, error) {
	cfg := DefaultConfig()

	// Try YAML config file first.
	if path != "" {
		if data, err := os.ReadFile(path); err == nil {
			if err := yaml.Unmarshal(data, &cfg); err != nil {
				return nil, fmt.Errorf("parse config %s: %w", path, err)
			}
		}
	}

	// Environment variables override config file.
	applyEnvOverrides(&cfg)

	// Ensure cache directory exists.
	cacheDir := filepath.Dir(cfg.Registry.CacheFile)
	if cacheDir != "" && cacheDir != "." {
		if err := os.MkdirAll(cacheDir, 0755); err != nil {
			return nil, fmt.Errorf("create cache dir %s: %w", cacheDir, err)
		}
	}

	return &cfg, nil
}

// applyEnvOverrides overrides config fields from environment variables.
func applyEnvOverrides(cfg *Config) {
	if v := os.Getenv("HALITE_HOST"); v != "" {
		cfg.Server.Host = v
	}
	if v := os.Getenv("HALITE_PORT"); v != "" {
		fmt.Sscanf(v, "%d", &cfg.Server.Port)
	}
	if v := os.Getenv("HALITE_XIAOMI_USERNAME"); v != "" {
		cfg.Xiaomi.Username = v
	}
	if v := os.Getenv("HALITE_XIAOMI_PASSWORD"); v != "" {
		cfg.Xiaomi.Password = v
	}
	if v := os.Getenv("HALITE_XIAOMI_REGION"); v != "" {
		cfg.Xiaomi.Region = v
	}
	if v := os.Getenv("HALITE_CACHE_FILE"); v != "" {
		cfg.Registry.CacheFile = v
	}
}

// Validate checks that the configuration is usable.
func (c *Config) Validate() error {
	var errs []string

	// Username/password are optional — user can use QR login instead.
	// At least one auth method will be needed before cloud sync works.
	if c.Server.Port < 1 || c.Server.Port > 65535 {
		errs = append(errs, fmt.Sprintf("server.port %d is out of range", c.Server.Port))
	}

	if len(errs) > 0 {
		return fmt.Errorf("configuration errors:\n  - %s", strings.Join(errs, "\n  - "))
	}
	return nil
}