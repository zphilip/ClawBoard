package miio

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/md5"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/rand"
	"net"
	"sync"
	"time"
)

const (
	// DefaultPort is the standard miio UDP port.
	DefaultPort = 54321
	// ReadTimeout is the maximum time to wait for a device response.
	ReadTimeout = 10 * time.Second
	// Magic is the miio protocol magic number.
	Magic = 0x2131
	// HeaderLen is the length of the miio packet header.
	HeaderLen = 32
)

// Device represents a Xiaomi miio device on the local network.
type Device struct {
	IP    string
	Token string
	Model  string
}

// NewDevice creates a new miio device handle.
func NewDevice(ip, token string, model string) *Device {
	return &Device{IP: ip, Token: token, Model: model}
}

// Packet represents a parsed miio packet.
type Packet struct {
	DeviceID   uint32
	Stamp      uint32
	Payload    []byte
	RawPayload []byte // encrypted bytes
}

// encodePacket builds a raw miio packet (Variant 2: key=MD5(token)).
func (d *Device) encodePacket(cmd interface{}, deviceID uint32) ([]byte, error) {
	return d.encodePacketVariant(cmd, deviceID, false)
}

// encodePacketVariant builds a raw miio packet with the specified encryption variant.
// alt=false → V2: key=MD5(token), iv=MD5(key+token) (newer devices)
// alt=true  → V1: key=token, iv=MD5(token) (older devices)
func (d *Device) encodePacketVariant(cmd interface{}, deviceID uint32, alt bool) ([]byte, error) {
	payload, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("marshal command: %w", err)
	}

	tokenBytes, err := hex.DecodeString(d.Token)
	if err != nil {
		return nil, fmt.Errorf("decode token: %w", err)
	}

	var key, iv []byte
	if alt {
		// V1: key=token directly, iv=MD5(token).
		key = tokenBytes
		iv = md5Hash(tokenBytes)
	} else {
		// V2: key=MD5(token), iv=MD5(key+token).
		key = md5Hash(tokenBytes)
		iv = md5Hash(append(key, tokenBytes...))
	}

	encrypted, err := aesEncrypt(key, iv, payload)
	if err != nil {
		return nil, fmt.Errorf("encrypt: %w", err)
	}

	stamp := uint32(time.Now().Unix())

	// Build header [0:16] for checksum calculation.
	headerPrefix := make([]byte, 16)
	binary.BigEndian.PutUint16(headerPrefix[0:2], Magic)
	binary.BigEndian.PutUint16(headerPrefix[2:4], uint16(HeaderLen+len(encrypted)))
	binary.BigEndian.PutUint32(headerPrefix[8:12], deviceID)
	binary.BigEndian.PutUint32(headerPrefix[12:16], stamp)

	// Checksum = MD5(headerPrefix + tokenBytes + encrypted)
	checksumData := make([]byte, 0, 16+len(tokenBytes)+len(encrypted))
	checksumData = append(checksumData, headerPrefix...)
	checksumData = append(checksumData, tokenBytes...)
	checksumData = append(checksumData, encrypted...)
	checksum := md5Hash(checksumData)

	packet := make([]byte, 0, HeaderLen+len(encrypted))
	packet = append(packet, headerPrefix...)
	packet = append(packet, checksum...)
	packet = append(packet, encrypted...)

	return packet, nil
}

// decodePacket parses a raw miio response packet (V2 by default).
func (d *Device) decodePacket(data []byte) (*Packet, error) {
	return d.decodePacketVariant(data, false)
}

// decodePacketVariant parses a miio response packet with a specific encryption variant.
func (d *Device) decodePacketVariant(data []byte, alt bool) (*Packet, error) {
	if len(data) < HeaderLen {
		return nil, fmt.Errorf("packet too short: %d bytes", len(data))
	}

	magic := binary.BigEndian.Uint16(data[0:2])
	if magic != Magic {
		return nil, fmt.Errorf("bad magic: 0x%04x", magic)
	}

	deviceID := binary.BigEndian.Uint32(data[8:12])
	stamp := binary.BigEndian.Uint32(data[12:16])
	encrypted := data[HeaderLen:]

	tokenBytes, err := hex.DecodeString(d.Token)
	if err != nil {
		return nil, fmt.Errorf("decode token: %w", err)
	}

	var key, iv []byte
	if alt {
		key = tokenBytes
		iv = md5Hash(tokenBytes)
	} else {
		key = md5Hash(tokenBytes)
		iv = md5Hash(append(key, tokenBytes...))
	}

	plain, err := aesDecrypt(key, iv, encrypted)
	if err != nil {
		return nil, fmt.Errorf("decrypt: %w", err)
	}

	return &Packet{
		DeviceID:   deviceID,
		Stamp:      stamp,
		Payload:    plain,
		RawPayload: encrypted,
	}, nil
}

