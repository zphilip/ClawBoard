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
	"time"
)

const (
	DefaultPort = 54321
	ReadTimeout = 5 * time.Second
	Magic       = 0x2131
	HeaderLen   = 32
)

// RawHello is the 32-byte discovery packet (matches python-miio MiIOProtocol.discover).
// It uses 0xFF padding and requests the device to respond with its device_id and timestamp.
var RawHello = []byte{
	0x21, 0x31, 0x00, 0x20, // magic + length
	0xFF, 0xFF, 0xFF, 0xFF, // device_id = broadcast
	0xFF, 0xFF, 0xFF, 0xFF, // timestamp
	0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, // checksum
	0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, // (no payload)
}

type Device struct {
	IP    string
	Token string
	Model string
}

func NewDevice(ip, token, model string) *Device {
	return &Device{IP: ip, Token: token, Model: model}
}

// ── Crypto ───────────────────────────────────────────────────────────────────

func md5Hash(data []byte) []byte {
	h := md5.Sum(data)
	return h[:]
}

func miioKeyIV(tokenHex string) (key, iv []byte, err error) {
	tokenBytes, err := hex.DecodeString(tokenHex)
	if err != nil {
		return nil, nil, err
	}
	key = md5Hash(tokenBytes)
	iv = md5Hash(append(key, tokenBytes...))
	return key, iv, nil
}

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
		return nil, fmt.Errorf("miio: not multiple of block size: %d", len(enc))
	}
	plain := make([]byte, len(enc))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(plain, enc)
	padLen := int(plain[len(plain)-1])
	if padLen > aes.BlockSize || padLen == 0 {
		return nil, fmt.Errorf("miio: bad padding: %d", padLen)
	}
	return plain[:len(plain)-padLen], nil
}

func buildEncryptedPacket(tokenHex string, payload []byte, deviceID uint32, timestamp uint32) ([]byte, error) {
	key, iv, err := miioKeyIV(tokenHex)
	if err != nil {
		return nil, err
	}
	encrypted, err := aesEncrypt(key, iv, payload)
	if err != nil {
		return nil, err
	}
	tokenBytes, _ := hex.DecodeString(tokenHex)

	header := make([]byte, 16)
	binary.BigEndian.PutUint16(header[0:2], Magic)
	binary.BigEndian.PutUint16(header[2:4], uint16(HeaderLen+len(encrypted)))
	// header[4:8] = 0
	binary.BigEndian.PutUint32(header[8:12], deviceID)
	binary.BigEndian.PutUint32(header[12:16], timestamp)

	checksum := md5Hash(append(append(header, tokenBytes...), encrypted...))

	pkt := make([]byte, 0, HeaderLen+len(encrypted))
	pkt = append(pkt, header...)
	pkt = append(pkt, checksum...)
	pkt = append(pkt, encrypted...)
	return pkt, nil
}

func parseEncryptedResponse(tokenHex string, data []byte) ([]byte, error) {
	if len(data) < HeaderLen {
		return nil, fmt.Errorf("miio: packet too short: %d", len(data))
	}
	magic := binary.BigEndian.Uint16(data[0:2])
	if magic != Magic {
		return nil, fmt.Errorf("miio: bad magic: 0x%04x", magic)
	}
	key, iv, err := miioKeyIV(tokenHex)
	if err != nil {
		return nil, err
	}
	return aesDecrypt(key, iv, data[HeaderLen:])
}

// ── Send ─────────────────────────────────────────────────────────────────────

// Send sends a miIO command using RAW hello handshake (matches python-miio).
// Step 1: Send RAW 32-byte hello to get device_id + timestamp.
// Step 2: Send encrypted command with the real device_id and timestamp+1.
func (d *Device) Send(cmd interface{}) (json.RawMessage, error) {
	cmdMap, ok := cmd.(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("miio: command must be map[string]interface{}")
	}
	method, _ := cmdMap["method"].(string)
	params, _ := cmdMap["params"].([]interface{})
	if params == nil {
		params = []interface{}{}
	}

	addr := &net.UDPAddr{IP: net.ParseIP(d.IP), Port: DefaultPort}
	conn, err := net.DialUDP("udp", nil, addr)
	if err != nil {
		return nil, fmt.Errorf("miio: dial: %w", err)
	}
	defer conn.Close()

	// Step 1: Send RAW hello (matching python-miio discover).
	conn.SetDeadline(time.Now().Add(ReadTimeout))
	if _, err := conn.Write(RawHello); err != nil {
		return nil, fmt.Errorf("miio: write raw hello: %w", err)
	}
	buf := make([]byte, 4096)
	n, err := conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("miio: read raw hello response: %w", err)
	}
	if n < 16 {
		return nil, fmt.Errorf("miio: raw hello response too short: %d", n)
	}

	deviceID := binary.BigEndian.Uint32(buf[8:12])
	devTS := binary.BigEndian.Uint32(buf[12:16])
	if deviceID == 0 || deviceID == 0xFFFFFFFF {
		return nil, fmt.Errorf("miio: invalid device_id in hello response: 0x%08x", deviceID)
	}
	devTS++ // increment as python-miio does (timedelta(seconds=1))

	// Step 2: Send encrypted command with real device_id and timestamp.
	cmdID := rand.Intn(9000) + 1000
	command := map[string]interface{}{"id": cmdID, "method": method, "params": params}
	cmdPayload, _ := json.Marshal(command)

	cmdPkt, err := buildEncryptedPacket(d.Token, cmdPayload, deviceID, devTS)
	if err != nil {
		return nil, fmt.Errorf("miio: build command packet: %w", err)
	}

	conn.SetDeadline(time.Now().Add(ReadTimeout))
	if _, err := conn.Write(cmdPkt); err != nil {
		return nil, fmt.Errorf("miio: write command: %w", err)
	}
	n, err = conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("miio: read command response: %w", err)
	}

	plain, err := parseEncryptedResponse(d.Token, buf[:n])
	if err != nil {
		return nil, fmt.Errorf("miio: parse command response: %w", err)
	}

	return json.RawMessage(plain), nil
}

// Ensure imports used.
var _ = bytes.NewReader
