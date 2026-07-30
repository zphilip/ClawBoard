package registry

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sync"
	"time"

	"github.com/openclaw/ha-lite/internal/xiaomi"
)

// DeviceInfo holds a device's runtime state.
type DeviceInfo struct {
	Name      string    `json:"name"`
	IP        string    `json:"ip"`
	Token     string    `json:"token"`
	Model     string    `json:"model"`
	DID       string    `json:"did"`
	UpdatedAt time.Time `json:"updated_at"`
	Online    bool      `json:"online"`
}

// Registry manages the local device registry with file-backed persistence.
type Registry struct {
	mu        sync.RWMutex
	devices   map[string]*DeviceInfo // keyed by DID
	cacheFile string
}

// New creates a new Registry.
func New(cacheFile string) *Registry {
	return &Registry{
		devices:   make(map[string]*DeviceInfo),
		cacheFile: cacheFile,
	}
}

// LoadCache reads cached devices from disk.
func (r *Registry) LoadCache() (int, error) {
	data, err := os.ReadFile(r.cacheFile)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, nil
		}
		return 0, fmt.Errorf("read cache: %w", err)
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	if err := json.Unmarshal(data, &r.devices); err != nil {
		return 0, fmt.Errorf("unmarshal cache: %w", err)
	}

	return len(r.devices), nil
}

// SaveCache persists the current device list to disk.
func (r *Registry) SaveCache() error {
	r.mu.RLock()
	data, err := json.MarshalIndent(r.devices, "", "  ")
	r.mu.RUnlock()
	if err != nil {
		return fmt.Errorf("marshal cache: %w", err)
	}

	tmpFile := r.cacheFile + ".tmp"
	if err := os.WriteFile(tmpFile, data, 0644); err != nil {
		return fmt.Errorf("write temp cache: %w", err)
	}
	if err := os.Rename(tmpFile, r.cacheFile); err != nil {
		return fmt.Errorf("rename cache: %w", err)
	}
	return nil
}

// MergeFromCloud updates the registry with fresh cloud data.
// Returns the number of new or updated devices.
func (r *Registry) MergeFromCloud(cloudDevices []xiaomi.DeviceInfo) int {
	r.mu.Lock()
	defer r.mu.Unlock()

	updated := 0
	seen := make(map[string]bool)

	for _, cd := range cloudDevices {
		seen[cd.DID] = true

		existing, ok := r.devices[cd.DID]
		if !ok {
			r.devices[cd.DID] = &DeviceInfo{
				Name:      cd.Name,
				IP:        cd.IP,
				Token:     cd.Token,
				Model:     cd.Model,
				DID:       cd.DID,
				UpdatedAt: time.Now(),
				Online:    true,
			}
			log.Printf("[registry] New device: %s (%s) @ %s", cd.Name, cd.Model, cd.IP)
			updated++
			continue
		}

		// Update if IP or token changed.
		changed := false
		if existing.IP != cd.IP {
			log.Printf("[registry] Device %s IP changed: %s → %s", cd.Name, existing.IP, cd.IP)
			existing.IP = cd.IP
			changed = true
		}
		if existing.Token != cd.Token {
			log.Printf("[registry] Device %s token refreshed", cd.Name)
			existing.Token = cd.Token
			changed = true
		}
		if existing.Model != cd.Model {
			existing.Model = cd.Model
			changed = true
		}
		if existing.Name != cd.Name {
			existing.Name = cd.Name
			changed = true
		}
		if changed {
			existing.UpdatedAt = time.Now()
			existing.Online = true
			updated++
		}
	}

	// Mark devices not seen in cloud as offline.
	for did, dev := range r.devices {
		if !seen[did] {
			dev.Online = false
		}
	}

	return updated
}

// GetAll returns all registered devices.
func (r *Registry) GetAll() []*DeviceInfo {
	r.mu.RLock()
	defer r.mu.RUnlock()

	result := make([]*DeviceInfo, 0, len(r.devices))
	for _, d := range r.devices {
		result = append(result, d)
	}
	return result
}

// Get returns a device by DID.
func (r *Registry) Get(did string) (*DeviceInfo, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	d, ok := r.devices[did]
	return d, ok
}

// Update sets local state for a device (e.g., after a successful local control).
func (r *Registry) Update(did, ip, token string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if d, ok := r.devices[did]; ok {
		if ip != "" {
			d.IP = ip
		}
		if token != "" {
			d.Token = token
		}
		d.UpdatedAt = time.Now()
		d.Online = true
	}
}

// MarkOffline marks a device as unreachable.
func (r *Registry) MarkOffline(did string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if d, ok := r.devices[did]; ok {
		d.Online = false
	}
}

// Count returns the number of devices in the registry.
func (r *Registry) Count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.devices)
}