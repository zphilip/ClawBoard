package xiaomi

import (
	"bytes"
	"crypto/rc4"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// miotEncryptedCall makes an encrypted API call to Xiaomi's MIoT API.
// This implements the ENCRYPT-RC4 protocol used by the Mi Home app.
func (c *CloudClient) miotEncryptedCall(apiPath string, formData url.Values, ssecurity string) ([]byte, error) {
	baseURL := "https://api.io.mi.com/app"
	fullURL := baseURL + apiPath

	// Add auth params to form data (will be encrypted).
	params := make(url.Values)
	for k, v := range formData {
		for _, vv := range v {
			params.Set(k, vv)
		}
	}

	// Generate encrypted parameters (returns signedNonce for decryption).
	encParams, signedNonce, err := generateEncParams(fullURL, "POST", params, ssecurity)
	if err != nil {
		return nil, fmt.Errorf("generate enc params: %w", err)
	}

	// Build request with encrypted params as query string.
	req, err := http.NewRequest("POST", fullURL+"?"+encParams.Encode(), nil)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set("User-Agent", "MIoT/Android APP/com.xiaomi.mihome APPV/10.5.201")
	req.Header.Set("x-xiaomi-protocal-flag-cli", "PROTOCAL-HTTP2")
	req.Header.Set("MIOT-ENCRYPT-ALGORITHM", "ENCRYPT-RC4")

	// Set auth cookies.
	req.AddCookie(&http.Cookie{Name: "userId", Value: c.userID})
	req.AddCookie(&http.Cookie{Name: "serviceToken", Value: c.serviceToken})
	req.AddCookie(&http.Cookie{Name: "yetAnotherServiceToken", Value: c.serviceToken})
	req.AddCookie(&http.Cookie{Name: "locale", Value: "en_GB"})
	req.AddCookie(&http.Cookie{Name: "timezone", Value: "GMT+02:00"})
	req.AddCookie(&http.Cookie{Name: "is_daylight", Value: "1"})
	req.AddCookie(&http.Cookie{Name: "dst_offset", Value: "3600000"})
	req.AddCookie(&http.Cookie{Name: "channel", Value: "MI_APP_STORE"})

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("API call: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body: %w", err)
	}

	fmt.Printf("[xiaomi] API %s → HTTP %d, content-type=%s\n", apiPath, resp.StatusCode, resp.Header.Get("Content-Type"))

	// Log raw body as hex for debugging.
	if len(body) > 0 {
		hexPrefix := fmt.Sprintf("%x", body[:min(80, len(body))])
		fmt.Printf("[xiaomi] raw body hex (first 80): %s\n", hexPrefix)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body[:min(200, len(body))]))
	}

	// Try to decrypt response with the same signedNonce used for encryption.
	// Debug: log nonce and signedNonce.
	nonceFromParams := encParams.Get("_nonce")
	recomputedKey := signNonce(nonceFromParams, ssecurity)
	fmt.Printf("[xiaomi] nonce=%s key=%s recomputed_key=%s match=%v\n",
		nonceFromParams[:min(20, len(nonceFromParams))],
		signedNonce[:min(20, len(signedNonce))],
		recomputedKey[:min(20, len(recomputedKey))],
		signedNonce == recomputedKey)

	// Strategy 1: body is base64-encoded RC4 output (Python approach).
	bodyStr := string(body)
	plain, err := decryptRC4(signedNonce, bodyStr)
	if err == nil && strings.Contains(plain, "{\"") {
		fmt.Printf("[xiaomi] decrypt OK (base64+RC4)\n")
		return []byte(plain), nil
	}
	// Also try with recomputed key.
	if signedNonce != recomputedKey {
		plain2, err2 := decryptRC4(recomputedKey, bodyStr)
		if err2 == nil && strings.Contains(plain2, "{\"") {
			fmt.Printf("[xiaomi] decrypt OK (base64+RC4 with recomputed key)\n")
			return []byte(plain2), nil
		}
	}
	if err == nil && len(plain) > 0 {
		fmt.Printf("[xiaomi] base64+RC4 result hex (first 40): %x\n", []byte(plain)[:min(40, len(plain))])
	}

	// Strategy 2: body is raw RC4-encrypted binary (no base64 wrapper).
	key, _ := base64.StdEncoding.DecodeString(signedNonce)
	if c, cerr := rc4.NewCipher(key); cerr == nil {
		raw := make([]byte, len(body))
		c.XORKeyStream(raw, body)
		// Check for JSON at byte level (not string, which corrupts non-UTF8).
		if bytes.Contains(raw, []byte("{")) {
			fmt.Printf("[xiaomi] decrypt OK (raw RC4), len=%d body=%s\n", len(raw), raw[:min(200, len(raw))])
			return raw, nil
		}
		// Log hex of decrypted output for debugging.
		if len(raw) > 0 {
			fmt.Printf("[xiaomi] raw RC4 result hex (first 40): %x\n", raw[:min(40, len(raw))])
		}
	}

	// Strategy 3: body is base64 → raw → RC4.
	if decoded, derr := base64.StdEncoding.DecodeString(bodyStr); derr == nil {
		key, _ := base64.StdEncoding.DecodeString(signedNonce)
		if c, cerr := rc4.NewCipher(key); cerr == nil {
			raw := make([]byte, len(decoded))
			c.XORKeyStream(raw, decoded)
			if bytes.Contains(raw, []byte("{")) {
				fmt.Printf("[xiaomi] decrypt OK (base64→raw→RC4), body=%s\n", raw[:min(200, len(raw))])
				return raw, nil
			}
			if len(raw) > 0 {
				fmt.Printf("[xiaomi] base64→RC4 result hex (first 40): %x\n", raw[:min(40, len(raw))])
			}
		}
	}

	// All strategies failed.
	fmt.Printf("[xiaomi] all decrypt strategies failed, returning raw body\n")
	return body, nil
}