// decodePacketAlt tries the alternative encryption scheme (V1).
func (d *Device) decodePacketAlt(data []byte) (*Packet, error) {
	return d.decodePacketVariant(data, true)
}

// Send sends a command to the device and returns the decrypted JSON response.
// Tries both encryption variants (V1: key=token, V2: key=MD5(token)).
func (d *Device) Send(cmd interface{}) (json.RawMessage, error) {
	deviceID := uint32(0)

	// Try Variant 2 first (key=MD5(token), iv=MD5(key+token)).
	resp, err := d.sendWithVariant(cmd, deviceID, false)
	if err == nil {
		return resp, nil
	}
	// If timeout/error, try Variant 1 (key=token, iv=MD5(token)).
	resp2, err2 := d.sendWithVariant(cmd, deviceID, true)
	if err2 == nil {
		return resp2, nil
	}
	return nil, fmt.Errorf("send failed: v2=%v, v1=%v", err, err2)
}

// sendWithVariant sends a command with a specific encryption variant.
// alt=true → older device variant (key=token directly, no MD5 wrapper).
func (d *Device) sendWithVariant(cmd interface{}, deviceID uint32, alt bool) (json.RawMessage, error) {
	packet, err := d.encodePacketVariant(cmd, deviceID, alt)
	if err != nil {
		return nil, fmt.Errorf("encode: %w", err)
	}

	addr := &net.UDPAddr{IP: net.ParseIP(d.IP), Port: DefaultPort}
	conn, err := net.DialUDP("udp", nil, addr)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", addr.String(), err)
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(ReadTimeout)); err != nil {
		return nil, fmt.Errorf("set deadline: %w", err)
	}

	if _, err := conn.Write(packet); err != nil {
		return nil, fmt.Errorf("write: %w", err)
	}

	buf := make([]byte, 4096)
	n, err := conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("read: %w", err)
	}

	// Decode with matching variant first.
	resp, err := d.decodePacketVariant(buf[:n], alt)
	if err != nil {
		// Try the other variant for decoding.
		resp, err2 := d.decodePacketVariant(buf[:n], !alt)
		if err2 != nil {
			return nil, fmt.Errorf("decode: v=%v (alt decode: %v)", err, err2)
		}
		return json.RawMessage(resp.Payload), nil
	}

	return json.RawMessage(resp.Payload), nil
}

// Hello sends a discovery "hello" packet and returns the parsed device info.
func (d *Device) Hello() (*HelloResponse, error) {
	// Hello packet uses a special device ID.
	cmd := map[string]interface{}{
		"id":      rand.Intn(9999) + 1,
		"method":  "miIO.info",
		"params":  []interface{}{},
	}

	deviceID := uint32(0xFFFFFFFF)
	packet, err := d.encodePacket(cmd, deviceID)
	if err != nil {
		return nil, fmt.Errorf("encode hello: %w", err)
	}

	addr := &net.UDPAddr{IP: net.ParseIP(d.IP), Port: DefaultPort}

	conn, err := net.DialUDP("udp", nil, addr)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", addr.String(), err)
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(ReadTimeout)); err != nil {
		return nil, err
	}

	if _, err := conn.Write(packet); err != nil {
		return nil, err
	}

	buf := make([]byte, 4096)
	n, err := conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("read hello: %w", err)
	}

	resp, err := d.decodePacket(buf[:n])
	if err != nil {
		return nil, fmt.Errorf("decode hello: %w", err)
	}

	var hello HelloResponse
	if err := json.Unmarshal(resp.Payload, &hello); err != nil {
		return nil, fmt.Errorf("unmarshal hello: %w", err)
	}

	return &hello, nil
}

