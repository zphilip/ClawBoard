package xiaomi

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"sync"
	"time"
)

// QRLoginManager handles the Xiaomi QR code login flow.
// Phase 1: get QR URLs from Xiaomi, download the QR image, start long-poll.
// Phase 2: detect scan, exchange for service token.
type QRLoginManager struct {
	mu sync.Mutex

	region      string
	client      *http.Client
	timeoutSecs int

	// Phase 1 state.
	qrImageURL     string
	loginURL       string
	longPollURL    string
	qrImageBytes   []byte
	qrPathToken    string
	detectedRegion string // e.g. "sgp" from login URL domain

	// Phase 2 result.
	status       string // "waiting", "scanned", "timeout", "error"
	statusMsg    string
	serviceToken string
	userID       string
	ssecurity    string
	passToken    string
	cUserId      string
	location     string

	// Poll control.
	done   chan struct{}
	stopCh chan struct{}
}

// NewQRLoginManager creates a new QR login manager.
func NewQRLoginManager(region string, timeoutSecs int) *QRLoginManager {
	jar, _ := cookiejar.New(nil)
	if timeoutSecs <= 0 {
		timeoutSecs = 120
	}
	return &QRLoginManager{
		region:      region,
		timeoutSecs: timeoutSecs,
		status:      "idle",
		client: &http.Client{
			Jar:     jar,
			Timeout: 30 * time.Second,
		},
	}
}

// QRLoginState holds the current QR login state for API responses.
type QRLoginState struct {
	Status           string `json:"status"`
	QRImageURL       string `json:"qr_image_url,omitempty"`
	QRImageB64       string `json:"qr_image_b64,omitempty"`
	QRImageB64DataURI string `json:"qr_image_b64_data_uri,omitempty"`
	QRAsciiArt       string `json:"qr_ascii_art,omitempty"`
	LoginURL         string `json:"login_url,omitempty"`
	Message          string `json:"message,omitempty"`
	ServiceToken     bool   `json:"has_service_token"`
}