// generateEncParams creates the encrypted+signed parameter map.
// Matches Python's generate_enc_params exactly:
// 1. Compute rc4_hash__ on unencrypted params (WITHOUT _nonce).
// 2. RC4-encrypt data and rc4_hash__ (NOT _nonce).
// 3. Compute signature on encrypted params (WITHOUT _nonce).
// 4. Add signature and _nonce (unencrypted).
func generateEncParams(fullURL, method string, params url.Values, ssecurity string) (url.Values, string, error) {
	millis := time.Now().UnixMilli()
	nonce := generateNonce(millis)
	signedNonce := signNonce(nonce, ssecurity)

	// Start with the original params (data only, no _nonce).
	out := make(url.Values)
	for k, v := range params {
		for _, vv := range v {
			out.Set(k, vv)
		}
	}

	// Step 1: rc4_hash__ on unencrypted params (data only).
	out.Set("rc4_hash__", generateEncSignature(fullURL, method, signedNonce, out))

	// Step 2: RC4-encrypt param values (data and rc4_hash__ only, NOT _nonce).
	for _, k := range []string{"data", "rc4_hash__"} {
		if v := out.Get(k); v != "" {
			encrypted, err := encryptRC4(signedNonce, v)
			if err != nil {
				return nil, "", fmt.Errorf("encrypt %s: %w", k, err)
			}
			out.Set(k, encrypted)
		}
	}

	// Step 3: signature on the encrypted params (data + rc4_hash__, no _nonce).
	out.Set("signature", generateEncSignature(fullURL, method, signedNonce, out))

	// Step 4: Add _nonce (unencrypted original).
	out.Set("_nonce", nonce)

	return out, signedNonce, nil
}

// generateNonce creates a Xiaomi-format nonce: 8 random bytes + 4 bytes of
// (millis / 60000) as big-endian, then base64-encoded.
func generateNonce(millis int64) string {
	b := make([]byte, 12)
	rand.Read(b[:8])
	minutes := uint32(millis / 60000)
	b[8] = byte(minutes >> 24)
	b[9] = byte(minutes >> 16)
	b[10] = byte(minutes >> 8)
	b[11] = byte(minutes)
	return base64.StdEncoding.EncodeToString(b)
}

