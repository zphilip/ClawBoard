package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/openclaw/ha-lite/internal/config"
	"github.com/openclaw/ha-lite/internal/miio"
	"github.com/openclaw/ha-lite/internal/registry"
	"github.com/openclaw/ha-lite/internal/xiaomi"
)

// ── Global state ──────────────────────────────────────────────────────────────

var (
	cfg       *config.Config
	reg       *registry.Registry
	cloud     *xiaomi.CloudClient
	buildTime = "dev"
	version   = "0.10.0"

	// QR login state.
	qrMu       sync.Mutex
	qrMgr      *xiaomi.QRLoginManager
	qrImagePNG []byte
	qrStatus   string // "idle", "waiting", "scanned", "authenticated", "timeout", "error"
	qrMessage  string
)

// ── Main ──────────────────────────────────────────────────────────────────────

func main() {
	log.SetFlags(log.LstdFlags | log.Lshortfile)
	log.Printf("🏠 ha-lite server %s (built %s)", version, buildTime)

	// Load configuration.
	var err error
	configPath := os.Getenv("HALITE_CONFIG")
	if configPath == "" {
		configPath = "halite.yaml"
	}
	cfg, err = config.Load(configPath)
	if err != nil {
		log.Fatalf("❌ Config error: %v", err)
	}
	if err := cfg.Validate(); err != nil {
		log.Fatalf("❌ Config validation: %v", err)
	}

	// Initialize device registry.
	reg = registry.New(cfg.Registry.CacheFile)
	if n, err := reg.LoadCache(); err != nil {
		log.Printf("⚠️  Cache load: %v", err)
	} else if n > 0 {
		log.Printf("📂 Loaded %d devices from cache", n)
	}

	// Initialize Xiaomi cloud client.
	cloud = xiaomi.NewCloudClient(cfg.Xiaomi.Username, cfg.Xiaomi.Password, cfg.Xiaomi.Region)

	// Initialize QR login manager.
	qrMgr = xiaomi.NewQRLoginManager(cfg.Xiaomi.Region, 120)
	qrStatus = "idle"

	// Initial cloud sync — try password first, then fall back to QR.
	if cfg.Xiaomi.HasPasswordAuth() {
		if err := syncFromCloud(); err != nil {
			log.Printf("⚠️  Initial cloud sync failed: %v", err)
			if reg.Count() == 0 {
				log.Println("💡 No devices cached. Use QR login: POST /api/login/qr/start")
			}
			log.Println("📂 Continuing with cached devices only.")
		}
	} else {
		log.Println("💡 No password configured. Open http://<pi-ip>:8090/api/login/qr in browser to scan QR")
		if reg.Count() == 0 {
			log.Println("⚠️  No cached devices and no login credentials — server will start but cloud sync is unavailable.")
		}
	}

	// Start periodic sync ticker.
	go cloudSyncLoop()

	// Set up HTTP server.
	mux := http.NewServeMux()

	// ── QR login endpoints ──────────────────────────────────────────────────
	mux.HandleFunc("/api/login/qr", handleQRPage)          // GET - browser-friendly QR page
	mux.HandleFunc("/api/login/qr/", handleQRPage)         // GET - alias
	mux.HandleFunc("/api/login/qr/start", handleQRStart)   // POST - API
	mux.HandleFunc("/api/login/qr/status", handleQRStatus) // GET
	mux.HandleFunc("/api/login/qr/collect", handleQRCollect) // POST
	mux.HandleFunc("/api/login/qr/cancel", handleQRCancel)  // POST
	mux.HandleFunc("/api/login/qr/image", handleQRImage)    // GET - PNG or HTML

	// ── OpenClaw / AI Agent endpoints ──────────────────────────────────────
	mux.HandleFunc("/openclaw/schema", handleSchema)
	mux.HandleFunc("/openclaw/schema/", handleSchema)

	// ── Device control endpoints ───────────────────────────────────────────
	mux.HandleFunc("/api/control", handleControl)
	mux.HandleFunc("/api/control/", handleControl)
	mux.HandleFunc("/api/devices", handleListDevices)
	mux.HandleFunc("/api/devices/", handleDeviceByID)
	mux.HandleFunc("/api/sync", handleSync)
	mux.HandleFunc("/api/health", handleHealth)

	// ── Root ───────────────────────────────────────────────────────────────
	mux.HandleFunc("/", handleRoot)

	addr := fmt.Sprintf("%s:%d", cfg.Server.Host, cfg.Server.Port)
	server := &http.Server{
		Addr:         addr,
		Handler:      withLogging(mux),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 15 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Graceful shutdown.
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		log.Println("🛑 Shutting down...")
		xiaomi.CancelQRLogin()
		server.Close()
	}()

	log.Printf("🚀 ha-lite server listening on %s", addr)
	log.Printf("🤖 OpenClaw schema: http://<pi-ip>:%d/openclaw/schema", cfg.Server.Port)
	log.Printf("📋 Device list:    http://<pi-ip>:%d/api/devices", cfg.Server.Port)
	log.Printf("📱 QR login:       http://<pi-ip>:%d/api/login/qr (open in browser)", cfg.Server.Port)

	if err := server.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatalf("❌ Server error: %v", err)
	}
	log.Println("👋 Server stopped.")
}

