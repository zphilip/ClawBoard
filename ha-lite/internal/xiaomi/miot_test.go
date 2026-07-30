package xiaomi

import (
	"encoding/base64"
	"strings"
	"testing"
)

func TestRC4RoundTrip(t *testing.T) {
	// Test that encrypt + decrypt produces the original.
	plain := `{"code":0,"result":{"list":[{"name":"test"}]}}`
	password := base64.StdEncoding.EncodeToString([]byte("test-key-123456"))

	enc, err := encryptRC4(password, plain)
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	t.Logf("encrypted: %s", enc[:min(40, len(enc))])

	dec, err := decryptRC4(password, enc)
	if err != nil {
		t.Fatalf("decrypt: %v", err)
	}

	if dec != plain {
		t.Errorf("round-trip failed:\n  want: %s\n  got:  %s", plain, dec)
	}
}

func TestSignNonceConsistency(t *testing.T) {
	// Test that signNonce produces consistent results.
	ssecurity := "VDCfbXEAA0y5mAjv"  // base64-encoded
	nonce := generateNonce(1234567890000)

	sn1 := signNonce(nonce, ssecurity)
	sn2 := signNonce(nonce, ssecurity)

	if sn1 != sn2 {
		t.Errorf("signNonce not consistent: %s != %s", sn1, sn2)
	}
	t.Logf("signedNonce: %s", sn1)

	// Verify it's valid base64.
	_, err := base64.StdEncoding.DecodeString(sn1)
	if err != nil {
		t.Errorf("signedNonce is not valid base64: %v", err)
	}

	// Verify it's 32 bytes (SHA256 output).
	decoded, _ := base64.StdEncoding.DecodeString(sn1)
	if len(decoded) != 32 {
		t.Errorf("signedNonce decoded length: %d, expected 32", len(decoded))
	}
}

func TestGenerateNonceFormat(t *testing.T) {
	nonce := generateNonce(1234567890000)
	t.Logf("nonce: %s", nonce)

	decoded, err := base64.StdEncoding.DecodeString(nonce)
	if err != nil {
		t.Fatalf("nonce not valid base64: %v", err)
	}
	if len(decoded) != 12 {
		t.Errorf("nonce length: %d, expected 12", len(decoded))
	}
}

func TestGenerateEncSignature(t *testing.T) {
	params := make(map[string][]string)
	params["data"] = []string{`{"test":true}`}

	sig := generateEncSignature(
		"https://api.io.mi.com/app/v2/homeroom/gethome",
		"POST",
		"testSignedNonce123",
		params,
	)
	t.Logf("signature: %s", sig)

	if sig == "" {
		t.Error("empty signature")
	}
}

func TestEncryptionDecryptionWithRealKey(t *testing.T) {
	// Simulate the full flow: generate nonce → sign → encrypt → decrypt.
	millis := int64(1753856000000)
	nonce := generateNonce(millis)
	ssecurity := "VDCfbXEAA0y5mAjv"
	signedNonce := signNonce(nonce, ssecurity)
	t.Logf("nonce: %s", nonce)
	t.Logf("signedNonce: %s", signedNonce)

	// Recompute signedNonce from nonce (as decryption path would).
	signedNonce2 := signNonce(nonce, ssecurity)
	if signedNonce != signedNonce2 {
		t.Fatal("signedNonce mismatch on recompute")
	}

	plain := `{"code":0,"message":"ok"}`
	enc, _ := encryptRC4(signedNonce, plain)
	dec, err := decryptRC4(signedNonce2, enc)
	if err != nil {
		t.Fatalf("decrypt: %v", err)
	}

	if !strings.Contains(dec, "ok") {
		t.Errorf("decrypted output doesn't contain 'ok': %s", dec)
	}
	if dec != plain {
		t.Errorf("decrypt mismatch, got: %s", dec)
	}
}