// HelloResponse is the device info returned by miIO.info.
type HelloResponse struct {
	ID      int    `json:"id"`
	Result  HelloResult `json:"result"`
}

// HelloResult contains device identification info.
type HelloResult struct {
	Life    int    `json:"life"`
	Model   string `json:"model"`
	Token   string `json:"token"`
	FwVer   string `json:"fw_ver"`
	HwVer   string `json:"hw_ver"`
	Mac     string `json:"mac"`
	WiFiFw  string `json:"wifi_fw_ver"`
	AP      struct {
		SSID string `json:"ssid"`
		BSSID string `json:"bssid"`
		RSSI int    `json:"rssi"`
	} `json:"ap"`
}

// SetPropertyCommand builds a set_properties command for a specific siid/piid pair.
// This is the standard MIoT property-setting method.
func SetPropertyCommand(siid, piid int, value interface{}) map[string]interface{} {
	return map[string]interface{}{
		"id":     rand.Intn(9999) + 1,
		"method": "set_properties",
		"params": []interface{}{
			map[string]interface{}{
				"did":   fmt.Sprintf("property-%d-%d", siid, piid),
				"siid":  siid,
				"piid":  piid,
				"value": value,
			},
		},
	}
}

// GetPropertyCommand builds a get_properties command.
func GetPropertyCommand(siid, piid int) map[string]interface{} {
	return map[string]interface{}{
		"id":     rand.Intn(9999) + 1,
		"method": "get_properties",
		"params": []interface{}{
			map[string]interface{}{
				"did":  fmt.Sprintf("property-%d-%d", siid, piid),
				"siid": siid,
				"piid": piid,
			},
		},
	}
}

// Device commands for common device types.
// These are the standard MIoT service/property IDs.

// SwitchCommands returns on/off commands for devices with a switch service (siid=2).
func SwitchCommands(turnOn bool) map[string]interface{} {
	value := false
	if turnOn {
		value = true
	}
	return SetPropertyCommand(2, 1, value)
}

// BrightnessCommand returns a brightness set command (siid=2, piid=2).
func BrightnessCommand(level int) map[string]interface{} {
	return SetPropertyCommand(2, 2, level)
}

// ColorTempCommand returns a color temperature command (siid=2, piid=3).
func ColorTempCommand(temp int) map[string]interface{} {
	return SetPropertyCommand(2, 3, temp)
}

// ── Crypto helpers ────────────────────────────────────────────────────────────

var (
	encryptPool = sync.Pool{
		New: func() interface{} { return make([]byte, 0, 4096) },
	}
)

func md5Hash(data []byte) []byte {
	h := md5.Sum(data)
	return h[:]
}

func aesEncrypt(key, iv, plain []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}

	// PKCS7 padding.
	padLen := aes.BlockSize - (len(plain) % aes.BlockSize)
	padded := make([]byte, len(plain)+padLen)
	copy(padded, plain)
	for i := len(plain); i < len(padded); i++ {
		padded[i] = byte(padLen)
	}

	encrypted := make([]byte, len(padded))
	mode := cipher.NewCBCEncrypter(block, iv)
	mode.CryptBlocks(encrypted, padded)

	return encrypted, nil
}

func aesDecrypt(key, iv, encrypted []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}

	if len(encrypted)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("ciphertext not multiple of block size: %d", len(encrypted))
	}

	plain := make([]byte, len(encrypted))
	mode := cipher.NewCBCDecrypter(block, iv)
	mode.CryptBlocks(plain, encrypted)

	// PKCS7 unpad.
	if len(plain) == 0 {
		return nil, fmt.Errorf("empty plaintext")
	}
	padLen := int(plain[len(plain)-1])
	if padLen > aes.BlockSize || padLen == 0 {
		return nil, fmt.Errorf("invalid padding: %d", padLen)
	}
	for i := len(plain) - padLen; i < len(plain); i++ {
		if plain[i] != byte(padLen) {
			return nil, fmt.Errorf("invalid padding byte at %d", i)
		}
	}

	return plain[:len(plain)-padLen], nil
}

// Ensure package-level functions are available.
var _ = md5Hash
var _ = bytes.NewReader