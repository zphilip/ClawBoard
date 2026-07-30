package xiaomi

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"time"
)

// CloudClient handles Xiaomi cloud account operations.
type CloudClient struct {
	username string
	password string
	region   string
	client   *http.Client
	debug    bool

	// Cached credentials.
	serviceToken string
	userID       string
	deviceID     string
	ssecurity    string // for encrypted API calls
}

// NewCloudClient creates a new Xiaomi cloud client.
func NewCloudClient(username, password, region string) *CloudClient {
	jar, _ := cookiejar.New(nil)
	return &CloudClient{
		username: username,
		password: password,
		region:   region,
		client: &http.Client{
			Jar:     jar,
			Timeout: 30 * time.Second,
		},
	}
}

// SetDebug enables verbose logging of HTTP requests.
func (c *CloudClient) SetDebug(v bool) { c.debug = v }

// Login authenticates to Xiaomi cloud and obtains a service token for miio.
func (c *CloudClient) Login() error {
	// Step 1: Get the service login page to obtain cookies and sign parameters.
	loginURL := "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&json=true"
	if c.region == "cn" {
		loginURL = "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&json=true"
	} else {
		loginURL = fmt.Sprintf("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&json=true&_locale=%s", c.region)
	}

	resp, err := c.client.Get(loginURL)
	if err != nil {
		return fmt.Errorf("serviceLogin GET: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	var loginPage struct {
		QS        string `json:"qs"`
		Sign      string `json:"_sign"`
		Callback  string `json:"callback"`
		Location  string `json:"location"`
	}
	if err := json.Unmarshal(body, &loginPage); err != nil {
		return fmt.Errorf("parse login page: %w (body=%s)", err, string(body[:min(len(body), 200)]))
	}

	if loginPage.QS == "" {
		loginPage.QS = "%3Fsid%3Dxiaomiio%26_json%3Dtrue"
	}
	if loginPage.Sign == "" {
		loginPage.Sign = ""
	}

	// Step 2: Compute password hash.
	passHash := strings.ToUpper(md5Hex(c.password))

	// Step 3: POST credentials to the auth endpoint.
	authURL := "https://account.xiaomi.com/pass/serviceLoginAuth2"
	formData := url.Values{}
	formData.Set("sid", "xiaomiio")
	formData.Set("hash", passHash)
	formData.Set("callback", loginPage.Callback)
	formData.Set("qs", loginPage.QS)
	formData.Set("user", c.username)
	formData.Set("_sign", loginPage.Sign)
	formData.Set("_json", "true")

	authReq, err := http.NewRequest("POST", authURL, strings.NewReader(formData.Encode()))
	if err != nil {
		return fmt.Errorf("create auth request: %w", err)
	}
	authReq.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	authReq.Header.Set("User-Agent", "XiaomiMiio/1.0")
	authReq.Header.Set("Referer", loginURL)

	authResp, err := c.client.Do(authReq)
	if err != nil {
		return fmt.Errorf("auth POST: %w", err)
	}
	defer authResp.Body.Close()

	authBody, _ := io.ReadAll(authResp.Body)

	// Handle various auth response formats.
	// Xiaomi wraps in "&&&START&&&" prefix sometimes.
	authBodyStr := string(authBody)
	authBodyStr = strings.TrimPrefix(authBodyStr, "&&&START&&&")

	var authResult struct {
		Code        int    `json:"code"`
		Description string `json:"description"`
		Location    string `json:"location"`
		UserID      string `json:"userId"`
		Token       string `json:"token"`
		Ssecurity   string `json:"ssecurity"`
		Nonce       int64  `json:"nonce"`
		NotificationURL string `json:"notificationUrl"`
		CaptchaURL  string `json:"captchaUrl"`
	}

	if err := json.Unmarshal([]byte(authBodyStr), &authResult); err != nil {
		return fmt.Errorf("parse auth response: %w (body=%s)", err, authBodyStr[:min(len(authBodyStr), 300)])
	}

	if authResult.Code != 0 && authResult.Code != 70016 {
		msg := authResult.Description
		if authResult.CaptchaURL != "" {
			msg = fmt.Sprintf("2FA/captcha required. Visit: %s", authResult.CaptchaURL)
		}
		if authResult.NotificationURL != "" {
			msg = fmt.Sprintf("2FA verification required. Check your Xiaomi app for approval, or visit: %s", authResult.NotificationURL)
		}
		return fmt.Errorf("auth failed (code %d): %s", authResult.Code, msg)
	}

	// Step 4: Follow the redirect location to get service token.
	redirectURL := authResult.Location
	if redirectURL == "" {
		// Construct redirect URL from userId and ssecurity.
		redirectURL = fmt.Sprintf("https://account.xiaomi.com/pass/serviceLoginAuth2?sid=xiaomiio&userId=%s&ssecurity=%s&_json=true",
			authResult.UserID, url.QueryEscape(authResult.Ssecurity))
	}

	redirectResp, err := c.client.Get(redirectURL)
	if err != nil {
		return fmt.Errorf("redirect GET: %w", err)
	}
	defer redirectResp.Body.Close()

	redirectBody, _ := io.ReadAll(redirectResp.Body)
	redirectBodyStr := strings.TrimPrefix(string(redirectBody), "&&&START&&&")

	var redirectResult struct {
		Code        int    `json:"code"`
		ServiceToken string `json:"serviceToken"`
		UserID       string `json:"userId"`
		Location string `json:"location"`
		Description string `json:"description"`
	}

	if err := json.Unmarshal([]byte(redirectBodyStr), &redirectResult); err != nil {
		return fmt.Errorf("parse redirect response: %w", err)
	}

	if redirectResult.ServiceToken == "" {
		// Try to extract from cookies.
		for _, ck := range c.client.Jar.Cookies(resp.Request.URL) {
			if ck.Name == "serviceToken" {
				redirectResult.ServiceToken = ck.Value
			}
			if ck.Name == "userId" {
				redirectResult.UserID = ck.Value
			}
		}
	}

	if redirectResult.ServiceToken == "" {
		return fmt.Errorf("no serviceToken obtained (code=%d, desc=%s)", redirectResult.Code, redirectResult.Description)
	}

	c.serviceToken = redirectResult.ServiceToken
	c.userID = redirectResult.UserID

	if c.debug {
		fmt.Printf("[xiaomi] Login success: userId=%s, serviceToken=%s...\n",
			c.userID, c.serviceToken[:min(8, len(c.serviceToken))])
	}

	return nil
}

// DeviceList fetches the full device list from Xiaomi cloud.
// Uses the /app/device/all_list endpoint (same as the Mi Home app).
func (c *CloudClient) DeviceList() ([]DeviceInfo, error) {
	if c.serviceToken == "" {
		return nil, fmt.Errorf("not logged in: call Login() first")
	}

	// Try encrypted API if ssecurity is available.
	fmt.Printf("[xiaomi] ssecurity available: %v (len=%d)\n", c.ssecurity != "", len(c.ssecurity))
	if c.ssecurity != "" {
		// Try native Go encrypted API first.
		devs, err := c.DeviceListEncrypted(c.ssecurity)
		fmt.Printf("[xiaomi] native encrypted result: %d devices, err=%v\n", len(devs), err)
		if err == nil && len(devs) > 0 {
			return devs, nil
		}
		if err != nil {
			fmt.Printf("[xiaomi] native encrypted API failed: %v, trying python fallback\n", err)
		}

		// Fallback to Python helper.
		devs, err = c.deviceListViaPython(c.ssecurity)
		if err == nil && len(devs) > 0 {
			return devs, nil
		}
		if err != nil {
			fmt.Printf("[xiaomi] python helper failed: %v\n", err)
		}
	}

	baseURL := c.apiBaseURL()

	// Try homes API first (newer API), fall back to all_list.
	homes, err := c.getHomes(baseURL)
	if err == nil && len(homes) > 0 {
		var allDevices []DeviceInfo
		for _, h := range homes {
			devs, err := c.getHomeDevices(baseURL, h.ID, h.OwnerID)
			if err != nil {
				fmt.Printf("[xiaomi] home %s devices error: %v\n", h.Name, err)
				continue
			}
			allDevices = append(allDevices, devs...)
		}
		if len(allDevices) > 0 {
			return allDevices, nil
		}
	}

	// Fallback: all_list.
	devs, err := c.deviceListAll(baseURL)
	if err != nil {
		return nil, err
	}

	// If all_list returns empty, try with userId as home_owner in home_device_list.
	if len(devs) == 0 && c.userID != "" {
		fmt.Printf("[xiaomi] all_list returned 0 devices, trying home_device_list with userId=%s\n", c.userID)
		if homeDevs, err := c.getHomeDevices(baseURL, c.userID, c.userID); err == nil {
			devs = homeDevs
		}
	}

	return devs, nil
}

type homeInfo struct {
	ID      string
	Name    string
	OwnerID string
}

func (c *CloudClient) apiBaseURL() string {
	// The base api.io.mi.com works for all regions.
	// Country-specific subdomains (e.g. de.api.io.mi.com) are optional.
	return "https://api.io.mi.com"
}

func (c *CloudClient) getHomes(baseURL string) ([]homeInfo, error) {
	apiURL := baseURL + "/v2/homeroom/gethome"
	params := `{"fg":true,"fetch_share":true,"fetch_share_dev":true,"limit":300,"app_ver":7}`
	data := url.Values{}
	data.Set("data", params)

	body, err := c.miotAPICall(apiURL, data)
	if err != nil {
		return nil, err
	}

	var result struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
		Result  struct {
			HomeList []struct {
				ID      string `json:"id"`
				Name    string `json:"name"`
				OwnerID json.Number `json:"uid"`
			} `json:"homelist"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("parse homes: %w", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("homes API code=%d: %s", result.Code, result.Message)
	}

	var homes []homeInfo
	for _, h := range result.Result.HomeList {
		homes = append(homes, homeInfo{
			ID:      h.ID,
			Name:    h.Name,
			OwnerID: h.OwnerID.String(),
		})
	}
	return homes, nil
}

func (c *CloudClient) getHomeDevices(baseURL, homeID, ownerID string) ([]DeviceInfo, error) {
	apiURL := baseURL + "/v2/home/home_device_list"
	params := fmt.Sprintf(`{"home_owner":%s,"home_id":%s,"limit":200,"get_split_device":true,"support_smart_home":true}`, ownerID, homeID)
	data := url.Values{}
	data.Set("data", params)

	body, err := c.miotAPICall(apiURL, data)
	if err != nil {
		return nil, err
	}

	var result struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
		Result  struct {
			DeviceInfo []struct {
				DID     string `json:"did"`
				Name    string `json:"name"`
				Model   string `json:"model"`
				LocalIP string `json:"localip"`
				Token   string `json:"token"`
			} `json:"device_info"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("parse devices: %w", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("devices API code=%d: %s", result.Code, result.Message)
	}

	var devices []DeviceInfo
	for _, d := range result.Result.DeviceInfo {
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
	return devices, nil
}

// deviceListAll is the fallback using /app/device/all_list.
func (c *CloudClient) deviceListAll(baseURL string) ([]DeviceInfo, error) {
	apiURL := baseURL + "/app/device/all_list"
	params := `{"getVirtualModel":false,"getHuamiDevices":0,"get_splitTv":false,"support_smart_home":true}`
	data := url.Values{}
	data.Set("data", params)

	body, err := c.miotAPICall(apiURL, data)
	if err != nil {
		return nil, err
	}

	var result struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
		Result  struct {
			DeviceInfo []struct {
				DID     string `json:"did"`
				Name    string `json:"name"`
				Model   string `json:"model"`
				LocalIP string `json:"localip"`
				Token   string `json:"token"`
			} `json:"device_info"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("parse all_list: %w (body=%s)", err, string(body[:min(len(body), 200)]))
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("all_list API code=%d: %s", result.Code, result.Message)
	}

	var devices []DeviceInfo
	for _, d := range result.Result.DeviceInfo {
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
	return devices, nil
}

// miotAPICall makes an authenticated POST request to the Xiaomi MIoT API.
// It uses the same cookie/header format as the Mi Home app.
func (c *CloudClient) miotAPICall(apiURL string, formData url.Values) ([]byte, error) {
	req, err := http.NewRequest("POST", apiURL, strings.NewReader(formData.Encode()))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", "MIoT/Android APP/com.xiaomi.mihome APPV/10.5.201")
	req.Header.Set("x-xiaomi-protocal-flag-cli", "PROTOCAL-HTTP2")
	req.Header.Set("MIOT-ENCRYPT-ALGORITHM", "ENCRYPT-RC4")
	req.Header.Set("Accept-Encoding", "identity")

	// Set auth cookies — match Python's cookie format exactly.
	c.userID = strings.TrimSpace(c.userID)
	c.serviceToken = strings.TrimSpace(c.serviceToken)
	req.AddCookie(&http.Cookie{Name: "userId", Value: c.userID})
	req.AddCookie(&http.Cookie{Name: "serviceToken", Value: c.serviceToken})
	req.AddCookie(&http.Cookie{Name: "yetAnotherServiceToken", Value: c.serviceToken})
	req.AddCookie(&http.Cookie{Name: "locale", Value: "en_GB"})
	req.AddCookie(&http.Cookie{Name: "timezone", Value: "GMT+02:00"})
	req.AddCookie(&http.Cookie{Name: "is_daylight", Value: "1"})
	req.AddCookie(&http.Cookie{Name: "dst_offset", Value: "3600000"})

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("API call: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body: %w", err)
	}

	prefix := string(body)
	if len(prefix) > 200 {
		prefix = prefix[:200]
	}
	fmt.Printf("[xiaomi] API %s → HTTP %d, body: %s\n", apiURL[:min(60, len(apiURL))], resp.StatusCode, prefix)
	return body, nil
}

// DeviceInfo holds a single device's cloud metadata.
type DeviceInfo struct {
	DID   string `json:"did"`
	Name  string `json:"name"`
	Model string `json:"model"`
	IP    string `json:"ip"`
	Token string `json:"token"`
}

// IsLoggedIn returns true if the client has a valid service token.
func (c *CloudClient) IsLoggedIn() bool {
	return c.serviceToken != ""
}

// ── Helpers ────────────────────────────────────────────────────────────────────

func md5Hex(s string) string {
	h := md5.Sum([]byte(s))
	return hex.EncodeToString(h[:])
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}