// Start begins the QR login flow. Returns the QR image URL and base64 data.
func (m *QRLoginManager) Start(host string, port int) (*QRLoginState, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Step 1: Get QR URLs from Xiaomi.
	url := "https://account.xiaomi.com/longPolling/loginUrl"
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	q := req.URL.Query()
	q.Set("_qrsize", "480")
	q.Set("qs", "%3Fsid%3Dxiaomiio%26_json%3Dtrue")
	q.Set("callback", "https://sts.api.io.mi.com/sts")
	q.Set("_hasLogo", "false")
	q.Set("sid", "xiaomiio")
	q.Set("serviceParam", "")
	q.Set("_locale", "en_GB")
	q.Set("_dc", fmt.Sprintf("%d", time.Now().UnixMilli()))
	req.URL.RawQuery = q.Encode()

	// Mimic Mi Home app user agent.
	req.Header.Set("User-Agent", "MIoT/Android APP/com.xiaomi.mihome APPV/10.5.201")
	req.Header.Set("Accept-Encoding", "identity")

	resp, err := m.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("get login URL: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	bodyStr := trimPrefix(string(body), "&&&START&&&")

	var result struct {
		QR       string `json:"qr"`
		LoginURL string `json:"loginUrl"`
		LP       string `json:"lp"`
		Timeout  int    `json:"timeout"`
	}
	if err := json.Unmarshal([]byte(bodyStr), &result); err != nil {
		return nil, fmt.Errorf("parse login URL response: %w (body=%s)", err, bodyStr[:min(200, len(bodyStr))])
	}

	if result.QR == "" {
		return nil, fmt.Errorf("no QR URL in response")
	}

	m.qrImageURL = result.QR
	m.loginURL = result.LoginURL
	m.longPollURL = result.LP
	if result.Timeout > 0 && result.Timeout < m.timeoutSecs {
		m.timeoutSecs = result.Timeout
	}
	// Detect real region from login URL domain (e.g. sgp.account.xiaomi.com → sgp).
	m.detectedRegion = detectRegion(m.loginURL)

	// Step 2: Download the QR image.
	imgResp, err := m.client.Get(m.qrImageURL)
	if err != nil {
		return nil, fmt.Errorf("download QR image: %w", err)
	}
	defer imgResp.Body.Close()

	m.qrImageBytes, _ = io.ReadAll(imgResp.Body)
	if len(m.qrImageBytes) == 0 {
		return nil, fmt.Errorf("empty QR image")
	}

	// Step 3: Start long-poll in background.
	m.status = "waiting"
	m.done = make(chan struct{})
	m.stopCh = make(chan struct{})

	go m.longPoll()

	return &QRLoginState{
		Status:      "waiting",
		QRImageB64:  base64.StdEncoding.EncodeToString(m.qrImageBytes),
		QRImageB64DataURI: "data:image/png;base64," + base64.StdEncoding.EncodeToString(m.qrImageBytes),
		LoginURL:    m.loginURL,
		Message:     "Open the QR image URL and scan with Mi Home app (Profile → top-right → Scan)",
	}, nil
}

// longPoll keeps the Xiaomi session alive and waits for scan detection.
func (m *QRLoginManager) longPoll() {
	defer close(m.done)

	start := time.Now()
	firstPoll := true
	for {
		select {
		case <-m.stopCh:
			return
		default:
		}

		if time.Since(start) > time.Duration(m.timeoutSecs)*time.Second {
			m.mu.Lock()
			m.status = "timeout"
			m.statusMsg = "QR code expired: no scan detected within timeout"
			m.mu.Unlock()
			fmt.Printf("[qrlogin] timed out after %ds\n", m.timeoutSecs)
			return
		}

		if firstPoll {
			fmt.Printf("[qrlogin] starting LP poll, URL=%s\n", m.longPollURL[:min(80, len(m.longPollURL))])
			firstPoll = false
		}

		req, err := http.NewRequest("GET", m.longPollURL, nil)
		if err != nil {
			fmt.Printf("[qrlogin] LP request error: %v\n", err)
			time.Sleep(2 * time.Second)
			continue
		}

		// Copy ALL cookies from the jar so the LP server sees our session.
		jarCookies := m.client.Jar.Cookies(req.URL)
		for _, c := range jarCookies {
			req.AddCookie(c)
		}

		// Use a client with a timeout — the LP request should return quickly
		// when a scan is detected, or timeout so we can retry.
		lpClient := &http.Client{
			Jar:     m.client.Jar,
			Timeout: 15 * time.Second,
		}

		lpResp, err := lpClient.Do(req)
		if err != nil {
			// Timeout or network error — retry.
			if strings.Contains(err.Error(), "timeout") || strings.Contains(err.Error(), "deadline") {
				// Normal: LP held connection for 15s without scan.
			} else {
				fmt.Printf("[qrlogin] LP poll error: %v\n", err)
			}
			time.Sleep(2 * time.Second)
			continue
		}

		fmt.Printf("[qrlogin] LP response: HTTP %d\n", lpResp.StatusCode)

		if lpResp.StatusCode == 200 {
			body, _ := io.ReadAll(lpResp.Body)
			lpResp.Body.Close()

			bodyStr := trimPrefix(string(body), "&&&START&&&")
			fmt.Printf("[qrlogin] LP 200 body prefix: %s\n", bodyStr[:min(120, len(bodyStr))])

			// Use json.Decoder with UseNumber to preserve large integers
			// (Xiaomi userId can exceed float64 precision).
			dec := json.NewDecoder(strings.NewReader(bodyStr))
			dec.UseNumber()
			var raw map[string]interface{}
			if err := dec.Decode(&raw); err != nil {
				fmt.Printf("[qrlogin] LP 200 parse error: %v\n", err)
			} else {
				fmt.Printf("[qrlogin] ✅ scan detected! raw keys: ")
				for k := range raw {
					fmt.Printf("%s ", k)
				}
				fmt.Println()

				m.mu.Lock()
				m.status = "scanned"
				// Extract fields, handling both string and number types.
				m.userID = stringField(raw, "userId")
				m.ssecurity = stringField(raw, "ssecurity")
				m.cUserId = stringField(raw, "cUserId")
				m.passToken = stringField(raw, "passToken")
				m.location = stringField(raw, "location")
				m.mu.Unlock()

				fmt.Printf("[qrlogin] userId=%s ssecurity=%s\n", m.userID, m.ssecurity[:min(16, len(m.ssecurity))])

				// Exchange for service token.
				m.exchangeServiceToken()
				return
			}
		}
		lpResp.Body.Close()

		time.Sleep(2 * time.Second)
	}
}

// exchangeServiceToken exchanges the scan credentials for a service token.
// The LP endpoint returns a location URL that already contains auth tokens.
// We follow it to obtain the serviceToken cookie from Xiaomi's STS service.
func (m *QRLoginManager) exchangeServiceToken() {
	m.mu.Lock()
	location := m.location
	userID := m.userID
	ssecurity := m.ssecurity
	cUserId := m.cUserId
	passToken := m.passToken
	m.mu.Unlock()

	// Build the service token exchange URL.
	// The location from the LP response is the redirect URL that Mi Home app follows.
	// It typically points to Xiaomi's STS (Security Token Service).
	svcURL := location
	if svcURL == "" {
		// Fallback: build service login URL with ssecurity.
		svcURL = "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true"
	}

	// Use a fresh client for the token exchange (no timeout, follow redirects).
	svcClient := &http.Client{
		Jar: m.client.Jar,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			// Follow redirects, preserving the URL for cookie domain matching.
			if len(via) >= 10 {
				return fmt.Errorf("too many redirects")
			}
			return nil
		},
	}

	// First, set the auth cookies from the scan onto the cookie jar.
	// We need to parse the location URL to get the right domain.
	if locURL, err := parseURL(svcURL); err == nil {
		if ssecurity != "" {
			m.client.Jar.SetCookies(locURL, []*http.Cookie{
				{Name: "ssecurity", Value: ssecurity, Path: "/", Domain: locURL.Host},
				{Name: "userId", Value: userID, Path: "/", Domain: locURL.Host},
			})
		}
		if cUserId != "" {
			m.client.Jar.SetCookies(locURL, []*http.Cookie{
				{Name: "cUserId", Value: cUserId, Path: "/", Domain: locURL.Host},
			})
		}
		if passToken != "" {
			m.client.Jar.SetCookies(locURL, []*http.Cookie{
				{Name: "passToken", Value: passToken, Path: "/", Domain: locURL.Host},
			})
		}
	}

	// Also set cookies for account.xiaomi.com (the ssecurity is used there).
	acctURL, _ := parseURL("https://account.xiaomi.com")
	if acctURL != nil && ssecurity != "" {
		m.client.Jar.SetCookies(acctURL, []*http.Cookie{
			{Name: "ssecurity", Value: ssecurity, Path: "/", Domain: ".xiaomi.com"},
			{Name: "userId", Value: userID, Path: "/", Domain: ".xiaomi.com"},
			{Name: "cUserId", Value: cUserId, Path: "/", Domain: ".xiaomi.com"},
			{Name: "passToken", Value: passToken, Path: "/", Domain: ".xiaomi.com"},
		})
	}

	resp, err := svcClient.Get(svcURL)
	if err != nil {
		m.mu.Lock()
		m.status = "error"
		m.statusMsg = fmt.Sprintf("service token exchange failed: %v", err)
		m.mu.Unlock()
		return
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	bodyStr := trimPrefix(string(body), "&&&START&&&")

	var svcRaw map[string]interface{}
	json.Unmarshal([]byte(bodyStr), &svcRaw)

	svcToken := stringField(svcRaw, "serviceToken")
	svcUserID := stringField(svcRaw, "userId")

	// Also check the final URL's cookies.
	finalURL := resp.Request.URL
	if finalURL != nil {
		for _, ck := range m.client.Jar.Cookies(finalURL) {
			if ck.Name == "serviceToken" && ck.Value != "" {
				svcToken = ck.Value
			}
			if ck.Name == "userId" && ck.Value != "" {
				svcUserID = ck.Value
			}
		}
	}

	// Check account.xiaomi.com cookies too.
	if acctURL != nil {
		for _, ck := range m.client.Jar.Cookies(acctURL) {
			if ck.Name == "serviceToken" && ck.Value != "" && svcToken == "" {
				svcToken = ck.Value
			}
			if ck.Name == "userId" && ck.Value != "" && svcUserID == "" {
				svcUserID = ck.Value
			}
		}
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	if svcToken != "" {
		m.serviceToken = svcToken
		if svcUserID != "" {
			m.userID = svcUserID
		} else if userID != "" {
			m.userID = userID
		}
		m.status = "authenticated"
		m.statusMsg = "QR login successful"
		fmt.Printf("[qrlogin] ✅ authenticated! userId=%s serviceToken=%s\n", m.userID, svcToken[:min(16, len(svcToken))])
	} else {
		m.status = "error"
		code := stringField(svcRaw, "code")
		desc := stringField(svcRaw, "description")
		m.statusMsg = fmt.Sprintf("cannot get service token (code=%s, desc=%s, location=%s)",
			code, desc, location[:min(80, len(location))])
		fmt.Printf("[qrlogin] ❌ token exchange failed: %s\n", m.statusMsg)
	}
}

func parseURL(rawURL string) (*url.URL, error) {
	return url.Parse(rawURL)
}

// stringField extracts a string value from a map. Handles string, json.Number
// (preserves exact large integers), and numeric types from any JSON decoder.
func stringField(m map[string]interface{}, key string) string {
	v, ok := m[key]
	if !ok {
		return ""
	}
	switch val := v.(type) {
	case string:
		return val
	case json.Number:
		return val.String()
	case float64:
		// Fallback for standard json.Unmarshal (may lose precision on large ints).
		if val == float64(int64(val)) {
			return fmt.Sprintf("%d", int64(val))
		}
		return fmt.Sprintf("%v", val)
	default:
		return fmt.Sprintf("%v", val)
	}
}

// Status returns the current QR login state.
func (m *QRLoginManager) Status() *QRLoginState {
	m.mu.Lock()
	defer m.mu.Unlock()

	return &QRLoginState{
		Status:        m.status,
		Message:       m.statusMsg,
		ServiceToken:  m.serviceToken != "",
	}
}

// GetCredentials returns the service token and user ID after successful login.
func (m *QRLoginManager) GetCredentials() (serviceToken, userID string, ok bool) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.serviceToken != "" {
		return m.serviceToken, m.userID, true
	}
	return "", "", false
}

// Cancel stops the QR login flow.
func (m *QRLoginManager) Cancel() {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.stopCh != nil {
		select {
		case <-m.stopCh:
			// Already closed.
		default:
			close(m.stopCh)
		}
	}
	m.status = "cancelled"
	m.qrImageBytes = nil
}

// SetCredentials sets the service token and user ID directly (used after QR login).
func (c *CloudClient) SetCredentials(serviceToken, userID string) {
	c.serviceToken = serviceToken
	c.userID = userID
}

// SetRegion sets the region for the cloud client.
func (c *CloudClient) SetRegion(region string) {
	c.region = region
}

// SetSsecurity sets the ssecurity for encrypted API calls.
func (c *CloudClient) SetSsecurity(s string) {
	c.ssecurity = s
}

// QRImagePNG returns the raw QR image bytes for serving over HTTP.
func (m *QRLoginManager) QRImagePNG() []byte {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.qrImageBytes
}

// LoginURL returns the direct Xiaomi login URL (alternative to scanning the QR).
func (m *QRLoginManager) LoginURL() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.loginURL
}