// signNonce creates the signed nonce: SHA256(base64decode(ssecurity) + base64decode(nonce)).
func signNonce(nonce, ssecurity string) string {
	ssBytes, _ := base64.StdEncoding.DecodeString(ssecurity)
	nBytes, _ := base64.StdEncoding.DecodeString(nonce)
	h := sha256.New()
	h.Write(ssBytes)
	h.Write(nBytes)
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

// generateEncSignature creates the rc4_hash__ or signature parameter.
func generateEncSignature(fullURL, method string, signedNonce string, params url.Values) string {
	// Extract path from URL: https://api.io.mi.com/app/v2/xxx → /v2/xxx
	path := fullURL
	if idx := strings.Index(path, "/app/"); idx >= 0 {
		path = path[idx+4:] // skip to after /app → /v2/...
	} else if idx := strings.Index(path, ".com"); idx >= 0 {
		path = path[idx+4:]
	}

	// Build signature string: METHOD&path&k1=v1&k2=v2&...&signedNonce
	// Keys must be in sorted order for consistency.
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sortStrings(keys)

	sigStr := strings.ToUpper(method) + "&" + path + "&"
	for _, k := range keys {
		sigStr += k + "=" + params.Get(k) + "&"
	}
	sigStr += signedNonce

	h := sha1.New()
	h.Write([]byte(sigStr))
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

func sortStrings(s []string) {
	for i := 0; i < len(s); i++ {
		for j := i + 1; j < len(s); j++ {
			if s[i] > s[j] {
				s[i], s[j] = s[j], s[i]
			}
		}
	}
}

// encryptRC4 encrypts a payload with RC4 using the given password (base64-encoded key).
func encryptRC4(password, payload string) (string, error) {
	key, err := base64.StdEncoding.DecodeString(password)
	if err != nil {
		return "", err
	}
	c, err := rc4.NewCipher(key)
	if err != nil {
		return "", err
	}
	plain := []byte(payload)
	encrypted := make([]byte, len(plain))
	c.XORKeyStream(encrypted, plain)
	return base64.StdEncoding.EncodeToString(encrypted), nil
}

// decryptRC4 decrypts an RC4-encrypted payload.
func decryptRC4(password, payload string) (string, error) {
	key, err := base64.StdEncoding.DecodeString(password)
	if err != nil {
		return "", err
	}
	encrypted, err := base64.StdEncoding.DecodeString(payload)
	if err != nil {
		return "", err
	}
	c, err := rc4.NewCipher(key)
	if err != nil {
		return "", err
	}
	plain := make([]byte, len(encrypted))
	c.XORKeyStream(plain, encrypted)
	return string(plain), nil
}

// DeviceListEncrypted fetches devices using the encrypted MIoT API.
// Flow: get homes → for each home get devices.
func (c *CloudClient) DeviceListEncrypted(ssecurity string) ([]DeviceInfo, error) {
	// Step 1: Get homes.
	type homeInfo struct {
		ID      string `json:"id"`
		Name    string `json:"name"`
		OwnerID string `json:"uid"`
	}

	homesParams := url.Values{}
	homesParams.Set("data", `{"fg":true,"fetch_share":true,"fetch_share_dev":true,"limit":300,"app_ver":7}`)

	homesBody, err := c.miotEncryptedCall("/v2/homeroom/gethome", homesParams, ssecurity)
	if err != nil {
		return nil, fmt.Errorf("encrypted gethome: %w", err)
	}

	bodyStr := trimPrefix(string(homesBody), "&&&START&&&")
	var homesResult struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
		Result  struct {
			HomeList []homeInfo `json:"homelist"`
		} `json:"result"`
	}
	if err := json.Unmarshal([]byte(bodyStr), &homesResult); err != nil {
		return nil, fmt.Errorf("parse homes: %w (body=%s)", err, bodyStr[:min(200, len(bodyStr))])
	}
	if homesResult.Code != 0 {
		return nil, fmt.Errorf("homes API code=%d: %s", homesResult.Code, homesResult.Message)
	}

	fmt.Printf("[xiaomi] encrypted homes: %d found\n", len(homesResult.Result.HomeList))

	// Step 2: Get devices for each home.
	var allDevices []DeviceInfo
	for _, h := range homesResult.Result.HomeList {
		devParams := url.Values{}
		devParams.Set("data", fmt.Sprintf(
			`{"home_owner":%s,"home_id":%s,"limit":200,"get_split_device":true,"support_smart_home":true}`,
			h.OwnerID, h.ID))

		devBody, err := c.miotEncryptedCall("/v2/home/home_device_list", devParams, ssecurity)
		if err != nil {
			fmt.Printf("[xiaomi] home %s devices error: %v\n", h.Name, err)
			continue
		}

		devStr := trimPrefix(string(devBody), "&&&START&&&")
		var devResult struct {
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
		if err := json.Unmarshal([]byte(devStr), &devResult); err != nil {
			fmt.Printf("[xiaomi] parse home %s devices: %v\n", h.Name, err)
			continue
		}
		if devResult.Code != 0 {
			fmt.Printf("[xiaomi] home %s devices code=%d: %s\n", h.Name, devResult.Code, devResult.Message)
			continue
		}

		for _, d := range devResult.Result.List {
			if d.DID != "" {
				allDevices = append(allDevices, DeviceInfo{
					DID:   d.DID,
					Name:  d.Name,
					Model: d.Model,
					IP:    d.LocalIP,
					Token: d.Token,
				})
			}
		}
	}

	return allDevices, nil
}

// Used for testing encryption.
var _ = json.Valid