// ── Cloud sync ────────────────────────────────────────────────────────────────

func syncFromCloud() error {
	// Try password login first.
	if cfg.Xiaomi.HasPasswordAuth() {
		if err := cloud.Login(); err != nil {
			return fmt.Errorf("cloud login: %w", err)
		}
	} else if !cloud.HasCredentials() {
		// Try QR login credentials from the active QR manager.
		qrMu.Lock()
		mgr := qrMgr
		qrMu.Unlock()
		if mgr != nil {
			if token, uid, ok := mgr.GetCredentials(); ok && token != "" {
				cloud.SetCredentials(token, uid)
			}
			if ss := mgr.Ssecurity(); ss != "" {
				cloud.SetSsecurity(ss)
			}
		}
		if !cloud.HasCredentials() {
			return fmt.Errorf("not authenticated: configure password or complete QR login (POST /api/login/qr/start)")
		}
	} else {
		// Already has credentials from previous QR login.
	}

	devices, err := cloud.DeviceList()
	if err != nil {
		// If device list fails, may be token expired. Clear and let caller retry.
		return fmt.Errorf("device list: %w", err)
	}

	log.Printf("☁️  Cloud returned %d devices", len(devices))
	for _, d := range devices {
		log.Printf("   • %s (%s) → %s", d.Name, d.Model, d.IP)
	}

	updated := reg.MergeFromCloud(devices)
	if updated > 0 {
		if err := reg.SaveCache(); err != nil {
			log.Printf("⚠️  Cache save: %v", err)
		}
		log.Printf("💾 Registry updated: %d device(s) changed, cached to %s", updated, cfg.Registry.CacheFile)
	}

	return nil
}

func cloudSyncLoop() {
	ticker := time.NewTicker(12 * time.Hour)
	defer ticker.Stop()

	for range ticker.C {
		log.Println("⏰ Scheduled cloud sync triggered")
		if err := syncFromCloud(); err != nil {
			log.Printf("⚠️  Scheduled sync failed: %v", err)
		}
	}
}

// ── QR Login Handlers ─────────────────────────────────────────────────────────

// handleQRPage is the browser-friendly QR login page.
// GET /api/login/qr — starts QR login (if not already active) and returns
// an interactive HTML page with the QR code, direct Xiaomi login link, and
// auto-refresh on scan detection.
func handleQRPage(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	if path != "/api/login/qr" && path != "/api/login/qr/" {
		writeJSON(w, http.StatusNotFound, map[string]interface{}{"error": "not found"})
		return
	}

	// If already authenticated via cloud client, don't start a new QR.
	if cloud != nil && cloud.HasCredentials() {
		qrMu.Lock()
		qrStatus = "authenticated"
		qrMu.Unlock()
		// Redirect to devices page.
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(fmt.Sprintf(`<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/api/devices"><title>ha-lite</title></head><body style="font-family:sans-serif;text-align:center;padding:40px;background:#1a1a2e;color:#e0e0e0;"><p>✅ Already logged in. <a href="/api/devices" style="color:#64ffda;">View devices</a></p></body></html>`)))
		return
	}

	// Start QR login if not already active.
	qrMu.Lock()
	needsStart := qrMgr == nil || qrStatus == "idle" || qrStatus == "timeout" || qrStatus == "error" || qrStatus == "cancelled"
	if needsStart {
		if qrMgr != nil {
			qrMgr.Cancel()
		}
		qrMgr = xiaomi.NewQRLoginManager(cfg.Xiaomi.Region, 120)
		qrStatus = "starting"

		host := strings.Split(r.Host, ":")[0]
		if host == "" || host == "0.0.0.0" {
			host = cfg.Server.Host
		}
		state, err := qrMgr.Start(host, cfg.Server.Port)
		if err != nil {
			qrStatus = "error"
			qrMessage = err.Error()
			qrMu.Unlock()
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.WriteHeader(http.StatusInternalServerError)
			fmt.Fprintf(w, errorHTML, err.Error())
			return
		}
		qrImagePNG = qrMgr.QRImagePNG()
		qrStatus = state.Status
		qrMessage = state.Message

		qrArt := qrMgr.QRAsciiArt()
		if qrArt != "" {
			log.Printf("📱 QR Code for Xiaomi login:\n%s", qrArt)
		}
		if loginURL := qrMgr.LoginURL(); loginURL != "" {
			log.Printf("🔗 Alternatively open: %s", loginURL)
		}
	}
	status := qrStatus
	mgr := qrMgr
	img := qrImagePNG
	qrMu.Unlock()

	// Sync status from the manager instance (may be ahead of the global variable).
	if mgr != nil {
		mgrState := mgr.Status()
		if mgrState != nil {
			if mgrState.Status == "authenticated" || mgrState.ServiceToken {
				status = "authenticated"
			} else if mgrState.Status != "" {
				status = mgrState.Status
			}
		}
	}

	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	serverURL := fmt.Sprintf("%s://%s", scheme, r.Host)

	// If already authenticated, redirect immediately.
	if status == "authenticated" {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(fmt.Sprintf(`<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/api/devices"><title>ha-lite</title></head><body style="font-family:sans-serif;text-align:center;padding:40px;background:#1a1a2e;color:#e0e0e0;"><p>✅ Login complete. <a href="/api/devices" style="color:#64ffda;">View devices</a></p></body></html>`)))
		return
	}

	var loginURL string
	if mgr != nil {
		loginURL = mgr.LoginURL()
	}

	page := qrLoginPageHTML(img, loginURL, serverURL, status)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Write([]byte(page))
}

