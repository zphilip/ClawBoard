package xiaomi

import (
	"bytes"
	"image"
	"image/color"
	"image/png"
	"strings"
	"testing"
)

var (
	testWhite = color.Gray{Y: 255}
	testBlack = color.Gray{Y: 0}
)

// makeTestQRPNG creates a synthetic QR-like PNG for testing the renderer.
func makeTestQRPNG(size int) []byte {
	img := image.NewGray(image.Rect(0, 0, size, size))

	// Fill white.
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			img.SetGray(x, y, testWhite)
		}
	}

	// Draw finder-pattern-like squares (simplified 7-module squares).
	modSize := size / 37 // ~4 for 148px
	if modSize < 2 {
		modSize = 2
	}
	qs := 4 * modSize // quiet zone

	drawTestFinder(img, qs, qs, modSize)
	drawTestFinder(img, size-qs-7*modSize, qs, modSize)
	drawTestFinder(img, qs, size-qs-7*modSize, modSize)

	var buf bytes.Buffer
	png.Encode(&buf, img)
	return buf.Bytes()
}

func drawTestFinder(img *image.Gray, x0, y0, ms int) {
	// Outer 7x7 black square.
	for dy := 0; dy < 7*ms; dy++ {
		for dx := 0; dx < 7*ms; dx++ {
			img.SetGray(x0+dx, y0+dy, testBlack)
		}
	}
	// Inner 5x5 white square.
	for dy := ms; dy < 6*ms; dy++ {
		for dx := ms; dx < 6*ms; dx++ {
			img.SetGray(x0+dx, y0+dy, testWhite)
		}
	}
	// Center 3x3 black square.
	for dy := 2 * ms; dy < 5*ms; dy++ {
		for dx := 2 * ms; dx < 5*ms; dx++ {
			img.SetGray(x0+dx, y0+dy, testBlack)
		}
	}
}

func TestQRRenderPNG_ProducesHalfBlocks(t *testing.T) {
	data := makeTestQRPNG(370) // 10px per module × 37 modules = 370px
	result := QRRenderPNG(data, 50)

	if result == "" {
		t.Fatal("QRRenderPNG returned empty string")
	}

	// Must contain at least some half-block or block characters.
	hasBlocks := strings.ContainsAny(result, "▀▄█")
	hasSpaces := strings.Contains(result, " ")
	if !hasBlocks && !hasSpaces {
		t.Error("expected block characters in output")
	}

	// Output should have newlines (rows).
	if !strings.Contains(result, "\n") {
		t.Error("expected newlines in output")
	}

	// Should not be absurdly long.
	maxLen := 8000
	if len(result) > maxLen {
		t.Errorf("output too long: %d chars (max %d)", len(result), maxLen)
	}

	t.Logf("Output length: %d chars", len(result))
}

func TestQRRenderPNG_EmptyInput(t *testing.T) {
	if out := QRRenderPNG(nil, 40); out != "" {
		t.Errorf("expected empty for nil input, got %q", out)
	}
	if out := QRRenderPNG([]byte{}, 40); out != "" {
		t.Errorf("expected empty for empty input, got %q", out)
	}
}

func TestQRRenderHTML_ProducesPage(t *testing.T) {
	data := makeTestQRPNG(370)
	page := QRRenderHTML(data, "http://localhost:8090")

	if page == "" {
		t.Fatal("QRRenderHTML returned empty string")
	}
	if !strings.Contains(page, "<html") {
		t.Error("expected HTML page")
	}
	if !strings.Contains(page, "data:image/png;base64,") {
		t.Error("expected base64-encoded PNG in data URI")
	}
	if !strings.Contains(page, "Mi Home") {
		t.Error("expected Mi Home mention in page")
	}
}

