#!/usr/bin/env python3
"""End-to-end VLM coordinate accuracy test — full agent pipeline.

Pipeline (mirrors artemis_agent.py exactly):
  1. Generate test PNG at device resolution (3120×1440)
  2. Push to device → display fullscreen via image viewer
  3. ADB screenshot (adb exec-out screencap -p)
  4. _encode_image() resize (2000×923)
  5. _call_vlm() with same system prompt format
  6. Parse VLM coordinates → compare with ground truth

Usage:
    python3 vlm_coord_e2e.py [--runs N] [--device SERIAL] [--synthetic-only]
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artemis_agent import _encode_image, _call_vlm, _scale_coordinate, load_skill_config

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed.")
    sys.exit(1)

# ── Configuration ───────────────────────────────────────────────────────────
DEV_W = 3120
DEV_H = 1440
OUT_DIR = Path("/tmp/vlm_coord_e2e")
SCREENSHOT_NAME = "step_test.png"

# ── Element definitions (device-pixel centers) ──────────────────────────────

@dataclass
class Element:
    name: str
    description: str
    dev_x: int
    dev_y: int
    width: int
    height: int
    norm_x: float = field(init=False)
    norm_y: float = field(init=False)

    def __post_init__(self):
        self.norm_x = round(self.dev_x / DEV_W * 1000, 1)
        self.norm_y = round(self.dev_y / DEV_H * 1000, 1)


ELEMENTS = [
    Element("back_button", "the back arrow '←' button in top-left corner",
            100, 110, width=80, height=80),
    Element("search_icon", "the magnifier '🔍' icon in top-right corner",
            3020, 110, width=80, height=80),
    Element("text_input", "the wide text input/search bar below the header with placeholder text '请输入目的地'",
            1560, 230, width=2400, height=100),
    Element("left_card", "the left card/button labeled '驾车路线' showing '最快 35 分钟'",
            520, 550, width=600, height=300),
    Element("center_card", "the center card/button labeled '公交路线' showing '约 1 小时 20 分'",
            1560, 550, width=600, height=300),
    Element("right_card", "the right card/button labeled '步行路线' showing '约 2 小时 45 分'",
            2600, 550, width=600, height=300),
    Element("cta_button", "the large red '开始导航' call-to-action button in the lower center area",
            1560, 850, width=1800, height=200),
    Element("nav_home", "the bottom nav '首页' button (leftmost nav item with house icon)",
            520, 1380, width=500, height=100),
    Element("nav_route", "the bottom nav '路线' button (center nav item with pin icon)",
            1310, 1380, width=500, height=100),
    Element("nav_profile", "the bottom nav '我的' button (rightmost nav item with person icon)",
            2600, 1380, width=500, height=100),
]


# ── Font helpers ────────────────────────────────────────────────────────────

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}

def _get_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            _FONT_CACHE[size] = ImageFont.truetype(p, size)
            return _FONT_CACHE[size]
    _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def _text_center(draw, xy, text, font, fill="#e0e0e0"):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((xy[0] - tw // 2, xy[1] - th // 2), text, font=font, fill=fill)


def _rounded_box(draw, x1, y1, x2, y2, radius, fill):
    draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill)


# ── Generate test image (realistic UI at device resolution) ─────────────────

def build_test_image(path: Path) -> None:
    """Generate a realistic 3120×1440 mobile app screenshot with known elements."""
    img = Image.new("RGB", (DEV_W, DEV_H), "#1a1a2e")
    draw = ImageDraw.Draw(img)
    f24, f28, f34, f40, f44 = (_get_font(s) for s in (24, 28, 34, 40, 44))

    # Status bar
    draw.rectangle([0, 0, DEV_W, 60], fill="#0d0d1a")
    draw.text((40, 16), "10:30", font=f24, fill="#888")
    draw.text((DEV_W - 500, 16), "WiFi  ████  🔋 92%", font=f24, fill="#888")

    # Header bar
    draw.rectangle([0, 60, DEV_W, 160], fill="#16213e")
    _text_center(draw, (DEV_W // 2, 110), "地图导航", f40, fill="#e0e0e0")

    # Back button (center: 100, 110)
    e = ELEMENTS[0]
    _rounded_box(draw, e.dev_x - 40, e.dev_y - 40, e.dev_x + 40, e.dev_y + 40,
                 12, fill="#222244")
    _text_center(draw, (e.dev_x, e.dev_y), "←", f44, fill="#e0e0e0")

    # Search icon (center: 3020, 110)
    e = ELEMENTS[1]
    _text_center(draw, (e.dev_x, e.dev_y), "🔍", f40, fill="#e0e0e0")

    # Search input bar (center: 1560, 230)
    e = ELEMENTS[2]
    _rounded_box(draw, e.dev_x - 1190, e.dev_y - 49, e.dev_x + 1190, e.dev_y + 49,
                 25, fill="#2a2a4a")
    # border
    draw.rounded_rectangle(
        [e.dev_x - 1190, e.dev_y - 49, e.dev_x + 1190, e.dev_y + 49],
        radius=25, outline="#3a3a5a", width=2)
    _text_center(draw, (e.dev_x - 900, e.dev_y), "🔍", f34, fill="#888")
    draw.text((e.dev_x - 840, e.dev_y - 18), "请输入目的地", font=f34, fill="#666")

    # Map content area with visual texture
    draw.rectangle([0, 310, DEV_W, 1320], fill="#0d1525")
    # Grid lines
    for x in range(0, DEV_W, 180):
        draw.line([x, 310, x, 1320], fill="#111a2a", width=1)
    for y in range(310, 1320, 180):
        draw.line([0, y, DEV_W, y], fill="#111a2a", width=1)
    # Winding route line
    route_pts = [(300, 900), (800, 700), (1400, 800), (2000, 500), (2600, 600), (3000, 450)]
    for i in range(len(route_pts) - 1):
        draw.line([route_pts[i], route_pts[i + 1]], fill="#4a90d9", width=12)

    # Cards (y: 400-700)
    card_data = [
        (ELEMENTS[3], "#e94560", "驾车路线", "最快 35 分钟"),
        (ELEMENTS[4], "#533483", "公交路线", "约 1 小时 20 分"),
        (ELEMENTS[5], "#00c853", "步行路线", "约 2 小时 45 分"),
    ]
    for el, accent, title, sub in card_data:
        rx1, ry1 = el.dev_x - 298, el.dev_y - 148
        rx2, ry2 = el.dev_x + 298, el.dev_y + 148
        _rounded_box(draw, rx1, ry1, rx2, ry2, 22, fill="#0f3460")
        draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=22,
                               outline="#1a3a60", width=1)
        draw.rectangle([rx1, ry1, rx2, ry1 + 8], fill=accent)
        _text_center(draw, (el.dev_x, el.dev_y - 28), title, f34, fill="#e0e0e0")
        _text_center(draw, (el.dev_x, el.dev_y + 30), sub, f28, fill="#888")

    # CTA button (center: 1560, 850)
    e = ELEMENTS[6]
    _rounded_box(draw, e.dev_x - 898, e.dev_y - 98, e.dev_x + 898, e.dev_y + 98,
                 35, fill="#e94560")
    _text_center(draw, (e.dev_x, e.dev_y - 14), "开始导航", f44, fill="#fff")
    _text_center(draw, (e.dev_x, e.dev_y + 38), "全程约 35 公里 · 预计 42 分钟",
                 f28, fill="#ffcccc")

    # Bottom nav (y: 1320-1440)
    draw.rectangle([0, 1320, DEV_W, DEV_H], fill="#16213e")
    draw.line([0, 1320, DEV_W, 1320], fill="#1e2d50", width=2)

    nav_data = [
        (ELEMENTS[7], "🏠", "首页"),
        (ELEMENTS[8], "📍", "路线"),
        (ELEMENTS[9], "👤", "我的"),
    ]
    for el, icon, label in nav_data:
        _text_center(draw, (el.dev_x, el.dev_y - 16), icon, f40, fill="#888")
        _text_center(draw, (el.dev_x, el.dev_y + 26), label, f28, fill="#888")

    img.save(path, format="PNG")
    print(f"  Test image: {path} ({DEV_W}×{DEV_H})")

    # Print ground truth
    print("\n  Ground truth (device px → 0-1000 norm):")
    for el in ELEMENTS:
        print(f"    {el.name:<18s} dev({el.dev_x:>4d},{el.dev_y:>4d}) → "
              f"norm({el.norm_x:>3.0f},{el.norm_y:>3.0f}) {el.width}×{el.height}px")


# ── Screenshot pipeline ─────────────────────────────────────────────────────

def adb_run(args: list[str], timeout: int = 15) -> tuple[int, bytes, bytes]:
    """Run an ADB command."""
    r = subprocess.run(args, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def take_screenshot(device: str, dest: Path) -> bool:
    """Capture screenshot via ADB — same as agent's _take_screenshot."""
    adb_path = _find_adb()
    cmd = [adb_path, "-s", device, "exec-out", "screencap", "-p"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        if r.returncode == 0 and r.stdout:
            dest.write_bytes(r.stdout)
            return True
    except Exception as e:
        print(f"  Screenshot failed: {e}")
    return False


def display_image_on_device(device: str, remote_path: str) -> bool:
    """Push PNG to device and open it fullscreen in the gallery."""
    adb_path = _find_adb()

    # Push to device
    local = OUT_DIR / "test_ui_source.png"
    rc, _, err = adb_run([adb_path, "-s", device, "push", str(local), remote_path])
    if rc != 0:
        print(f"  Push failed: {err.decode()}")
        return False

    # Open the image — try gallery app, fall back to intent
    for intent in [
        # Try Directly open file (lets Android pick viewer)
        f'am start -a android.intent.action.VIEW -d "file://{remote_path}" -t image/png',
        # Alternate: force fullscreen via immersive mode
        f'am start -a android.intent.action.VIEW -d "file://{remote_path}" -t image/png --ez android.intent.extra.FULLSCREEN true',
    ]:
        rc, _, err = adb_run([adb_path, "-s", device, "shell"] + intent.split())
        if rc == 0:
            break
    else:
        print(f"  Failed to open image on device")
        return False

    # Wait for render
    time.sleep(2)

    # Try to dismiss any UI chrome by tapping center then waiting
    adb_run([adb_path, "-s", device, "shell", "input", "tap", str(DEV_W // 2), str(DEV_H // 2)])
    time.sleep(1)

    return True


def _find_adb() -> str:
    """Find the ADB binary path."""
    # Check common locations
    for p in ["adb", "/usr/bin/adb", "/usr/local/bin/adb",
              os.path.expanduser("~/Android/Sdk/platform-tools/adb")]:
        rc, _, _ = subprocess.run(["which", p] if "/" not in p else ["test", "-x", p],
                                  capture_output=True)
        if rc == 0:
            return p if "/" in p else subprocess.run(["which", p], capture_output=True, text=True).stdout.strip()
    return "adb"


# ── VLM calling ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a precision mobile UI element coordinate locator.

    COORDINATE SPACE: 1000×1000. Output ALL coordinates in 0-1000 range.
    They will be auto-scaled to device pixels.
    (0,0) is top-left corner, (1000,1000) is bottom-right corner.

    Output ONLY valid JSON — no markdown, no explanation, no code fences.
    Format:
    {"elements": [{"name": "<name>", "x": <int>, "y": <int>}, ...]}

    Each (x, y) must be the CENTER of the named element, as an integer 0-1000.
    """)


def build_user_prompt() -> str:
    lines = [
        "Locate the exact CENTER coordinates (in 0-1000 space) of each element:",
        "",
    ]
    for i, el in enumerate(ELEMENTS, 1):
        lines.append(f"  {i}. \"{el.name}\" — {el.description}")
    lines.append("")
    lines.append('Output: {"elements": [{"name": "...", "x": <int>, "y": <int>}, ...]}')
    return "\n".join(lines)


def parse_response(raw: str, names: set[str]) -> dict[str, tuple[float, float]] | None:
    if not raw:
        return None
    text = raw.strip()

    # Strip markdown fences if present
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # Find JSON object
    m = re.search(r'\{.*"elements"\s*:\s*\[.*\]\s*\}', text, re.DOTALL)
    if m:
        text = m.group(0)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try truncating from the right until valid
        for i in range(len(text), 0, -1):
            try:
                data = json.loads(text[:i])
                break
            except json.JSONDecodeError:
                continue
        else:
            print(f"  ⚠ JSON parse FAILED. Raw: {raw[:400]}")
            return None

    result = {}
    for item in data.get("elements", []):
        name = item.get("name", "")
        if name in names:
            try:
                result[name] = (float(item["x"]), float(item["y"]))
            except (KeyError, TypeError, ValueError):
                pass

    missing = names - set(result.keys())
    if missing:
        print(f"  ⚠ VLM missed: {sorted(missing)}")
    return result


# ── Results ─────────────────────────────────────────────────────────────────

@dataclass
class EResult:
    name: str
    pred_x: float; pred_y: float
    gt_x: float; gt_y: float
    delta_norm: float = 0.0
    delta_dev: float = 0.0
    hit: bool = False


def compute(predictions: dict[str, tuple[float, float]]) -> list[EResult]:
    results = []
    for el in ELEMENTS:
        p = predictions.get(el.name)
        if p is None:
            results.append(EResult(el.name, 0, 0, el.norm_x, el.norm_y, 999, 99999, False))
            continue
        px, py = p
        dx, dy = px - el.norm_x, py - el.norm_y
        dn = math.sqrt(dx * dx + dy * dy)
        ddx = dx / 1000 * DEV_W
        ddy = dy / 1000 * DEV_H
        dd = math.sqrt(ddx * ddx + ddy * ddy)
        hit = abs(ddx) <= el.width / 2 and abs(ddy) <= el.height / 2
        results.append(EResult(el.name, px, py, el.norm_x, el.norm_y, dn, dd, hit))
    return results


def print_report(results: list[EResult], label: str, run_idx: int = 1):
    print(f"\n{'='*95}")
    print(f"  VLM Coordinate Accuracy: {label} (Run {run_idx})")
    print(f"{'='*95}")
    header = (f"  {'Element':<18s} {'Δxy':>6s} {'Δx':>7s} {'Δy':>7s} "
              f"{'Δdev':>7s} {'Hit':>5s}  {'Pred':>18s}  {'GT':>18s}")
    print(header)
    print("  " + "─" * 91)

    hit_count, total_delta = 0, 0.0
    valid = [r for r in results if r.delta_norm < 999]
    for r in results:
        hit_str = "✅" if r.hit else "❌"
        if r.hit: hit_count += 1
        if r.delta_norm < 999: total_delta += r.delta_norm
        print(f"  {r.name:<18s} {r.delta_norm:>5.1f}  {r.pred_x - r.gt_x:>+6.1f}  "
              f"{r.pred_y - r.gt_y:>+6.1f}  {r.delta_dev:>6.0f}px  {hit_str:>4s}  "
              f"({r.pred_x:>5.0f},{r.pred_y:>5.0f})        ({r.gt_x:>5.0f},{r.gt_y:>5.0f})")

    n = len(valid)
    avg = total_delta / n if n > 0 else 999
    rate = hit_count / len(results) * 100 if results else 0
    print("  " + "─" * 91)
    print(f"  {'SUMMARY':18s} {avg:>5.1f}  {'':>6s}  {'':>6s}  "
          f"{'':>6s}  {hit_count}/{len(results)}  "
          f"Hit={rate:.0f}%  AvgΔ={avg:.1f}")

    # By category
    cats = [
        ("Small edge (back,search)", ["back_button", "search_icon"]),
        ("Thin bar (text_input)",    ["text_input"]),
        ("Medium cards",             ["left_card", "center_card", "right_card"]),
        ("Large CTA",                ["cta_button"]),
        ("Bottom nav",               ["nav_home", "nav_route", "nav_profile"]),
    ]
    print("\n  By category:")
    for cat_name, names in cats:
        items = [r for r in valid if r.name in names]
        if items:
            a = sum(r.delta_norm for r in items) / len(items)
            ad = sum(r.delta_dev for r in items) / len(items)
            h = sum(1 for r in items if r.hit)
            print(f"    {cat_name:<26s} Δ={a:4.1f}  Δdev={ad:5.0f}px  hit={h}/{len(items)}")
    print()


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="E2E VLM coordinate test")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--device", type=str, default="a60f6655",
                    help="ADB device serial")
    ap.add_argument("--synthetic-only", action="store_true",
                    help="Skip device display — test via direct PIL image only")
    ap.add_argument("--remote-path", type=str,
                    default="/sdcard/test_ui_coord.png")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Generate test image ──
    pil_path = OUT_DIR / SCREENSHOT_NAME
    build_test_image(pil_path)

    # ── Step 2: Display on device + screenshot (unless synthetic-only) ──
    screenshot_path = pil_path  # default: use PIL image directly

    if not args.synthetic_only:
        src_path = OUT_DIR / "test_ui_source.png"
        import shutil
        shutil.copy(pil_path, src_path)

        print(f"\n  Displaying on device {args.device}...")
        if not display_image_on_device(args.device, args.remote_path):
            print("  ⚠ Device display failed — falling back to synthetic")
        else:
            ss_path = OUT_DIR / "device_screenshot.png"
            print(f"  Taking screenshot...")
            if take_screenshot(args.device, ss_path):
                print(f"  Device screenshot: {ss_path}")
                screenshot_path = ss_path
            else:
                print("  ⚠ Screenshot failed — falling back to synthetic")

    # ── Step 3: Print what the VLM will see ──
    print(f"\n  Test pipeline:")
    from artemis_agent import NORM_MAX_DIM
    print(f"    Input:  {screenshot_path}")
    print(f"    Device: {DEV_W}×{DEV_H}  NORM_MAX_DIM={NORM_MAX_DIM}")

    # Show what encode does
    b64, nw, nh = _encode_image(str(screenshot_path))
    print(f"    Encoded: base64 {len(b64)} chars, norm=({nw},{nh})")

    # ── Step 4: Run VLM tests ──
    cfg = load_skill_config()
    print(f"    VLM:    {cfg['model']} @ {cfg['base_url']}\n")

    all_results: list[list[EResult]] = []
    for run_i in range(1, args.runs + 1):
        tag = "DEVICE SCREENSHOT" if screenshot_path != pil_path else "DIRECT PIL"
        print(f"  Run {run_i}/{args.runs}: {tag} → VLM...")

        t0 = time.time()
        raw, reason, nw, nh = _call_vlm(
            SYSTEM_PROMPT, build_user_prompt(),
            str(screenshot_path), max_tokens=2048,
        )
        elapsed = time.time() - t0
        print(f"  Response: {elapsed:.1f}s  finish={reason}  chars={len(raw or '')}")

        if not raw:
            print("  ❌ No response\n")
            continue

        preds = parse_response(raw, {e.name for e in ELEMENTS})
        if preds is None:
            continue

        results = compute(preds)
        all_results.append(results)
        print_report(results, tag, run_i)

    # ── Aggregate ──
    if args.runs > 1 and all_results:
        print(f"\n{'='*95}")
        print(f"  AGGREGATE ({len(all_results)} runs)")
        print(f"{'='*95}")
        for el in ELEMENTS:
            xs, ys = [], []
            for rr in all_results:
                for r in rr:
                    if r.name == el.name and r.delta_norm < 999:
                        xs.append(abs(r.pred_x - r.gt_x))
                        ys.append(abs(r.pred_y - r.gt_y))
            if xs:
                ax, ay = sum(xs) / len(xs), sum(ys) / len(ys)
                axy = math.sqrt(ax**2 + ay**2)
                print(f"  {el.name:<18s} avg|Δx|={ax:.1f} |Δy|={ay:.1f} Δxy={axy:.1f} "
                      f"~{axy/1000*DEV_W:.0f}×{axy/1000*DEV_H:.0f} dev-px ({len(xs)} runs)")


if __name__ == "__main__":
    main()