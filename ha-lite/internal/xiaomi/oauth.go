package xiaomi

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	OAuthClientID    = "2882303761520251711"
	OAuthCallbackPort = 8123
	OAuthCallbackPath = "/callback"
	OAuthScope        = "1 3 6000" // profile, open_id, smart home
	OAuthAuthURL      = "https://account.xiaomi.com/oauth2/authorize"
	OAuthTokenURL     = "https://ha.api.io.mi.com/app/v2/ha/oauth/get_token"
	OAuthAPIBase      = "https://ha.api.io.mi.com"
)

// OAuthClient manages Xiaomi OAuth 2.0 login for cloud API access.
// It uses HA's registered client_id and runs a local callback server
// on port 8123 to receive the authorization code.
type OAuthClient struct {
	mu sync.Mutex

	clientID    string
	redirectURI string
	state       string

	// Callback server.
	callbackServer *http.Server
	callbackHost   string

	// Auth result.
	authCode     string
	accessToken  string
	refreshToken string
	userID       string
	expiresAt    time.Time
	status       string // "idle", "waiting", "authenticated", "error"
	statusMsg    string

	// Token persistence.
	tokenCachePath string

	// Control.
	done chan struct{}
}

// NewOAuthClient creates a new OAuth client.
func NewOAuthClient(tokenCachePath string) *OAuthClient {
	if tokenCachePath == "" {
		tokenCachePath = "cache/oauth_token.json"
	}
	return &OAuthClient{
		clientID:       OAuthClientID,
		state:          randomString(16),
		status:         "idle",
		tokenCachePath: tokenCachePath,
		done:           make(chan struct{}),
	}
}

// LoadToken loads a saved OAuth token from disk.
func (c *OAuthClient) LoadToken() error {
	c.mu.Lock()
	defer c.mu.Unlock()

	data, err := os.ReadFile(c.tokenCachePath)
	if err != nil {
		return err
	}

	var token struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		UserID       string `json:"user_id"`
		ExpiresAt    int64  `json:"expires_at"`
	}
	if err := json.Unmarshal(data, &token); err != nil {
		return fmt.Errorf("parse oauth token: %w", err)
	}

	if token.ExpiresAt > 0 && time.Now().Unix() > token.ExpiresAt {
		return fmt.Errorf("oauth token expired")
	}

	c.accessToken = token.AccessToken
	c.refreshToken = token.RefreshToken
	c.userID = token.UserID
	c.expiresAt = time.Unix(token.ExpiresAt, 0)
	c.status = "authenticated"
	log.Printf("[oauth] Loaded saved token (expires in %s)", time.Until(c.expiresAt).Round(time.Second))
	return nil
}

// saveTokenLocked persists the token to disk. Caller must hold c.mu.
func (c *OAuthClient) saveTokenLocked() {
	dir := filepath.Dir(c.tokenCachePath)
	if dir != "" && dir != "." {
		os.MkdirAll(dir, 0755)
	}

	token := map[string]interface{}{
		"access_token":  c.accessToken,
		"refresh_token": c.refreshToken,
		"user_id":       c.userID,
		"expires_at":    c.expiresAt.Unix(),
	}
	data, _ := json.MarshalIndent(token, "", "  ")
	os.WriteFile(c.tokenCachePath, data, 0600)
}

// StartAuth starts the OAuth flow: launches the callback server and returns
// the authorization URL to open in a browser.
func (c *OAuthClient) StartAuth(host string) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Stop any existing flow.
	c.stopCallbackLocked()

	c.authCode = ""
	c.status = "waiting"
	c.statusMsg = ""
	c.state = randomString(16)
	c.callbackHost = host

	// Build redirect URI.
	c.redirectURI = fmt.Sprintf("http://%s:%d%s", host, OAuthCallbackPort, OAuthCallbackPath)

	// Start callback server on port 8123.
	c.callbackServer = &http.Server{
		Addr:    fmt.Sprintf(":%d", OAuthCallbackPort),
		Handler: c.callbackHandler(),
	}
	c.done = make(chan struct{})

	go func() {
		if err := c.callbackServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("[oauth] callback server error: %v", err)
		}
	}()

	// Build OAuth authorization URL.
	params := url.Values{}
	params.Set("client_id", c.clientID)
	params.Set("redirect_uri", c.redirectURI)
	params.Set("response_type", "code")
	params.Set("scope", OAuthScope)
	params.Set("state", c.state)
	params.Set("skip_confirm", "true")

	authURL := fmt.Sprintf("%s?%s", OAuthAuthURL, params.Encode())
	log.Printf("[oauth] Auth URL: %s", authURL)
	log.Printf("[oauth] Callback server listening on :%d", OAuthCallbackPort)

	return authURL, nil
}