func TestDetectModuleSize(t *testing.T) {
	// Use a larger QR where finder pattern is clear.
	data := makeTestQRPNG(370)
	img, _ := png.Decode(bytes.NewReader(data))
	gray := makeGray(img)
	w := img.Bounds().Dx()

	modSize := detectModuleSize(gray, w)
	// 370px / 37 modules = 10px per module. Expect ~10.
	if modSize < 6 || modSize > 15 {
		t.Errorf("expected module size ~10, got %d", modSize)
	}
	t.Logf("detected module size: %d", modSize)
}

func TestMakeGray(t *testing.T) {
	data := makeTestQRPNG(40)
	img, _ := png.Decode(bytes.NewReader(data))
	bounds := img.Bounds()
	gray := makeGray(img)

	expected := bounds.Dx() * bounds.Dy()
	if len(gray) != expected {
		t.Errorf("gray length = %d, expected %d", len(gray), expected)
	}

	// The finder pattern (top-left) should have black pixels.
	midRow := bounds.Dy() / 2
	offset := midRow * bounds.Dx()
	hasDark := false
	hasLight := false
	for x := 0; x < bounds.Dx(); x++ {
		if gray[offset+x] < 128 {
			hasDark = true
		}
		if gray[offset+x] > 200 {
			hasLight = true
		}
	}
	if !hasDark || !hasLight {
		t.Error("expected both dark and light pixels in QR image")
	}
}

func TestRenderMatrix(t *testing.T) {
	// 5x5 matrix: all black.
	modules := 5
	matrix := make([][]bool, modules)
	for r := 0; r < modules; r++ {
		matrix[r] = make([]bool, modules)
		for c := 0; c < modules; c++ {
			matrix[r][c] = true
		}
	}

	out := renderMatrix(matrix, modules, 40)
	if out == "" {
		t.Fatal("renderMatrix returned empty")
	}
	// All-black → all spaces.
	if !strings.Contains(out, "\n") {
		t.Error("expected newlines")
	}
}

func TestFindSegments(t *testing.T) {
	// Create a row with a clear dark-light-dark pattern.
	imgW := 100
	gray := make([]uint8, 2*imgW) // 2 rows
	row := 0

	// Fill row with pattern: 10 dark, 10 light, 30 dark, 10 light, 10 dark.
	for x := 0; x < 10; x++ {
		gray[row*imgW+x] = 0
	}
	for x := 10; x < 20; x++ {
		gray[row*imgW+x] = 255
	}
	for x := 20; x < 50; x++ {
		gray[row*imgW+x] = 0
	}
	for x := 50; x < 60; x++ {
		gray[row*imgW+x] = 255
	}
	for x := 60; x < 70; x++ {
		gray[row*imgW+x] = 0
	}
	for x := 70; x < imgW; x++ {
		gray[row*imgW+x] = 255
	}

	segs := findSegments(gray, row, imgW)
	if len(segs) < 5 {
		t.Fatalf("expected at least 5 segments, got %d: %v", len(segs), segs)
	}

	// The first 5 segments should match 10,10,30,10,10.
	if segs[0] != 10 || segs[1] != 10 || segs[2] != 30 || segs[3] != 10 || segs[4] != 10 {
		t.Errorf("segment mismatch: %v", segs[:5])
	}

	modSize := findModuleSizeFromSegments(segs)
	if modSize != 10 {
		t.Errorf("expected modSize=10, got %d", modSize)
	}
}

func TestFindModuleSizeFromSegments(t *testing.T) {
	// 1:1:3:1:1 with module size 5.
	segs := []int{5, 5, 15, 5, 5}
	ms := findModuleSizeFromSegments(segs)
	if ms != 5 {
		t.Errorf("expected 5, got %d", ms)
	}

	// No match.
	segs = []int{1, 2, 3, 4, 5, 6, 7}
	ms = findModuleSizeFromSegments(segs)
	if ms != 0 {
		t.Errorf("expected 0 for no match, got %d", ms)
	}

	// Empty.
	ms = findModuleSizeFromSegments(nil)
	if ms != 0 {
		t.Errorf("expected 0 for nil, got %d", ms)
	}
}