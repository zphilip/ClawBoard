package miio

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/md5"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/rand"
	"net"
	"syscall"
	"time"
)

const (
	DefaultPort = 54321
	ReadTimeout = 5 * time.Second
	Magic       = 0x2131
	HeaderLen   = 32
)

// RawHello is the 32-byte discovery packet (matching python-miio).
var RawHello = []byte{
	0x21, 0x31, 0x00, 0x20,
	0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
	0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
	0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
	0xFF, 0xFF, 0xFF, 0xFF,
}

type Device struct {
	IP    string
	Token string
	Model string
}

func NewDevice(ip, token, model string) *Device {
	return &Device{IP: ip, Token: token, Model: model}
}

func md5Hash(data []byte) []byte {
	h := md5.Sum(data)
	return h[:]
}

func aesDecrypt(key, iv, ciphertext []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	if len(ciphertext) == 0 || len(ciphertext)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("miio: invalid ciphertext length %d", len(ciphertext))
	}
	plain := make([]byte, len(ciphertext))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(plain, ciphertext)
	// Remove PKCS7 padding.
	padLen := int(plain[len(plain)-1])
	if padLen < 1 || padLen > aes.BlockSize {
		return plain, nil // no valid padding, return as-is
	}
	return plain[:len(plain)-padLen], nil
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

// Send sends a miIO command using RAW hello handshake (matching python-miio).
// cmd must be map[string]interface{} with "method" and "params" keys.
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

	tokenBytes, err := hex.DecodeString(d.Token)
	if err != nil {
		return nil, fmt.Errorf("miio: bad token: %w", err)
	}
	key := md5Hash(tokenBytes)
	iv := md5Hash(append(key, tokenBytes...))

	addr := &net.UDPAddr{IP: net.ParseIP(d.IP), Port: DefaultPort}
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		return nil, fmt.Errorf("miio: listen: %w", err)
	}
	defer conn.Close()

	// SO_BROADCAST (matching python-miio).
	if sc, err := conn.SyscallConn(); err == nil {
		sc.Control(func(fd uintptr) {
			syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 0x6, 1)
		})
	}

	// Step 1: RAW hello ×3 → get device_id + timestamp.
	for i := 0; i < 3; i++ {
		conn.SetWriteDeadline(time.Now().Add(ReadTimeout))
		if _, err := conn.WriteTo(RawHello, addr); err != nil {
			return nil, fmt.Errorf("miio: write hello: %w", err)
		}
	}
	conn.SetReadDeadline(time.Now().Add(ReadTimeout))
	buf := make([]byte, 4096)
	n, _, err := conn.ReadFrom(buf)
	if err != nil {
		return nil, fmt.Errorf("miio: read hello: %w", err)
	}
	if n < 16 {
		return nil, fmt.Errorf("miio: hello response too short: %d", n)
	}
	deviceID := binary.BigEndian.Uint32(buf[8:12])
	devTS := binary.BigEndian.Uint32(buf[12:16]) + 1
	if deviceID == 0 || deviceID == 0xFFFFFFFF {
		return nil, fmt.Errorf("miio: invalid device_id: 0x%08x", deviceID)
	}

	// Step 2: Encrypted command (header: HHIII = 16 bytes, matching python-miio).
	cmdID := rand.Intn(9000) + 1000
	command := map[string]interface{}{"id": cmdID, "method": method, "params": params}
	cmdPayload, _ := json.Marshal(command)

	encrypted, err := aesEncrypt(key, iv, cmdPayload)
	if err != nil {
		return nil, fmt.Errorf("miio: encrypt: %w", err)
	}

	header := make([]byte, 16)
	binary.BigEndian.PutUint16(header[0:2], Magic)
	binary.BigEndian.PutUint16(header[2:4], uint16(HeaderLen+len(encrypted)))
	binary.BigEndian.PutUint32(header[4:8], 0)
	binary.BigEndian.PutUint32(header[8:12], deviceID)
	binary.BigEndian.PutUint32(header[12:16], devTS)

	checksum := md5Hash(append(append(header, tokenBytes...), encrypted...))
	pkt := make([]byte, 0, HeaderLen+len(encrypted))
	pkt = append(pkt, header...)
	pkt = append(pkt, checksum...)
	pkt = append(pkt, encrypted...)

	conn.SetDeadline(time.Now().Add(ReadTimeout))
	if _, err := conn.WriteTo(pkt, addr); err != nil {
		return nil, fmt.Errorf("miio: write command: %w", err)
	}
	n, _, err = conn.ReadFrom(buf)
	if err != nil {
		return nil, fmt.Errorf("miio: read response: %w", err)
	}

	// Decrypt response (skip 32-byte header: 16 header + 16 checksum).
	if n > 32 {
		decrypted, err := aesDecrypt(key, iv, buf[32:n])
		if err == nil && len(decrypted) > 0 {
			return json.RawMessage(decrypted), nil
		}
		// Fallback: return raw "ok" if decryption fails (device ACK).
	}
	return json.RawMessage(`"ok"`), nil
}
