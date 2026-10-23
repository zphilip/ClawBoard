#!/usr/bin/env python3
"""End-to-end VLM coordinate accuracy test — real screenshot + known markers.

Pipeline (mirrors artemis_agent.py exactly):
  1. Take a real screenshot from the device (ADB screencap -p)
  2. Overlay numbered colored markers at known pixel positions
  3. _encode_image() → resize to 2000×923
  4. _call_vlm() → ask VLM to locate each marker
  5. Compare VLM coordinates with ground truth

This tests VLM accuracy on a REAL screenshot with all its visual complexity.
No fake fonts, no synthetic layouts.

Usage:
    python3 vlm_coord_e2e.py [--runs N] [--device SERIAL]
    python3 vlm_coord_e2e.py --screenshot /path/to/screenshot.png --runs 2
"""

from __future__ import annotations

import json, math, os, re, subprocess, sys, textwrap, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artemis_agent import _encode_image, _call_vlm, load_skill_config

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: pip install Pillow")
    sys.exit(1)

OUT_DIR = Path("/tmp/vlm_coord_e2e")


# ── Marker definitions (device-pixel centers) ───────────────────────────────
#
# Markers are placed at diverse positions across the screen to test:
#   - Top edge, bottom edge, corners, center
#   - Both dense (small targets) and sparse (easy targets)
#   - Horizontal strips (thin bars like search boxes)
#
# Adjust positions to avoid overlapping status bar on your device.

@dataclass
class Marker:
    name: str
    dev_x: int
    dev_y: int
    size: int = 40             # visual marker radius in device px
    norm_x: float = field(init=False)
    norm_y: float = field(init=False)

    def __post_init__(self):
        self.norm_x = round(self.dev_x / DEV_W * 1000, 1)
        self.norm_y = round(self.dev_y / DEV_H * 1000, 1)

    @property
    def tolerance_dev_px(self) -> int:
        """Max tap deviation that still 'hits' — size plus some margin."""
        return self.size + 10


DEV_W = 3120
DEV_H = 1440

# Marker positions — spread across entire screen
# Format: (name, x, y, size)
_MARKER_SPECS = [
    # Top row: left, center, right
    ("P1_top_left",       160,   80, 35),
    ("P2_top_center",    1560,   80, 35),
    ("P3_top_right",     2960,   80, 35),
    # Upper-middle: thin horizontal strip area (like search bars)
    ("P4_bar_left",       400,  220, 30),
    ("P5_bar_center",    1560,  220, 30),
    ("P6_bar_right",     2720,  220, 30),
    # Content center: large isolation
    ("P7_deep_left",      300,  720, 35),
    ("P8_deep_center",   1560,  720, 45),
    ("P9_deep_right",    2820,  720, 35),
    # Lower area: above bottom nav
    ("P10_low_left",      400, 1200, 30),
    ("P11_low_center",   1560, 1200, 30),
    ("P12_low_right",    2720, 1200, 30),
    # Bottom: near nav bar edge
    ("P13_nav_left",      520, 1380, 25),
    ("P14_nav_center",   1310, 1380, 25),
    ("P15_nav_right",    2600, 1380, 25),
]

MARKERS = [Marker(*spec) for spec in _MARKER_SPECS]

# Bright distinct colors for markers (one per marker)
MARKER_COLORS = [
    "#FF4444", "#44FF44", "#4488FF", "#FFAA00", "#FF44FF",
    "#00CCCC", "#FF8888", "#88FF88", "#8888FF", "#FFCC44",
    "#CC44CC", "#44CCCC", "#FF6688", "#66FF88", "#6688FF",
]


# ── Screenshot helpers ──────────────────────────────────────────────────────