// callbackHandler returns the HTTP handler for the OAuth callback server.
func (c *OAuthClient) callbackHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(OAuthCallbackPath, func(w http.ResponseWriter, r *http.Request) {
		code := r.URL.Query().Get("code")
		state := r.URL.Query().Get("state")

		c.mu.Lock()
		defer c.mu.Unlock()

		if code == "" {
			c.status = "error"
			c.statusMsg = "No authorization code received"
			w.WriteHeader(http.StatusBadRequest)
			w.Write([]byte("<html><body><h2 style='color:red;'>Authorization Failed</h2><p>No code received.</p></body></html>"))
			return
		}

		if state != "" && state != c.state {
			c.status = "error"
			c.statusMsg = "State mismatch"
			w.WriteHeader(http.StatusBadRequest)
			w.Write([]byte("<html><body><h2 style='color:red;'>Authorization Failed</h2><p>State mismatch.</p></body></html>"))
			return
		}

		c.authCode = code
		c.status = "authorized"

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;text-align:center;padding:40px;">
<h2 style="color:green;">Authorization Successful!</h2>
<p>You can close this window and return to the dashboard.</p>
</body></html>`))
	})

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})

	return mux
}

// stopCallbackLocked stops the callback server. Caller must hold c.mu.
func (c *OAuthClient) stopCallbackLocked() {
	if c.callbackServer != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		c.callbackServer.Shutdown(ctx)
		c.callbackServer = nil
	}
}

// StopCallback stops the callback server.
func (c *OAuthClient) StopCallback() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stopCallbackLocked()
}

// ExchangeCode exchanges the authorization code for an access token.
func (c *OAuthClient) ExchangeCode() error {
	c.mu.Lock()
	code := c.authCode
	redirectURI := c.redirectURI
	c.mu.Unlock()

	if code == "" {
		return fmt.Errorf("no authorization code available")
	}

	// Xiaomi's custom OAuth token endpoint: GET with data as query param.
	reqData := map[string]interface{}{
		"client_id":     c.clientID,
		"code":          code,
		"redirect_uri":  redirectURI,
		"grant_type":    "authorization_code",
	}
	dataBytes, _ := json.Marshal(reqData)
	tokenURL := fmt.Sprintf("%s?data=%s", OAuthTokenURL, url.QueryEscape(string(dataBytes)))

	req, err := http.NewRequest("GET", tokenURL, nil)
	if err != nil {
		return fmt.Errorf("create token request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("X-Client-BizId", "haapi")

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("token exchange: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	log.Printf("[oauth] Token exchange response (HTTP %d): %s", resp.StatusCode, string(body[:min(len(body), 500)]))

	var result struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int    `json:"expires_in"`
		UserID       string `json:"user_id"`
		Code         int    `json:"code"`
		Message      string `json:"message"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return fmt.Errorf("parse token response: %w (body=%s)", err, string(body[:min(len(body), 200)]))
	}

	if result.AccessToken == "" {
		if result.Message != "" {
			return fmt.Errorf("token exchange failed: %s (code=%d)", result.Message, result.Code)
		}
		return fmt.Errorf("token exchange failed: no access_token in response")
	}

	c.mu.Lock()
	c.accessToken = result.AccessToken
	c.refreshToken = result.RefreshToken
	c.userID = result.UserID
	if result.ExpiresIn > 0 {
		c.expiresAt = time.Now().Add(time.Duration(float64(result.ExpiresIn)*0.7) * time.Second)
	} else {
		c.expiresAt = time.Now().Add(30 * 24 * time.Hour) // 30 days default
	}
	c.status = "authenticated"
	c.statusMsg = "Login successful"
	c.saveTokenLocked()
	c.mu.Unlock()

	// Stop the callback server — it's no longer needed.
	c.StopCallback()

	log.Printf("[oauth] Token exchange success (expires in %s)", time.Until(c.expiresAt).Round(time.Second))
	return nil
}