// handleQRStart starts the QR code login flow (POST API).
func handleQRStart(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]interface{}{"error": "POST only"})
		return
	}

	qrMu.Lock()
	defer qrMu.Unlock()

	// Cancel any existing QR login.
	if qrMgr != nil {
		qrMgr.Cancel()
	}

	qrMgr = xiaomi.NewQRLoginManager(cfg.Xiaomi.Region, 120)
	qrStatus = "starting"

	// Get local IP from the request host or use configured host.
	host := strings.Split(r.Host, ":")[0]
	if host == "" || host == "0.0.0.0" {
		host = cfg.Server.Host
	}

	state, err := qrMgr.Start(host, cfg.Server.Port)
	if err != nil {
		qrStatus = "error"
		qrMessage = err.Error()
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{
			"status":  "error",
			"message": err.Error(),
		})
		return
	}

	qrImagePNG = qrMgr.QRImagePNG()
	qrStatus = state.Status
	qrMessage = state.Message

	// Generate QR ASCII art for terminal display.
	qrArt := qrMgr.QRAsciiArt()
	if qrArt != "" {
		// Print QR code to server logs for terminal visibility.
		log.Printf("📱 QR Code for Xiaomi login:\n%s", qrArt)
	}

	// Build response with QR image URL, base64 data, and ASCII art.
	resp := map[string]interface{}{
		"status":          "waiting",
		"message":         "Scan the QR code with Mi Home app (Profile → top-right → Scan)",
		"qr_image_url":    fmt.Sprintf("http://%s:%d/api/login/qr/image", host, cfg.Server.Port),
		"qr_image_page":   fmt.Sprintf("http://%s:%d/api/login/qr/image", host, cfg.Server.Port),
		"login_url":       state.LoginURL,
		"next_step":       "Check status: GET /api/login/qr/status. After scan: POST /api/login/qr/collect",
		"timeout_seconds": 120,
	}

	if state.QRImageB64 != "" {
		resp["qr_image_b64"] = state.QRImageB64
		resp["qr_image_b64_data_uri"] = state.QRImageB64DataURI
	}
	if qrArt != "" {
		resp["qr_ascii_art"] = qrArt
	}

	writeJSON(w, http.StatusOK, resp)
}

// handleQRStatus returns the current QR login status.
func handleQRStatus(w http.ResponseWriter, r *http.Request) {
	qrMu.Lock()
	globalStatus := qrStatus
	globalMsg := qrMessage
	mgr := qrMgr
	qrMu.Unlock()

	// Use the manager's internal status if more up-to-date.
	status := globalStatus
	message := globalMsg
	hasToken := false
	if mgr != nil {
		// The manager's long-poll goroutine updates its internal status.
		// Check credentials first (most authoritative signal).
		if token, _, ok := mgr.GetCredentials(); ok && token != "" {
			hasToken = true
			status = "authenticated"
			message = "Login successful"
		}
		// Also sync back if manager status is ahead.
		mgrState := mgr.Status()
		if mgrState != nil {
			if mgrState.Status == "authenticated" || mgrState.ServiceToken {
				status = "authenticated"
				hasToken = true
			} else if mgrState.Status == "scanned" && status == "waiting" {
				status = "scanned"
			} else if mgrState.Status == "timeout" {
				status = "timeout"
				message = mgrState.Message
			}
		}
	}
	if cloud != nil && cloud.HasCredentials() {
		hasToken = true
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":           status,
		"message":          message,
		"has_service_token": hasToken,
	})
}

