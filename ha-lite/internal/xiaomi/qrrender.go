package xiaomi

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"strings"
)

// ── QR ASCII rendering ────────────────────────────────────────────────────────

// QRRenderPNG renders a QR code PNG as Unicode half-block art for terminal display.
//
// The rendering maps the PNG image directly to Unicode half-block characters.
// Each output character represents a small rectangular region of the source image
// (2 pixel rows × N pixel columns). The output width is chosen so that each QR
// module spans ~2 characters, producing a clean, scannable QR.
//
// For dark-background terminals, black modules are rendered as spaces and white
// modules as █ full-block characters.
func QRRenderPNG(pngData []byte, maxWidth int) string {
	img, err := png.Decode(bytes.NewReader(pngData))
	if err != nil {
		return ""
	}

	bounds := img.Bounds()
	imgW := bounds.Dx()
	imgH := bounds.Dy()
	if imgW == 0 || imgH == 0 {
		return ""
	}

	// Convert to grayscale.
	gray := makeGray(img)

	// Detect module size to pick a good output width.
	// Target: each module spans ~2.2 output characters (for clean half-block edges).
	modSize := detectModuleSize(gray, imgW)
	if modSize < 2 {
		// Fallback: assume version 5 QR (37 modules) for Xiaomi QR codes.
		modSize = imgW / 37
	}
	if modSize < 2 {
		return ""
	}
	modules := imgW / modSize

	// Output width: 2.2 chars per module for clean rendering.
	// Clamp to [modules, maxWidth] or [modules, 80] if maxWidth is 0.
	outW := modules * 22 / 10
	if outW < modules {
		outW = modules
	}
	limit := maxWidth
	if limit <= 0 {
		limit = 80
	}
	if outW > limit {
		outW = limit
	}

	outH := outW * 2 // each char covers 2 rows, square aspect

	var sb strings.Builder
	sb.Grow(outW*(outH/2+1) + outH)

	sb.WriteByte('\n')

	// Each character maps to a region of (imgW/outW) pixels wide × (imgH/outH) pixels tall.
	// This naturally handles any image size without module alignment issues.
	for cy := 0; cy < outH/2; cy++ {
		for cx := 0; cx < outW; cx++ {
			// Region for the top half-row of this character block.
			x0 := cx * imgW / outW
			x1 := (cx + 1) * imgW / outW
			yTop0 := (cy * 2) * imgH / outH
			yTop1 := (cy*2 + 1) * imgH / outH
			// Region for the bottom half-row.
			yBot0 := (cy*2 + 1) * imgH / outH
			yBot1 := (cy*2 + 2) * imgH / outH

			if x1 <= x0 {
				x1 = x0 + 1
			}
			if yTop1 <= yTop0 {
				yTop1 = yTop0 + 1
			}
			if yBot1 <= yBot0 {
				yBot1 = yBot0 + 1
			}

			topBlack := regionDarker(gray, imgW, imgH, x0, yTop0, x1-x0, yTop1-yTop0)
			botBlack := regionDarker(gray, imgW, imgH, x0, yBot0, x1-x0, yBot1-yBot0)

			switch {
			case topBlack && botBlack:
				sb.WriteRune(' ') // both dark → space (blends with dark terminal bg)
			case topBlack && !botBlack:
				sb.WriteRune('▄') // top dark, bottom light
			case !topBlack && botBlack:
				sb.WriteRune('▀') // top light, bottom dark
			default:
				sb.WriteRune('█') // both light → full block
			}
		}
		sb.WriteByte('\n')
	}

	sb.WriteByte('\n')
	return sb.String()
}

// regionDarker returns true if the majority of pixels in the given region are
// darker than the midpoint threshold (128).
func regionDarker(gray []uint8, imgW, imgH, x0, y0, w, h int) bool {
	if w <= 0 {
		w = 1
	}
	if h <= 0 {
		h = 1
	}

	dark := 0
	total := 0
	for y := y0; y < y0+h && y < imgH; y++ {
		rowOff := y * imgW
		for x := x0; x < x0+w && x < imgW; x++ {
			if gray[rowOff+x] < 128 {
				dark++
			}
			total++
		}
	}

	if total == 0 {
		return false
	}
	return dark > total/2
}

// renderMatrix converts a boolean module matrix to half-block ASCII art.
// true = black module, false = white module.
func renderMatrix(matrix [][]bool, modules, maxWidth int) string {
	outW := modules
	if maxWidth > 0 && outW > maxWidth {
		outW = maxWidth
	}

	var sb strings.Builder
	sb.Grow(outW*(modules/2+1) + modules)

	sb.WriteByte('\n')

	for r := 0; r < modules; r += 2 {
		for c := 0; c < modules; c++ {
			top := matrix[r][c]
			bot := r+1 < modules && matrix[r+1][c]

			switch {
			case top && bot:
				sb.WriteRune(' ')
			case top && !bot:
				sb.WriteRune('▄')
			case !top && bot:
				sb.WriteRune('▀')
			default:
				sb.WriteRune('█')
			}
		}
		sb.WriteByte('\n')
	}

	sb.WriteByte('\n')
	return sb.String()
}

// ── Module size detection ─────────────────────────────────────────────────────