// DeviceList fetches the device list via OAuth Bearer token.
func (c *OAuthClient) DeviceList() ([]DeviceInfo, error) {
	c.mu.Lock()
	token := c.accessToken
	c.mu.Unlock()

	if token == "" {
		return nil, fmt.Errorf("not authenticated")
	}

	apiURL := OAuthAPIBase + "/app/v2/home/device_list_page"
	body := `{"limit":200,"get_split_device":true,"get_third_device":true}`

	req, err := http.NewRequest("POST", apiURL, strings.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer"+token)
	req.Header.Set("X-Client-BizId", "haapi")
	req.Header.Set("X-Client-AppId", c.clientID)
	req.Header.Set("Host", "ha.api.io.mi.com")

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("device list API: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	var result struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
		Result  struct {
			List []struct {
				DID     string `json:"did"`
				Name    string `json:"name"`
				Model   string `json:"model"`
				LocalIP string `json:"localip"`
				Token   string `json:"token"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("parse device list: %w (body=%s)", err, string(respBody[:min(len(respBody), 200)]))
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("device list API error (code=%d): %s", result.Code, result.Message)
	}

	var devices []DeviceInfo
	for _, d := range result.Result.List {
		if d.DID != "" {
			devices = append(devices, DeviceInfo{
				DID:   d.DID,
				Name:  d.Name,
				Model: d.Model,
				IP:    d.LocalIP,
				Token: d.Token,
			})
		}
	}
	log.Printf("[oauth] Device list: %d devices", len(devices))
	return devices, nil
}

// ControlDevice sends a control command via OAuth Bearer token.
func (c *OAuthClient) ControlDevice(did string, siid, piid int, value interface{}) error {
	c.mu.Lock()
	token := c.accessToken
	c.mu.Unlock()

	if token == "" {
		return fmt.Errorf("not authenticated")
	}

	apiURL := OAuthAPIBase + "/app/v2/miotspec/prop/set"
	reqBody := map[string]interface{}{
		"params": []map[string]interface{}{
			{"did": did, "siid": siid, "piid": piid, "value": value},
		},
	}
	bodyBytes, _ := json.Marshal(reqBody)

	req, err := http.NewRequest("POST", apiURL, strings.NewReader(string(bodyBytes)))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer"+token)
	req.Header.Set("X-Client-BizId", "haapi")
	req.Header.Set("X-Client-AppId", c.clientID)
	req.Header.Set("Host", "ha.api.io.mi.com")

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("control API: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	var result struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return fmt.Errorf("parse control response: %w", err)
	}
	if result.Code != 0 && result.Code != 1 {
		return fmt.Errorf("control failed (code=%d): %s", result.Code, result.Message)
	}
	return nil
}

// Status returns the current OAuth status.
func (c *OAuthClient) Status() (status, message string, authenticated bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.status, c.statusMsg, c.accessToken != ""
}

// AccessToken returns the current access token.
func (c *OAuthClient) AccessToken() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.accessToken
}

// IsAuthenticated returns true if we have a valid access token.
func (c *OAuthClient) IsAuthenticated() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.accessToken != "" && time.Now().Before(c.expiresAt)
}

// Cancel stops the OAuth flow and cleans up.
func (c *OAuthClient) Cancel() {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.stopCallbackLocked()
	c.status = "idle"
	c.statusMsg = ""
	c.authCode = ""

	select {
	case <-c.done:
	default:
		close(c.done)
	}
}

// HasCallbackServer returns true if the callback server is running.
func (c *OAuthClient) HasCallbackServer() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.callbackServer != nil
}

// CheckCallbackPort checks if port 8123 is available for the callback server.
func CheckCallbackPort() error {
	ln, err := net.Listen("tcp", fmt.Sprintf(":%d", OAuthCallbackPort))
	if err != nil {
		return fmt.Errorf("port %d is not available: %w", OAuthCallbackPort, err)
	}
	ln.Close()
	return nil
}

// randomString generates a random string of the given length.
func randomString(n int) string {
	const letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	b := make([]byte, n)
	for i := range b {
		b[i] = letters[time.Now().UnixNano()%int64(len(letters))]
		time.Sleep(time.Nanosecond)
	}
	return string(b)
}