// handleQRCollect collects the QR login result and syncs devices.
func handleQRCollect(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]interface{}{"error": "POST only"})
		return
	}

	qrMu.Lock()
	currentStatus := qrStatus
	mgr := qrMgr
	qrMu.Unlock()

	// Check credentials: first from the active QR manager, then from global.
	haveCreds := false
	var token, uid string
	if mgr != nil {
		if t, u, ok := mgr.GetCredentials(); ok && t != "" {
			token, uid, haveCreds = t, u, true
		}
	}
	if !haveCreds {
		if t, u, ok := xiaomi.GetQRLoginCredentials(); ok && t != "" {
			token, uid, haveCreds = t, u, true
		}
	}
	if cloud.HasCredentials() {
		haveCreds = true
	}

	if !haveCreds && currentStatus != "authenticated" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"status":  "not_ready",
			"message": "QR login not yet complete. Current status: " + currentStatus,
			"hint":    "Check GET /api/login/qr/status first. Scan the QR with Mi Home app.",
		})
		return
	}

	if !cloud.HasCredentials() && token != "" {
		cloud.SetCredentials(token, uid)
	}
	// Sync ssecurity from QR login to cloud client for encrypted API calls.
	if mgr != nil {
		if ss := mgr.Ssecurity(); ss != "" {
			cloud.SetSsecurity(ss)
			log.Printf("[ssecurity] synced to cloud client (len=%d)", len(ss))
		} else {
			log.Printf("[ssecurity] WARNING: mgr.Ssecurity() is empty!")
		}
	} else {
		log.Printf("[ssecurity] WARNING: mgr is nil!")
	}
	qrMu.Lock()
	if qrStatus != "authenticated" {
		qrStatus = "authenticated"
	}
	qrMu.Unlock()

	// Sync devices from cloud.
	if err := syncFromCloud(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{
			"status":  "login_ok_sync_failed",
			"message": fmt.Sprintf("QR login succeeded but device sync failed: %v", err),
		})
		return
	}

	devices := reg.GetAll()
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":  "success",
		"message": fmt.Sprintf("QR login complete. %d device(s) synced.", len(devices)),
		"count":   len(devices),
		"devices": devices,
	})
}

// handleQRCancel cancels the current QR login flow.
func handleQRCancel(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]interface{}{"error": "POST only"})
		return
	}

	xiaomi.CancelQRLogin()
	qrMu.Lock()
	qrStatus = "idle"
	qrImagePNG = nil
	qrMu.Unlock()

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status": "cancelled",
	})
}

// handleQRImage serves the QR code as an HTML page (for browser) or raw PNG.
// Query param: ?format=raw  → returns raw PNG image
// Otherwise returns a styled HTML page with the QR code embedded.
func handleQRImage(w http.ResponseWriter, r *http.Request) {
	qrMu.Lock()
	img := qrImagePNG
	mgr := qrMgr
	qrMu.Unlock()

	// Raw PNG mode: ?format=raw
	if r.URL.Query().Get("format") == "raw" {
		if len(img) == 0 {
			http.Error(w, "QR code not available", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store")
		w.Write(img)
		return
	}

	// HTML page mode (default).
	if len(img) == 0 {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte(`<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ha-lite QR Login</title></head><body style="font-family:sans-serif;text-align:center;padding:40px;background:#1a1a2e;color:#e0e0e0;"><h2>QR Code Not Available</h2><p>Start a QR login first: <code>POST /api/login/qr/start</code></p></body></html>`))
		return
	}

	// Build server URL from request.
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	serverURL := fmt.Sprintf("%s://%s", scheme, r.Host)

	var page string
	if mgr != nil {
		page = mgr.QRHTMLPage(serverURL, serverURL)
	} else {
		page = xiaomi.QRRenderHTML(img, serverURL)
	}

	// If page is empty (image decode failed), fall back to raw PNG.
	if page == "" {
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store")
		w.Write(img)
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Write([]byte(page))
}

// ── HTTP Handlers ─────────────────────────────────────────────────────────────

// handleSchema returns the OpenClaw-compatible tool schema.
func handleSchema(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, `{"error":"GET only"}`, http.StatusMethodNotAllowed)
		return
	}

	devices := reg.GetAll()

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"name":        "ha_lite_device_control",
		"description": "Control Xiaomi smart home devices on the local network via ha-lite. Supports lights, switches, plugs, and other MIoT devices. Devices are automatically synced with Xiaomi cloud to keep local tokens and IPs up to date. Login supports both password and QR code (Mi Home app scan).",
		"version":     version,
		"endpoints": map[string]interface{}{
			"control": map[string]interface{}{
				"method":      "POST",
				"path":        "/api/control",
				"description": "Send a control command to a specific device.",
				"payload": map[string]interface{}{
					"did":    "string (required) - Device unique ID",
					"action": "string (required) - Action: 'on', 'off', 'toggle', 'brightness:<0-100>', 'color_temp:<2700-6500>'",
				},
			},
			"list_devices": map[string]interface{}{
				"method":      "GET",
				"path":        "/api/devices",
				"description": "List all registered devices with their current state.",
			},
			"sync": map[string]interface{}{
				"method":      "POST",
				"path":        "/api/sync",
				"description": "Force a cloud sync to refresh device tokens and IPs.",
			},
			"health": map[string]interface{}{
				"method":      "GET",
				"path":        "/api/health",
				"description": "Health check and server status.",
			},
			"qr_login": map[string]interface{}{
				"method":      "POST",
				"path":        "/api/login/qr/start",
				"description": "Start QR code login (scan with Mi Home app). No password needed.",
			},
		},
		"devices": buildDeviceSchemaList(devices),
		"parameters": map[string]interface{}{
			"type": "object",
			"properties": map[string]interface{}{
				"did": map[string]interface{}{
					"type":        "string",
					"description": "Device unique ID (DID). Get from /api/devices.",
				},
				"action": map[string]interface{}{
					"type":        "string",
					"description": "Action: 'on', 'off', 'toggle', or device-specific commands.",
				},
			},
			"required": []string{"did", "action"},
		},
	})
}