def _find_adb() -> str:
    r = subprocess.run(["which", "adb"], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout.strip()
    return "adb"


def take_screenshot(device: str, dest: Path) -> bool:
    """Same pipeline as agent's _take_screenshot."""
    adb = _find_adb()
    r = subprocess.run(
        [adb, "-s", device, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=15,
    )
    if r.returncode == 0 and r.stdout:
        dest.write_bytes(r.stdout)
        return True
    return False


# ── Marker overlay ──────────────────────────────────────────────────────────

def _try_load_font(size: int):
    """Try to find a font that works for ASCII digits."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def overlay_markers(src: Path, dst: Path) -> None:
    """Overlay numbered colored circles at known positions on the screenshot."""
    img = Image.open(src).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _try_load_font(24)
    font_sm = _try_load_font(18)

    for i, (m, color) in enumerate(zip(MARKERS, MARKER_COLORS)):
        r = m.size
        x, y = m.dev_x, m.dev_y

        # Filled circle with slight transparency
        draw.ellipse(
            [x - r, y - r, x + r, y + r],
            fill=color + "CC",   # ~80% opacity
            outline=color,
            width=3,
        )
        # Crosshair
        draw.line([x - r//2, y, x + r//2, y], fill="#FFFFFF", width=2)
        draw.line([x, y - r//2, x, y + r//2], fill="#FFFFFF", width=2)
        # Number label
        draw.text((x + r + 6, y - 10), str(i + 1), font=font, fill="#FFFFFF")

    # Composite onto original
    result = Image.alpha_composite(img, overlay)
    result = result.convert("RGB")
    result.save(dst, format="PNG")
    print(f"  Markers overlaid: {dst} ({result.size[0]}×{result.size[1]})")


# ── VLM calling ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a precision coordinate locator for mobile UI screenshots.

    A screenshot has been taken from a real mobile device. Numbered markers
    (colored circles with crosshairs and white number labels) have been
    overlaid at specific positions.

    COORDINATE SPACE: 1000×1000. Output ALL coordinates in 0-1000 range.
    (0,0) is the top-left corner of the ENTIRE image.
    (1000,1000) is the bottom-right corner.

    Your task: find the exact CENTER (crosshair intersection) of each
    numbered marker and report its (x, y) in 0-1000 normalized space.

    Output ONLY valid JSON — no markdown, no explanation:
    {"markers": [{"id": <int>, "x": <int>, "y": <int>}, ...]}
    """)


def build_user_prompt() -> str:
    lines = [
        "Locate the center (crosshair) of each numbered marker.",
        "Output their coordinates in 0-1000 normalized space.",
        "",
        "Markers to locate:",
    ]
    for i, m in enumerate(MARKERS, 1):
        lines.append(f"  Marker {i}: near top-left" if i <= 3 else
                     f"  Marker {i}: upper-middle" if i <= 6 else
                     f"  Marker {i}: center area" if i <= 9 else
                     f"  Marker {i}: lower area" if i <= 12 else
                     f"  Marker {i}: bottom edge")
    lines.append("")
    lines.append('Output: {"markers": [{"id": 1, "x": <int>, "y": <int>}, ...]}')
    return "\n".join(lines)


def parse_response(raw: str, count: int) -> dict[int, tuple[float, float]] | None:
    if not raw:
        return None
    text = raw.strip()

    # Strip markdown fences
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # Find JSON object with "markers" key
    m = re.search(r'\{.*"markers"\s*:\s*\[.*\]\s*\}', text, re.DOTALL)
    if m:
        text = m.group(0)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        for i in range(len(text), 0, -1):
            try:
                data = json.loads(text[:i])
                break
            except json.JSONDecodeError:
                continue
        else:
            print(f"  ⚠ JSON parse FAILED. Raw: {raw[:300]}")
            return None

    result = {}
    for item in data.get("markers", []):
        try:
            mid = int(item["id"])
            x, y = float(item["x"]), float(item["y"])
            if 1 <= mid <= count:
                result[mid] = (x, y)
        except (KeyError, TypeError, ValueError):
            pass

    missing = set(range(1, count + 1)) - set(result.keys())
    if missing:
        print(f"  ⚠ VLM missed markers: {sorted(missing)}")
    return result


# ── Results ─────────────────────────────────────────────────────────────────

@dataclass
class TResult:
    marker_id: int
    pred_x: float; pred_y: float
    gt_x: float; gt_y: float
    delta_norm: float = 0.0
    delta_dev_x: float = 0.0
    delta_dev_y: float = 0.0
    delta_dev: float = 0.0

    @property
    def hit(self) -> bool:
        m = MARKERS[self.marker_id - 1]
        return (abs(self.delta_dev_x) <= m.tolerance_dev_px and
                abs(self.delta_dev_y) <= m.tolerance_dev_px)


def compute(predictions: dict[int, tuple[float, float]]) -> list[TResult]:
    results = []
    for i, m in enumerate(MARKERS, 1):
        p = predictions.get(i)
        if p is None:
            results.append(TResult(i, 0, 0, m.norm_x, m.norm_y, 999, 9999, 9999, 9999))
            continue
        px, py = p
        dx, dy = px - m.norm_x, py - m.norm_y
        dn = math.sqrt(dx * dx + dy * dy)
        ddx = dx / 1000 * DEV_W
        ddy = dy / 1000 * DEV_H
        dd = math.sqrt(ddx * ddx + ddy * ddy)
        results.append(TResult(i, px, py, m.norm_x, m.norm_y, dn, ddx, ddy, dd))
    return results


def print_report(results: list[TResult], label: str, run_idx: int = 1):
    print(f"\n{'='*100}")
    print(f"  {label} — Run {run_idx} — {len(MARKERS)} markers")
    print(f"{'='*100}")
    hdr = (f"  {'ID':>3s} {'Name':<18s} {'Δxy':>6s} {'Δx':>8s} {'Δy':>8s} "
           f"{'Δdev':>8s}  {'Pred':>20s}  {'GT':>20s}  {'Hit':>4s}")
    print(hdr)
    print("  " + "─" * 98)

    for r in results:
        m = MARKERS[r.marker_id - 1]
        hit = "✅" if r.hit else "❌"
        pred_s = f"({r.pred_x:>6.1f}, {r.pred_y:>6.1f})"
        gt_s = f"({r.gt_x:>6.1f}, {r.gt_y:>6.1f})"
        print(f"  {r.marker_id:>3d} {m.name:<18s} {r.delta_norm:>5.1f}  "
              f"{r.pred_x - r.gt_x:>+7.1f}  {r.pred_y - r.gt_y:>+7.1f}  "
              f"{r.delta_dev:>7.0f}px  {pred_s:>20s}  {gt_s:>20s}  {hit:>4s}")

    valid = [r for r in results if r.delta_norm < 999]
    n = len(valid)
    total = sum(r.delta_norm for r in valid)
    avg = total / n if n > 0 else 999
    hits = sum(1 for r in results if r.hit)
    rate = hits / 15 * 100

    avg_dx = sum(abs(r.pred_x - r.gt_x) for r in valid) / n if n > 0 else 0
    avg_dy = sum(abs(r.pred_y - r.gt_y) for r in valid) / n if n > 0 else 0
    avg_ddx = avg_dx / 1000 * DEV_W
    avg_ddy = avg_dy / 1000 * DEV_H
    avg_dd = sum(r.delta_dev for r in valid) / n if n > 0 else 0

    print("  " + "─" * 98)
    print(f"  SUMMARY: Δxy={avg:.1f}  |Δx|={avg_dx:.1f}({avg_ddx:.0f}px)  "
          f"|Δy|={avg_dy:.1f}({avg_ddy:.0f}px)  "
          f"Δdev={avg_dd:.0f}px  Hit={hits}/{n} ({rate:.0f}%)")

    # By region
    top = [r for r in valid if r.marker_id in (1, 2, 3)]
    thin = [r for r in valid if r.marker_id in (4, 5, 6)]
    mid = [r for r in valid if r.marker_id in (7, 8, 9)]
    low = [r for r in valid if r.marker_id in (10, 11, 12)]
    bot = [r for r in valid if r.marker_id in (13, 14, 15)]

    def _avg(items, attr="delta_norm"):
        return sum(getattr(x, attr) for x in items) / len(items) if items else 0

    def _hits(items):
        return sum(1 for x in items if x.hit)

    print(f"\n  By screen region:")
    print(f"    {'Top edge (y≈80)':<24s} Δ={_avg(top):4.1f} Δdev={_avg(top,'delta_dev'):5.0f}px {_hits(top)}/{len(top)}")
    print(f"    {'Thin bar (y≈220)':<24s} Δ={_avg(thin):4.1f} Δdev={_avg(thin,'delta_dev'):5.0f}px {_hits(thin)}/{len(thin)}")
    print(f"    {'Content center (y≈720)':<24s} Δ={_avg(mid):4.1f} Δdev={_avg(mid,'delta_dev'):5.0f}px {_hits(mid)}/{len(mid)}")
    print(f"    {'Lower area (y≈1200)':<24s} Δ={_avg(low):4.1f} Δdev={_avg(low,'delta_dev'):5.0f}px {_hits(low)}/{len(low)}")
    print(f"    {'Bottom nav (y≈1380)':<24s} Δ={_avg(bot):4.1f} Δdev={_avg(bot,'delta_dev'):5.0f}px {_hits(bot)}/{len(bot)}")

    # Per-axis bias check: is Y systematically off?
    y_errors = [r.pred_y - r.gt_y for r in valid]
    avg_y_bias = sum(y_errors) / len(y_errors) if y_errors else 0
    x_errors = [r.pred_x - r.gt_x for r in valid]
    avg_x_bias = sum(x_errors) / len(x_errors) if x_errors else 0
    print(f"\n  Axis bias:  X mean error = {avg_x_bias:+.1f}  Y mean error = {avg_y_bias:+.1f}")
    if abs(avg_y_bias) > 5:
        print(f"  ⚠ SIGNIFICANT Y-axis bias detected! VLM may be consistently off in Y.")
    print()


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="E2E VLM coordinate test (real screenshot + markers)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--device", type=str, default="a60f6655")
    ap.add_argument("--screenshot", type=str, default=None,
                    help="Use existing screenshot instead of capturing from device")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Get screenshot ──
    raw_path = OUT_DIR / "raw_screenshot.png"
    if args.screenshot:
        import shutil
        shutil.copy(args.screenshot, raw_path)
        print(f"  Using provided screenshot: {raw_path}")
    else:
        print(f"  Capturing screenshot from device {args.device}...")
        if not take_screenshot(args.device, raw_path):
            print("  ❌ Screenshot failed")
            sys.exit(1)
        print(f"  Captured: {raw_path}")

    # Detect resolution from screenshot
    with Image.open(raw_path) as img:
        w, h = img.size
        global DEV_W, DEV_H, MARKERS
        DEV_W, DEV_H = w, h
        # Recalculate markers for this resolution
        MARKERS = [Marker(*spec) for spec in _MARKER_SPECS]
        print(f"  Resolution: {w}×{h}")

    # ── Step 2: Overlay markers ──
    marked_path = OUT_DIR / "marked_screenshot.png"
    overlay_markers(raw_path, marked_path)

    # Print ground truth
    print(f"\n  Ground truth (device px → 0-1000 norm, tolerance):")
    for m in MARKERS:
        print(f"    {m.name:<18s} dev({m.dev_x:>4d},{m.dev_y:>4d}) → "
              f"norm({m.norm_x:>5.1f},{m.norm_y:>5.1f})  ±{m.tolerance_dev_px}px")

    # ── Step 3: Run VLM ──
    cfg = load_skill_config()
    print(f"\n  VLM: {cfg['model']} @ {cfg['base_url']}\n")

    all_results: list[list[TResult]] = []
    for run_i in range(1, args.runs + 1):
        print(f"  Run {run_i}/{args.runs}: sending marked screenshot to VLM...")

        t0 = time.time()
        raw, reason, nw, nh = _call_vlm(
            SYSTEM_PROMPT, build_user_prompt(),
            str(marked_path), max_tokens=2048,
        )
        elapsed = time.time() - t0
        print(f"  Response: {elapsed:.1f}s  finish={reason}  chars={len(raw or '')}")

        if not raw:
            print("  ❌ No response\n")
            continue

        preds = parse_response(raw, len(MARKERS))
        if preds is None:
            continue

        results = compute(preds)
        all_results.append(results)
        print_report(results, "MARKER COORDINATE TEST", run_i)

    # ── Step 4: Aggregate ──
    if args.runs > 1 and all_results:
        print(f"\n{'='*100}")
        print(f"  AGGREGATE ({len(all_results)} runs)")
        print(f"{'='*100}")
        for i, m in enumerate(MARKERS, 1):
            xs, ys = [], []
            for rr in all_results:
                for r in rr:
                    if r.marker_id == i and r.delta_norm < 999:
                        xs.append(abs(r.pred_x - r.gt_x))
                        ys.append(abs(r.pred_y - r.gt_y))
            if xs:
                ax, ay = sum(xs)/len(xs), sum(ys)/len(ys)
                print(f"  {m.name:<18s} |Δx|={ax:.1f} |Δy|={ay:.1f} "
                      f"→ ~{ax/1000*DEV_W:.0f}×{ay/1000*DEV_H:.0f} dev-px ({len(xs)} runs)")

    # Cleanup: keep marked version for inspection
    print(f"\n  Output: {marked_path}")


if __name__ == "__main__":
    main()