// detectModuleSize determines the QR code module size in pixels.
//
// Strategy: scan multiple rows and columns looking for the characteristic
// 1:1:3:1:1 finder-pattern ratio. If pattern detection fails, fall back to
// assuming version 5 (37×37 modules) — the most common size for Xiaomi QR codes.
func detectModuleSize(gray []uint8, imgW int) int {
	imgH := len(gray) / imgW
	if imgH < 20 || imgW < 20 {
		return 0
	}

	// Try horizontal and vertical scans for the finder pattern.
	best := 0

	for row := imgH / 12; row < imgH/3; row += 3 {
		segs := findSegments(gray, row, imgW)
		if ms := findModuleSizeFromSegments(segs); ms > best {
			best = ms
		}
	}
	for col := imgW / 12; col < imgW/3; col += 3 {
		segs := findVerticalSegments(gray, col, imgW, imgH)
		if ms := findModuleSizeFromSegments(segs); ms > best {
			best = ms
		}
	}

	if best > 1 && best < imgW/10 {
		return best
	}

	// Fallback: assume version 5 QR (37 modules).
	ms := imgW / 37
	if ms < 2 {
		return 0
	}
	return ms
}

// findSegments returns the lengths of alternating dark/light pixel runs in a row.
func findSegments(gray []uint8, row, imgW int) []int {
	offset := row * imgW
	var segs []int
	if imgW == 0 {
		return segs
	}

	curDark := gray[offset] < 128
	runLen := 1

	for c := 1; c < imgW; c++ {
		isDark := gray[offset+c] < 128
		if isDark == curDark {
			runLen++
		} else {
			segs = append(segs, runLen)
			curDark = isDark
			runLen = 1
		}
	}
	segs = append(segs, runLen)
	return segs
}

// findVerticalSegments returns lengths of alternating dark/light pixel runs in a column.
func findVerticalSegments(gray []uint8, col, imgW, imgH int) []int {
	var segs []int
	if imgH == 0 {
		return segs
	}

	curDark := gray[col] < 128
	runLen := 1

	for r := 1; r < imgH; r++ {
		isDark := gray[r*imgW+col] < 128
		if isDark == curDark {
			runLen++
		} else {
			segs = append(segs, runLen)
			curDark = isDark
			runLen = 1
		}
	}
	segs = append(segs, runLen)
	return segs
}

// findModuleSizeFromSegments looks for the 1:1:3:1:1 dark-light-dark-light-dark
// ratio that characterizes a QR finder pattern center.
func findModuleSizeFromSegments(segs []int) int {
	if len(segs) < 5 {
		return 0
	}

	for i := 0; i < len(segs)-4; i++ {
		a, b, c, d, e := segs[i], segs[i+1], segs[i+2], segs[i+3], segs[i+4]

		// Skip if any of the 1-module segments are too small (noise).
		if a < 2 || b < 2 || d < 2 || e < 2 {
			continue
		}
		// The 3-module segment must be clearly larger.
		if c < 2*a || c < 2*b || c < 2*d || c < 2*e {
			continue
		}

		// Try with the 1-module segments as base.
		base := (a + b + d + e) / 4
		if base < 2 {
			continue
		}
		ratio := float64(c) / float64(base)
		if ratio >= 2.3 && ratio <= 3.7 {
			return base
		}
	}

	return 0
}

// ── Grayscale conversion ──────────────────────────────────────────────────────

func makeGray(img image.Image) []uint8 {
	bounds := img.Bounds()
	w := bounds.Dx()
	h := bounds.Dy()
	gray := make([]uint8, w*h)

	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			r, g, b, _ := img.At(x+bounds.Min.X, y+bounds.Min.Y).RGBA()
			// 16-bit → 8-bit luminance.
			lum := uint8((19595*uint32(r) + 38470*uint32(g) + 7471*uint32(b) + 1<<15) >> 16)
			gray[y*w+x] = lum
		}
	}
	return gray
}

// ── HTML page ─────────────────────────────────────────────────────────────────

// QRRenderHTML returns an HTML page that displays the QR code as an embedded
// PNG image, suitable for opening in a browser to scan.
func QRRenderHTML(pngData []byte, serverURL string) string {
	b64 := base64.StdEncoding.EncodeToString(pngData)

	return fmt.Sprintf(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ha-lite QR Login</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 100vh;
    padding: 20px;
  }
  .card {
    background: #16213e; border-radius: 16px;
    padding: 40px; text-align: center;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    max-width: 480px; width: 100%%;
  }
  h1 { font-size: 1.5rem; margin-bottom: 8px; color: #ffffff; }
  .subtitle { color: #8892b0; font-size: 0.9rem; margin-bottom: 24px; }
  .qr-img {
    background: #ffffff; border-radius: 12px;
    padding: 16px; display: inline-block;
  }
  .qr-img img { display: block; max-width: 280px; height: auto; }
  .steps {
    text-align: left; margin-top: 24px;
    color: #8892b0; font-size: 0.9rem; line-height: 1.8;
  }
  .steps span { color: #64ffda; font-weight: bold; margin-right: 6px; }
  .footer { margin-top: 24px; color: #575d6e; font-size: 0.75rem; }
</style>
</head>
<body>
<div class="card">
  <h1>🏠 ha-lite QR Login</h1>
  <p class="subtitle">Scan with Mi Home app to login</p>
  <div class="qr-img">
    <img src="data:image/png;base64,%s" alt="Xiaomi Login QR Code">
  </div>
  <div class="steps">
    <p><span>1.</span> Open <b>Mi Home / 米家</b> app on your phone</p>
    <p><span>2.</span> Go to <b>Profile → top-right → Scan</b></p>
    <p><span>3.</span> Scan the QR code above</p>
    <p><span>4.</span> After scan, run:<br><code>curl -X POST %s/api/login/qr/collect</code></p>
  </div>
  <p class="footer">ha-lite server • QR session expires in 120s</p>
</div>
</body>
</html>`, b64, serverURL)
}

// Keep color import used.
var _ = color.Gray{}