// buildDeviceSchemaList builds a list of device descriptions for the schema.
func buildDeviceSchemaList(devices []*registry.DeviceInfo) []map[string]interface{} {
	result := make([]map[string]interface{}, 0, len(devices))
	for _, d := range devices {
		entry := map[string]interface{}{
			"did":    d.DID,
			"name":   d.Name,
			"model":  d.Model,
			"online": d.Online,
			"ip":     d.IP,
		}

		caps := inferCapabilities(d.Model)
		if len(caps) > 0 {
			entry["capabilities"] = caps
		}

		result = append(result, entry)
	}
	return result
}

func inferCapabilities(model string) []string {
	model = strings.ToLower(model)
	caps := []string{"on", "off", "toggle"}

	switch {
	case strings.Contains(model, "light") || strings.Contains(model, "lamp") || strings.Contains(model, "bulb"):
		caps = append(caps, "brightness:<0-100>", "color_temp:<2700-6500>")
	case strings.Contains(model, "plug") || strings.Contains(model, "outlet") || strings.Contains(model, "socket"):
		// Basic switch only.
	case strings.Contains(model, "curtain") || strings.Contains(model, "blind"):
		caps = append(caps, "position:<0-100>", "pause")
	case strings.Contains(model, "air") || strings.Contains(model, "purifier"):
		caps = append(caps, "fan_speed:<0-3>", "mode:<auto,sleep,manual>")
	case strings.Contains(model, "humidifier"):
		caps = append(caps, "humidity:<30-80>", "fan_speed:<0-3>")
	case strings.Contains(model, "ac") || strings.Contains(model, "aircondition"):
		caps = append(caps, "temperature:<16-30>", "mode:<cool,heat,auto,fan,dry>", "fan_speed:<0-3>")
	case strings.Contains(model, "fan") || strings.Contains(model, "ventilator"):
		caps = append(caps, "fan_speed:<0-3>", "oscillate:<on,off>")
	case strings.Contains(model, "heater"):
		caps = append(caps, "temperature:<16-30>")
	}

	return caps
}