// Ssecurity returns the ssecurity from the QR login scan.
func (m *QRLoginManager) Ssecurity() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.ssecurity
}

// DetectedRegion returns the region detected from the Xiaomi login URL domain.
func (m *QRLoginManager) DetectedRegion() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.detectedRegion
}

// detectRegion extracts the region code from a Xiaomi login URL.
// e.g. "https://sgp.account.xiaomi.com/..." → "sgp"
func detectRegion(loginURL string) string {
	u, err := parseURL(loginURL)
	if err != nil {
		return ""
	}
	parts := strings.Split(u.Host, ".")
	if len(parts) >= 3 && strings.Contains(parts[1], "account") {
		return parts[0]
	}
	return ""
}

// QRAsciiArt returns the QR code rendered as Unicode half-block art for
// terminal display. Returns empty string if no QR image is available.
func (m *QRLoginManager) QRAsciiArt() string {
	m.mu.Lock()
	data := m.qrImageBytes
	m.mu.Unlock()
	if len(data) == 0 {
		return ""
	}
	return QRRenderPNG(data, 60)
}

// QRHTMLPage returns an HTML page displaying the QR code for browser scanning.
func (m *QRLoginManager) QRHTMLPage(serverURL, message string) string {
	m.mu.Lock()
	data := m.qrImageBytes
	m.mu.Unlock()
	if len(data) == 0 {
		return ""
	}
	return QRRenderHTML(data, serverURL)
}

