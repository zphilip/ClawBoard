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
	"syscall"
	"time"
)

const (
	DefaultPort = 54321
	ReadTimeout = 5 * time.Second
	Magic       = 0x2131
	HeaderLen   = 32
)

// RawHello is the 32-byte discovery packet (matches python-miio).
// python-miio uses: bytes.fromhex("21310020" + "ff" * 28)
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
	// header[4:8] = 0 (unknown field)
	binary.BigEndian.PutUint32(header[4:8], 0)
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

	// Use ListenUDP("udp4") + WriteTo (matching Python socket.sendto exactly).
	// "udp4" forces IPv4-only to avoid dual-stack issues.
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		return nil, fmt.Errorf("miio: listen: %w", err)
	}
	defer conn.Close()

	// Set SO_BROADCAST (matching python-miio). SO_BROADCAST = 0x0006 on Linux.
	if sc, err := conn.SyscallConn(); err == nil {
		sc.Control(func(fd uintptr) {
			syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 0x6, 1)
		})
	} else {
		fmt.Printf("[miio] WARNING: cannot set SO_BROADCAST: %v\n", err)
	}

	// Step 1: Send RAW hello 3 times (matching python-miio discover).
	fmt.Printf("[miio] sending RAW hello to %s:%d (len=%d, local=%s)\n", d.IP, DefaultPort, len(RawHello), conn.LocalAddr())
	for i := 0; i < 3; i++ {
		conn.SetWriteDeadline(time.Now().Add(ReadTimeout))
		nw, err := conn.WriteTo(RawHello, addr)
		fmt.Printf("[miio] hello send #%d: wrote=%d err=%v\n", i+1, nw, err)
		if err != nil {
			return nil, fmt.Errorf("miio: write raw hello: %w", err)
		}
	}
	conn.SetReadDeadline(time.Now().Add(ReadTimeout))
	buf := make([]byte, 4096)
	n, from, err := conn.ReadFrom(buf)
	if err != nil {
		fmt.Printf("[miio] RAW hello response error from %v: %v\n", from, err)
		return nil, fmt.Errorf("miio: read raw hello response: %w", err)
	}
	fmt.Printf("[miio] RAW hello response: %d bytes from %v, hex=%x\n", n, from, buf[:min(n, 32)])
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
	if _, err := conn.WriteTo(cmdPkt, addr); err != nil {
		return nil, fmt.Errorf("miio: write command: %w", err)
	}
	n, _, err = conn.ReadFrom(buf)
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