// handleControl handles device control requests from AI agents.
func handleControl(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]interface{}{"error": "POST only"})
		return
	}

	var req struct {
		DID    string `json:"did"`
		Action string `json:"action"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{"error": "Invalid JSON body"})
		return
	}

	if req.DID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{"error": "Missing required field: did"})
		return
	}
	if req.Action == "" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{"error": "Missing required field: action"})
		return
	}

	dev, ok := reg.Get(req.DID)
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]interface{}{"error": fmt.Sprintf("Device not found: %s", req.DID)})
		return
	}

	result := controlDevice(dev, req.Action)
	writeJSON(w, http.StatusOK, result)
}

func controlDevice(dev *registry.DeviceInfo, action string) map[string]interface{} {
	miioDev := miio.NewDevice(dev.IP, dev.Token, dev.Model)

	// Handle toggle: read current power state, then flip.
	if strings.ToLower(strings.TrimSpace(action)) == "toggle" {
		statusCmd := map[string]interface{}{"method": "get_prop", "params": []interface{}{"power"}}
		resp, err := miioDev.Send(statusCmd)
		if err == nil {
			var result []interface{}
			if json.Unmarshal(resp, &result) == nil && len(result) > 0 {
				current := fmt.Sprintf("%v", result[0])
				current = strings.ToLower(current)
				if current == "on" {
					action = "off"
				} else {
					action = "on"
				}
				log.Printf("🔄 Toggle: current=%s → %s", current, action)
			}
		}
		// If toggle detection fails, default to "on".
		if action == "toggle" {
			action = "on"
		}
	}

	cmd := buildCommand(action)
	resp, err := miioDev.Send(cmd)
	if err == nil {
		return map[string]interface{}{
			"status":   "success",
			"did":      dev.DID,
			"name":     dev.Name,
			"action":   action,
			"via":      "local",
			"response": json.RawMessage(resp),
		}
	}

	log.Printf("⚠️  Local control failed for %s: %v. Attempting cloud refresh...", dev.Name, err)

	if syncErr := syncFromCloud(); syncErr != nil {
		log.Printf("❌ Cloud sync also failed: %v", syncErr)
		return map[string]interface{}{
			"status": "failed",
			"did":    dev.DID,
			"name":   dev.Name,
			"action": action,
			"error":  fmt.Sprintf("Local control failed and cloud sync failed: %v / %v", err, syncErr),
		}
	}

	updatedDev, ok := reg.Get(dev.DID)
	if !ok {
		return map[string]interface{}{
			"status": "failed",
			"did":    dev.DID,
			"error":  "Device disappeared after cloud sync",
		}
	}

	miioDev = miio.NewDevice(updatedDev.IP, updatedDev.Token, updatedDev.Model)
	resp, err = miioDev.Send(cmd)
	if err != nil {
		// Local UDP failed — try cloud control as last resort.
		log.Printf("⚠️  Local retry failed for %s: %v. Attempting cloud control...", dev.Name, err)
		if cloudErr := cloudControlDevice(updatedDev, action); cloudErr == nil {
			return map[string]interface{}{
				"status": "success",
				"did":    dev.DID,
				"name":   dev.Name,
				"action": action,
				"via":    "cloud",
			}
		} else {
			log.Printf("❌ Cloud control also failed for %s: %v", dev.Name, cloudErr)
			return map[string]interface{}{
				"status": "failed",
				"did":    dev.DID,
				"name":   dev.Name,
				"action": action,
				"error":  fmt.Sprintf("Local + cloud control both failed: %v / %v", err, cloudErr),
			}
		}
	}

	return map[string]interface{}{
		"status":   "success",
		"did":      dev.DID,
		"name":     dev.Name,
		"action":   action,
		"via":      "local (token refreshed)",
		"response": json.RawMessage(resp),
	}

}

// cloudControlDevice sends a control command via Xiaomi cloud API.
func cloudControlDevice(dev *registry.DeviceInfo, action string) error {
	if cloud == nil || !cloud.HasCredentials() || cloud.Ssecurity() == "" {
		return fmt.Errorf("cloud credentials not available")
	}

	siid, piid, value := mapActionToMIoT(action)
	return cloud.DeviceControlEncrypted(dev.DID, siid, piid, value, cloud.Ssecurity())
}

// mapActionToMIoT maps a ha-lite action to MIoT siid/piid/value.
func mapActionToMIoT(action string) (siid, piid int, value interface{}) {
	action = strings.ToLower(strings.TrimSpace(action))
	switch {
	case action == "on" || action == "true":
		return 2, 1, true
	case action == "off" || action == "false":
		return 2, 1, false
	case strings.HasPrefix(action, "brightness:"):
		var level int
		fmt.Sscanf(action, "brightness:%d", &level)
		return 2, 2, level
	case strings.HasPrefix(action, "color_temp:"):
		var temp int
		fmt.Sscanf(action, "color_temp:%d", &temp)
		return 2, 3, temp
	default:
		return 2, 1, action == "on"
	}
}

func buildCommand(action string) map[string]interface{} {
	action = strings.ToLower(strings.TrimSpace(action))
	switch {
	case action == "on":
		return map[string]interface{}{"method": "set_power", "params": []interface{}{"on"}}
	case action == "off":
		return map[string]interface{}{"method": "set_power", "params": []interface{}{"off"}}
	case action == "toggle":
		return map[string]interface{}{"method": "toggle", "params": []interface{}{}}
	case action == "status":
		return map[string]interface{}{"method": "get_prop", "params": []interface{}{"power", "bright", "cct"}}
	case strings.HasPrefix(action, "brightness:"):
		val := strings.TrimPrefix(action, "brightness:")
		var level int
		fmt.Sscanf(val, "%d", &level)
		if level < 1 {
			level = 1
		}
		if level > 100 {
			level = 100
		}
		return map[string]interface{}{"method": "set_bright", "params": []interface{}{level}}
	case strings.HasPrefix(action, "color_temp:"):
		val := strings.TrimPrefix(action, "color_temp:")
		var temp int
		fmt.Sscanf(val, "%d", &temp)
		if temp < 2700 {
			temp = 2700
		}
		if temp > 6500 {
			temp = 6500
		}
		return map[string]interface{}{"method": "set_cct", "params": []interface{}{temp}}
	default:
		return map[string]interface{}{"method": "set_power", "params": []interface{}{action}}
	}
}

// handleListDevices returns all registered devices.
// By default, probes device reachability via miIO hello before returning.
// Use ?probe=false to skip probing for fast listing.
func handleListDevices(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]interface{}{"error": "GET only"})
		return
	}

	devices := reg.GetAll()

	// Probe device reachability via miIO RAW hello handshake.
	// Only the hello response determines online status — no IP filtering.
	if r.URL.Query().Get("probe") != "false" {
		probeOnline(devices)
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"count":   len(devices),
		"devices": devices,
	})
}

// probeOnline checks device reachability via miIO hello handshake.
// Each device is probed concurrently with a 2s timeout.
// Only the hello response determines online status — no IP filtering.
func probeOnline(devices []*registry.DeviceInfo) {
	var wg sync.WaitGroup
	for _, d := range devices {
		wg.Add(1)
		go func(d *registry.DeviceInfo) {
			defer wg.Done()
			online := miio.Ping(d.IP, 2*time.Second)
			reg.SetOnline(d.DID, online)
		}(d)
	}
	wg.Wait()
}

// handleDeviceByID handles GET /api/devices/<did>.
func handleDeviceByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]interface{}{"error": "GET only"})
		return
	}

	did := strings.TrimPrefix(r.URL.Path, "/api/devices/")
	did = strings.TrimPrefix(did, "/")
	if did == "" {
		handleListDevices(w, r)
		return
	}

	dev, ok := reg.Get(did)
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]interface{}{
			"error": fmt.Sprintf("Device not found: %s", did),
		})
		return
	}

	writeJSON(w, http.StatusOK, dev)
}

// handleSync forces a cloud sync.
func handleSync(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]interface{}{"error": "POST only"})
		return
	}

	if err := syncFromCloud(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{
			"status": "failed",
			"error":  err.Error(),
		})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":  "synced",
		"devices": reg.Count(),
	})
}

// handleHealth returns server health status.
func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":       "ok",
		"version":      version,
		"build_time":   buildTime,
		"device_count": reg.Count(),
		"cloud_authed": cloud.HasCredentials(),
		"qr_active":    qrStatus == "waiting",
		"uptime":       time.Now().Format(time.RFC3339),
	})
}

// handleRoot returns a simple info page.
func handleRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeJSON(w, http.StatusNotFound, map[string]interface{}{"error": "not found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"service": "ha-lite Server",
		"version": version,
		"endpoints": map[string]string{
			"/openclaw/schema":      "GET  - AI agent tool schema",
			"/api/devices":          "GET  - List all devices",
			"/api/devices/:did":     "GET  - Get device info",
			"/api/control":          "POST - Control a device (body: {did, action})",
			"/api/sync":             "POST - Force cloud sync",
			"/api/health":           "GET  - Health check",
			"/api/login/qr/start":   "POST - Start QR code login (Mi Home app scan)",
			"/api/login/qr/status":  "GET  - Check QR login status",
			"/api/login/qr/collect": "POST - Complete QR login and sync devices",
			"/api/login/qr/image":   "GET  - QR code image (PNG)",
		},
	})
}

// ── Helpers ────────────────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("⚠️  JSON encode error: %v", err)
	}
}

func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s %v", r.Method, r.URL.Path, r.RemoteAddr, time.Since(start).Round(time.Microsecond))
	})
}

// ── QR Login HTML page ────────────────────────────────────────────────────────

const errorHTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ha-lite QR Login</title></head>
<body style="font-family:sans-serif;text-align:center;padding:40px;background:#1a1a2e;color:#e0e0e0;">
<h2>⚠️ QR Login Failed</h2><p>%s</p>
<p><small>Check network to Xiaomi cloud. <a href="/api/login/qr" style="color:#64ffda;">Retry</a></small></p>
</body></html>`

// qrLoginPageHTML builds an interactive HTML page showing the QR code, a direct
// Xiaomi login URL fallback, and auto-refresh on scan detection.
func qrLoginPageHTML(pngData []byte, directURL, serverURL, status string) string {
	imgTag := ""
	if len(pngData) > 0 {
		b64 := base64.StdEncoding.EncodeToString(pngData)
		imgTag = fmt.Sprintf(`<img src="data:image/png;base64,%s" alt="QR Code" style="max-width:300px;height:auto;">`, b64)
	}

	directLinkHTML := ""
	if directURL != "" {
		directLinkHTML = fmt.Sprintf(`
<div class="fallback">
  <p>Or open this link on phone (requires Mi Home app):</p>
  <code style="word-break:break-all;font-size:0.75rem;color:#8892b0;">%s</code>
</div>`, directURL)
	}

	// Auto-refresh on scan: check status every 2s. On success, auto-collect.
	autoRefreshJS := fmt.Sprintf(`
<script>
(function poll() {
  var statusEl = document.getElementById('status');
  if (!statusEl) return;
  fetch('%s/api/login/qr/status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.has_service_token || d.status === 'authenticated') {
        statusEl.textContent = '✅ Login OK! Syncing devices...';
        statusEl.className = 'status success';
        fetch('%s/api/login/qr/collect', {method:'POST'})
          .then(function(r) { return r.json(); })
          .then(function(r) {
            if (r.status === 'success') {
              statusEl.innerHTML = '✅ Login complete! ' + r.count + ' device(s) synced. ' +
                '<br><a href="/api/devices" style="color:#64ffda;font-size:1.1em;">→ View device list</a>';
              // Auto-redirect to devices after 2s.
              setTimeout(function() { window.location.href = '/api/devices'; }, 2000);
            } else {
              statusEl.innerHTML = '⚠️ Login OK but sync failed: ' + (r.message || '');
            }
          })
          .catch(function() {
            statusEl.textContent = '⚠️ Sync request failed. Try POST /api/login/qr/collect manually.';
          });
      } else if (d.status === 'scanned') {
        statusEl.textContent = '📱 QR scanned! Completing login...';
        statusEl.className = 'status scanning';
        setTimeout(poll, 1500);
      } else if (d.status === 'timeout') {
        statusEl.innerHTML = '⏰ QR code expired. <a href="/api/login/qr" style="color:#64ffda;">Click to refresh</a>';
        statusEl.className = 'status error';
      } else if (d.status === 'error') {
        statusEl.innerHTML = '❌ Error: ' + (d.message || 'unknown') + ' <a href="/api/login/qr" style="color:#64ffda;">Retry</a>';
        statusEl.className = 'status error';
      } else {
        statusEl.textContent = '⏳ Waiting for scan...';
        statusEl.className = 'status';
        setTimeout(poll, 2000);
      }
    })
    .catch(function() {
      setTimeout(poll, 3000);
    });
})();
</script>`, serverURL, serverURL)

	return fmt.Sprintf(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ha-lite QR Login</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 100vh;
    padding: 20px;
  }
  .card {
    background: #16213e; border-radius: 16px;
    padding: 32px 40px; text-align: center;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    max-width: 520px; width: 100%%;
  }
  h1 { font-size: 1.5rem; margin-bottom: 4px; color: #fff; }
  .subtitle { color: #8892b0; font-size: 0.9rem; margin-bottom: 20px; }
  .qr-box {
    background: #ffffff; border-radius: 12px;
    padding: 16px; display: inline-block; margin-bottom: 20px;
  }
  .qr-box img { display: block; }
  .steps {
    text-align: left; margin-bottom: 20px;
    color: #8892b0; font-size: 0.9rem; line-height: 1.8;
  }
  .steps span { color: #64ffda; font-weight: bold; margin-right: 6px; }
  .status {
    background: #0f3460; border-radius: 8px;
    padding: 10px 16px; margin-bottom: 16px;
    color: #64ffda; font-size: 0.9rem; font-weight: bold;
  }
  .status.success { background: #0a3d2e; color: #64ffda; }
  .status.scanning { background: #3d2e0a; color: #ffd464; }
  .status.error { background: #3d0a0a; color: #ff6464; }
  .fallback {
    border-top: 1px solid #2a3a5c; padding-top: 16px; margin-top: 16px;
    color: #8892b0; font-size: 0.85rem;
  }
  .direct-link {
    display: inline-block; margin: 8px 0; padding: 8px 16px;
    background: #0f3460; color: #64ffda; border-radius: 6px;
    font-size: 0.8rem; word-break: break-all; text-decoration: none;
  }
  .direct-link:hover { background: #1a4a7a; }
  .hint { font-size: 0.75rem; color: #575d6e; margin-top: 4px; }
  .footer { margin-top: 16px; color: #575d6e; font-size: 0.75rem; }
  a { color: #64ffda; }
</style>
</head>
<body>
<div class="card">
  <h1>🏠 ha-lite QR Login</h1>
  <p class="subtitle">Scan with Mi Home app to login</p>
  <div class="qr-box">%s</div>
  <div id="status" class="status">%s</div>
  <div class="steps">
    <p><span>1.</span> Open <b>Mi Home / 米家</b> app on your phone</p>
    <p><span>2.</span> Go to <b>Profile → top-right → Scan</b></p>
    <p><span>3.</span> Scan the QR code above</p>
    <p><span>4.</span> Login completes automatically ✨</p>
  </div>
  %s
  <p class="footer">ha-lite server • session expires in 120s</p>
</div>
%s
</body>
</html>`, imgTag, statusMsg(status), directLinkHTML, autoRefreshJS)
}

func statusMsg(s string) string {
	switch s {
	case "authenticated":
		return "✅ Already logged in! Redirecting to device list..."
	case "scanned":
		return "📱 Scan detected! Completing login..."
	case "timeout":
		return "⏰ QR code expired. <a href='/api/login/qr'>Click to refresh</a>"
	case "error":
		return "❌ Error. <a href='/api/login/qr'>Retry</a>"
	default:
		return "⏳ Waiting for scan..."
	}
}