// QRPathToken returns a unique token for the QR image URL path.
func (m *QRLoginManager) QRPathToken() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.qrPathToken
}

// SetQRPathToken sets the QR image path token.
func (m *QRLoginManager) SetQRPathToken(token string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.qrPathToken = token
}

// Done returns a channel that closes when the QR login flow completes.
func (m *QRLoginManager) Done() <-chan struct{} {
	return m.done
}

// HasCredentials returns whether we have a valid service token.
func (c *CloudClient) HasCredentials() bool {
	return c.serviceToken != ""
}

// ── Helpers ────────────────────────────────────────────────────────────────────

func trimPrefix(s, prefix string) string {
	if len(s) >= len(prefix) && s[:len(prefix)] == prefix {
		return s[len(prefix):]
	}
	return s
}

// Global QR login manager instance for the server.
var (
	globalQRMgr     *QRLoginManager
	globalQRMgrMu   sync.Mutex
	globalQRImgData []byte
)

// InitQRLogin initializes the global QR login manager.
func InitQRLogin(region string) {
	globalQRMgrMu.Lock()
	defer globalQRMgrMu.Unlock()
	globalQRMgr = NewQRLoginManager(region, 120)
}

// StartQRLogin starts the QR login flow.
func StartQRLogin(host string, port int) (*QRLoginState, error) {
	globalQRMgrMu.Lock()
	mgr := globalQRMgr
	globalQRMgrMu.Unlock()

	if mgr == nil {
		return nil, fmt.Errorf("QR login not initialized")
	}

	state, err := mgr.Start(host, port)
	if err != nil {
		return nil, err
	}

	globalQRMgrMu.Lock()
	globalQRImgData = mgr.QRImagePNG()
	globalQRMgrMu.Unlock()

	return state, nil
}

// GetQRLoginStatus returns the current QR login status.
func GetQRLoginStatus() *QRLoginState {
	globalQRMgrMu.Lock()
	mgr := globalQRMgr
	globalQRMgrMu.Unlock()

	if mgr == nil {
		return &QRLoginState{Status: "not_initialized", Message: "QR login not started"}
	}

	return mgr.Status()
}

// GetQRImage returns the current QR image bytes.
func GetQRImage() []byte {
	globalQRMgrMu.Lock()
	defer globalQRMgrMu.Unlock()
	return globalQRImgData
}

// GetQRLoginCredentials returns credentials from a successful QR login.
func GetQRLoginCredentials() (serviceToken, userID string, ok bool) {
	globalQRMgrMu.Lock()
	mgr := globalQRMgr
	globalQRMgrMu.Unlock()

	if mgr == nil {
		return "", "", false
	}
	return mgr.GetCredentials()
}

// CancelQRLogin cancels the current QR login flow.
func CancelQRLogin() {
	globalQRMgrMu.Lock()
	mgr := globalQRMgr
	globalQRMgrMu.Unlock()

	if mgr != nil {
		mgr.Cancel()
	}
	globalQRImgData = nil
}