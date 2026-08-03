package miio

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/md5"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"net"
	"time"
)

const (
	DefaultPort = 54321
	ReadTimeout = 5 * time.Second
	Magic       = 0x2131
	HeaderLen   = 32
)

type Device struct {
	IP    string
	Token string
	Model string
}

func NewDevice(ip, token, model string) *Device {
	return &Device{IP: ip, Token: token, Model: model}
}

// ── Encryption params (matches python-miio) ──────────────────────────────────

func miioKeyIV(tokenHex string) (key, iv []byte, tokenBytes []byte, err error) {
	tokenBytes, err = hex.DecodeString(tokenHex)
	if err != nil {
		return nil, nil, nil, err
	}
	key = md5Hash(tokenBytes)
	iv = md5Hash(append(key, tokenBytes...))
	return key, iv, tokenBytes, nil
}

func md5Hash(data []byte) []byte {
	h := md5.Sum(data)
	return h[:]
}

// ── Packet encode/decode ─────────────────────────────────────────────────────

func buildPacket(tokenHex string, payload []byte, deviceID uint32) ([]byte, error) {
	key, iv, tokenBytes, err := miioKeyIV(tokenHex)
	if err != nil {
		return nil, err
	}

	encrypted, err := aesEncrypt(key, iv, payload)
	if err != nil {
		return nil, err
	}

	stamp := uint32(time.Now().Unix())
	header := make([]byte, 16)
	binary.BigEndian.PutUint16(header[0:2], Magic)
	binary.BigEndian.PutUint16(header[2:4], uint16(HeaderLen+len(encrypted)))
	// header[4:8] = 0
	binary.BigEndian.PutUint32(header[8:12], deviceID)
	binary.BigEndian.PutUint32(header[12:16], stamp)

	checksum := md5Hash(append(append(header, tokenBytes...), encrypted...))

	pkt := make([]byte, 0, HeaderLen+len(encrypted))
	pkt = append(pkt, header...)
	pkt = append(pkt, checksum...)
	pkt = append(pkt, encrypted...)
	return pkt, nil
}

func parsePacket(tokenHex string, data []byte) ([]byte, error) {
	if len(data) < HeaderLen {
		return nil, fmt.Errorf("packet too short: %d", len(data))
	}
	magic := binary.BigEndian.Uint16(data[0:2])
	if magic != Magic {
		return nil, fmt.Errorf("bad magic: 0x%04x", magic)
	}
	key, iv, _, err := miioKeyIV(tokenHex)
	if err != nil {
		return nil, err
	}
	plain, err := aesDecrypt(key, iv, data[HeaderLen:])
	if err != nil {
		return nil, err
	}
	return plain, nil
}

// ── Send with handshake ──────────────────────────────────────────────────────

// Send sends a miIO command using handshake + command flow (matches python-miio).
// cmd can be map[string]interface{} with "method" and "params" keys.
func (d *Device) Send(cmd interface{}) (json.RawMessage, error) {
	cmdMap, ok := cmd.(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("command must be map[string]interface{}")
	}
	method, _ := cmdMap["method"].(string)
	params, _ := cmdMap["params"].([]interface{})
	if params == nil {
		params = []interface{}{}
	}

	// Step 1: Hello handshake — send miIO.info with deviceID=0xFFFFFFFF
	helloID := randomID()
	hello := map[string]interface{}{"id": helloID, "method": "miIO.info", "params": []interface{}{}}
	helloPayload, _ := json.Marshal(hello)
	helloPkt, err := buildPacket(d.Token, helloPayload, 0xFFFFFFFF)
	if err != nil {
		return nil, fmt.Errorf("build hello: %w", err)
	}

	addr := &net.UDPAddr{IP: net.ParseIP(d.IP), Port: DefaultPort}
	conn, err := net.DialUDP("udp", nil, addr)
	if err != nil {
		return nil, fmt.Errorf("dial: %w", err)
	}
	defer conn.Close()

	conn.SetDeadline(time.Now().Add(ReadTimeout))
	if _, err := conn.Write(helloPkt); err != nil {
		return nil, fmt.Errorf("write hello: %w", err)
	}
	buf := make([]byte, 4096)
	n, err := conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("read hello: %w", err)
	}

	helloResp, err := parsePacket(d.Token, buf[:n])
	if err != nil {
		return nil, fmt.Errorf("parse hello: %w", err)
	}
	var hr struct {
		ID     int `json:"id"`
		Result struct {
			DeviceID uint32 `json:"device_id"`
		} `json:"result"`
	}
	if err := json.Unmarshal(helloResp, &hr); err != nil {
		return nil, fmt.Errorf("unmarshal hello: %w", err)
	}
	deviceID := hr.Result.DeviceID
	if deviceID == 0 {
		deviceID = 1 // fallback
	}

	// Step 2: Send actual command with the real device ID.
	cmdID := randomID()
	command := map[string]interface{}{"id": cmdID, "method": method, "params": params}
	cmdPayload, _ := json.Marshal(command)
	cmdPkt, err := buildPacket(d.Token, cmdPayload, deviceID)
	if err != nil {
		return nil, fmt.Errorf("build command: %w", err)
	}

	conn.SetDeadline(time.Now().Add(ReadTimeout))
	if _, err := conn.Write(cmdPkt); err != nil {
		return nil, fmt.Errorf("write command: %w", err)
	}
	n, err = conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("read command: %w", err)
	}

	cmdResp, err := parsePacket(d.Token, buf[:n])
	if err != nil {
		return nil, fmt.Errorf("parse command: %w", err)
	}

	return json.RawMessage(cmdResp), nil
}

func randomID() int {
	n, _ := rand.Int(rand.Reader, big.NewInt(9000))
	return int(n.Int64()) + 1000
}

// ── AES helpers ──────────────────────────────────────────────────────────────

func aesEncrypt(key, iv, plain []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	padLen := aes.BlockSize - (len(plain) % aes.BlockSize)
	padded := make([]byte, len(plain)+padLen)
	copy(padded, plain)
	for i := len(plain); i < len(padded); i++ {
		padded[i] = byte(padLen)
	}
	enc := make([]byte, len(padded))
	cipher.NewCBCEncrypter(block, iv).CryptBlocks(enc, padded)
	return enc, nil
}

func aesDecrypt(key, iv, enc []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	if len(enc)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("not multiple of block size: %d", len(enc))
	}
	plain := make([]byte, len(enc))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(plain, enc)
	padLen := int(plain[len(plain)-1])
	if padLen > aes.BlockSize || padLen == 0 {
		return nil, fmt.Errorf("bad padding: %d", padLen)
	}
	return plain[:len(plain)-padLen], nil
}

// Ensure imports used.
var _ = bytes.NewReader
var _ = rand.Reader
var _ = binary.BigEndian
