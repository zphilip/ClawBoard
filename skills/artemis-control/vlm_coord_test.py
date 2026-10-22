#!/usr/bin/env python3
"""VLM coordinate accuracy test harness.

Generates a synthetic mobile UI image with known ground-truth element
positions, sends it through the exact same _encode_image + _call_vlm
pipeline used by the agent, and measures coordinate prediction accuracy.

Usage:
    python3 vlm_coord_test.py [--runs N] [--no-image]

Output columns:
    - Δxy: Euclidean distance in 0-1000 normalized space
    - Δx, Δy: Per-axis error in 0-1000 space
    - Δdev: Equivalent device-pixel error (3120×1440)
    - Hit: Whether the tap would hit the element (Δdev < half element dimension)
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

# Import the agent's VLM pipeline
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artemis_agent import _encode_image, _call_vlm, _scale_coordinate, load_skill_config

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed.  pip install Pillow")
    sys.exit(1)

# ── Test image configuration ────────────────────────────────────────────────

# Match the actual test device (landscape 3120×1440)
DEV_W = 3120
DEV_H = 1440

# Colors (dark-mode-ish for realism)
BG_COLOR = "#1a1a2e"
HEADER_BG = "#16213e"
CARD_BG = "#0f3460"
ACCENT = "#e94560"
ACCENT2 = "#533483"
WHITE = "#e0e0e0"
GRAY = "#888888"
GREEN = "#00c853"
ORANGE = "#ff9100"
STATUS_BAR_BG = "#0d0d1a"
INPUT_BG = "#2a2a4a"
NAV_BG = "#16213e"


@dataclass
class Element:
    """A UI element with known ground-truth position."""
    name: str
    description: str                           # what the VLM is asked to find
    dev_x: int                                 # center X in device pixels
    dev_y: int                                 # center Y in device pixels
    width: int                                 # element width in device pixels
    height: int                                # element height in device pixels
    # Derived: center in 0-1000 normalized space
    norm_x: float = field(init=False)
    norm_y: float = field(init=False)

    def __post_init__(self):
        self.norm_x = round(self.dev_x / DEV_W * 1000, 1)
        self.norm_y = round(self.dev_y / DEV_H * 1000, 1)


# ── Test element definitions ───────────────────────────────────────────────

ELEMENTS = [
    Element("back_button", "the back arrow/button in the top-left corner",
            100, 100, width=80, height=80),
    Element("search_icon", "the search magnifier icon in the top-right corner",
            3020, 100, width=80, height=80),
    Element("text_input", "the wide text input/search bar in the header area",
            1560, 230, width=2400, height=80),
    Element("left_card", "the card/button on the left side in the content area",
            520, 550, width=600, height=300),
    Element("center_card", "the card/button in the center of the content area",
            1560, 550, width=600, height=300),
    Element("right_card", "the card/button on the right side in the content area",
            2600, 550, width=600, height=300),
    Element("cta_button", "the large call-to-action button in the lower center",
            1560, 850, width=1800, height=200),
    Element("nav_home", "the bottom navigation bar Home/首页 button (leftmost nav item)",
            312, 1390, width=500, height=100),
    Element("nav_route", "the bottom navigation bar Route/路线 button (center nav item)",
            936, 1390, width=500, height=100),
    Element("nav_profile", "the bottom navigation bar Profile/我的 button (right nav item)",
            1560, 1390, width=500, height=100),
]


# ── Image generation ────────────────────────────────────────────────────────

def _try_font(size: int):
    """Find a CJK-capable font, falling back to default."""
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
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _text_center(draw, xy, text, font, fill=WHITE):
    """Draw text centered at xy."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((xy[0] - tw // 2, xy[1] - th // 2), text, font=font, fill=fill)


def _rounded_box(draw, x1, y1, x2, y2, radius, fill):
    """Draw a filled rounded rectangle."""
    draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill)


def generate_test_image(path: str) -> dict[str, tuple[int, int]]:
    """Create a synthetic 3120×1440 mobile UI screenshot.

    Returns ground-truth (dev_x, dev_y) for each named element.
    """
    img = Image.new("RGB", (DEV_W, DEV_H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    font_title = _try_font(36)
    font_body = _try_font(28)
    font_small = _try_font(24)
    font_btn = _try_font(32)
    font_large = _try_font(42)

    # ── Status bar (y: 0–60) ──
    draw.rectangle([0, 0, DEV_W, 60], fill=STATUS_BAR_BG)
    draw.text((30, 14), "10:30", font=font_small, fill=GRAY)
    draw.text((DEV_W - 450, 14), "WiFi  ████  🔋 92%", font=font_small, fill=GRAY)

    # ── Header bar (y: 60–160) ──
    draw.rectangle([0, 60, DEV_W, 160], fill=HEADER_BG)
    # Back arrow
    arrow_font = _try_font(48)
    _text_center(draw, (ELEMENTS[0].dev_x, ELEMENTS[0].dev_y),
                 "←", font=arrow_font, fill=WHITE)
    # Title
    _text_center(draw, (DEV_W // 2, 110), "地图导航", font=font_title, fill=WHITE)
    # Search icon
    _text_center(draw, (ELEMENTS[1].dev_x, ELEMENTS[1].dev_y),
                 "🔍", font=font_title, fill=WHITE)

    # ── Search/text input bar (y: 170–290) ──
    inp = ELEMENTS[2]
    _rounded_box(draw, inp.dev_x - 1200, 170, inp.dev_x + 1200, 290, radius=25,
                 fill=INPUT_BG)
    draw.text((inp.dev_x - 380, 200), "请输入目的地", font=font_body, fill=GRAY)

    # ── Card row (y: 400–700) ──
    for idx, el in enumerate(ELEMENTS[3:6]):
        rx1, ry1 = el.dev_x - 300, el.dev_y - 150
        rx2, ry2 = el.dev_x + 300, el.dev_y + 150
        _rounded_box(draw, rx1, ry1, rx2, ry2, radius=20, fill=CARD_BG)
        # Colored accent bar at top of each card
        colors = [ACCENT, ACCENT2, GREEN]
        draw.rectangle([rx1, ry1, rx2, ry1 + 8], fill=colors[idx])
        labels = ["驾车路线", "公交路线", "步行路线"]
        _text_center(draw, (el.dev_x, el.dev_y - 30), labels[idx],
                     font=font_btn, fill=WHITE)
        subtitles = ["最快 35分钟", "约 1小时20分", "约 2小时45分"]
        _text_center(draw, (el.dev_x, el.dev_y + 30), subtitles[idx],
                     font=font_small, fill=GRAY)

    # ── CTA button (y: 750–950) ──
    cta = ELEMENTS[6]
    _rounded_box(draw, cta.dev_x - 900, 750, cta.dev_x + 900, 950,
                 radius=30, fill=ACCENT)
    _text_center(draw, (cta.dev_x, cta.dev_y), "开始导航",
                 font=font_large, fill=WHITE)
    # Subtitle
    _text_center(draw, (cta.dev_x, cta.dev_y + 45), "全程约 35 公里  ·  预计 42 分钟",
                 font=font_small, fill="#ffcccc")

    # ── Bottom navigation bar (y: 1340–1440) ──
    draw.rectangle([0, 1330, DEV_W, DEV_H], fill=NAV_BG)
    # Divider line
    draw.line([0, 1330, DEV_W, 1330], fill="#2a3a5e", width=2)

    nav_items = [
        (ELEMENTS[7], "🏠", "首页"),
        (ELEMENTS[8], "📍", "路线"),
        (ELEMENTS[9], "👤", "我的"),
    ]
    for el, icon, label in nav_items:
        _text_center(draw, (el.dev_x, el.dev_y - 15), icon,
                     font=font_title, fill=GRAY)
        _text_center(draw, (el.dev_x, el.dev_y + 28), label,
                     font=font_small, fill=GRAY)

    img.save(path, format="PNG")
    print(f"  Test image saved: {path}  ({DEV_W}×{DEV_H})")

    # Return ground truth
    return {e.name: (e.dev_x, e.dev_y) for e in ELEMENTS}


# ── VLM prompting ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a precision coordinate locator for mobile UI elements.
    You are given a screenshot of a mobile app at 3120×1440 pixels.

    COORDINATE SPACE: You must output ALL coordinates in 0-1000 normalized space.
    (0,0) is top-left, (1000,1000) is bottom-right.
    Coordinates will be auto-scaled to device pixels.

    Output ONLY valid JSON in the exact format below. No markdown, no explanation.
    Each coordinate must be an integer 0-1000 representing the CENTER of the element.

    {
      "elements": [
        {"name": "<element_name>", "x": <int>, "y": <int>},
        ...
      ]
    }
    """)


def build_user_prompt(elements: list[Element]) -> str:
    """Build the user prompt asking the VLM to locate specific elements."""
    lines = [
        "Locate the CENTER coordinates of each UI element listed below.",
        "For each element, estimate the pixel center in the 0-1000 normalized space.",
        "",
        "Elements to locate:",
    ]
    for i, el in enumerate(elements, 1):
        lines.append(f"  {i}. name=\"{el.name}\" — {el.description}")
    lines.append("")
    lines.append("Output as JSON:")
    lines.append('{"elements": [{"name": "...", "x": <int>, "y": <int>}, ...]}')
    return "\n".join(lines)


def parse_vlm_response(raw: str, element_names: set[str]) -> dict[str, tuple[float, float]] | None:
    """Extract named coordinates from VLM JSON response.

    Returns {name: (x, y)} on success, None on parse failure.
    """
    if not raw:
        return None

    # Extract JSON block — handle markdown fences, raw JSON, etc.
    json_str = raw.strip()
    # Try markdown ```json ... ``` fences first
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, re.DOTALL)
    if m:
        json_str = m.group(1).strip()
    # Try bare { ... }
    m2 = re.search(r'\{.*"elements"\s*:\s*\[.*\]\s*\}', json_str, re.DOTALL)
    if m2:
        json_str = m2.group(0)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Attempt repair: trim trailing junk
        for closer in ['}]', '}"]', '"}']:
            for i in range(len(json_str), 0, -1):
                trial = json_str[:i]
                try:
                    data = json.loads(trial)
                    break
                except json.JSONDecodeError:
                    continue
            else:
                continue
            break
        else:
            print(f"  ⚠ JSON parse failed. Raw (first 300 chars): {raw[:300]}")
            return None

    result: dict[str, tuple[float, float]] = {}
    for item in data.get("elements", []):
        name = item.get("name", "")
        if name not in element_names:
            continue
        try:
            x = float(item["x"])
            y = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        result[name] = (x, y)

    missing = element_names - set(result.keys())
    if missing:
        print(f"  ⚠ VLM missed {len(missing)} elements: {sorted(missing)}")

    return result


# ── Metrics ─────────────────────────────────────────────────────────────────

@dataclass
class ElementResult:
    name: str
    pred_x: float
    pred_y: float
    gt_x: float
    gt_y: float
    # Derived
    delta_norm: float = 0.0      # Euclidean distance in 0-1000 space
    delta_dev: float = 0.0       # Equivalent device-pixel error
    hit: bool = False            # Tap would hit the element


def compute_results(
    predictions: dict[str, tuple[float, float]],
    elements: list[Element],
) -> list[ElementResult]:
    """Compare predictions against ground truth."""
    results: list[ElementResult] = []
    for el in elements:
        pred = predictions.get(el.name)
        if pred is None:
            # Mark as missed — use large error
            results.append(ElementResult(
                el.name, 0, 0, el.norm_x, el.norm_y,
                delta_norm=999, delta_dev=999999, hit=False,
            ))
            continue

        pred_x, pred_y = pred
        dx = pred_x - el.norm_x
        dy = pred_y - el.norm_y
        delta_norm = math.sqrt(dx * dx + dy * dy)

        # Convert to device pixels
        dev_dx = dx / 1000 * DEV_W
        dev_dy = dy / 1000 * DEV_H
        delta_dev = math.sqrt(dev_dx * dev_dx + dev_dy * dev_dy)

        # Hit = predicted point inside the element bounds (± half width/height)
        half_w = el.width / 2
        half_h = el.height / 2
        hit = abs(dev_dx) <= half_w and abs(dev_dy) <= half_h

        results.append(ElementResult(
            el.name, pred_x, pred_y, el.norm_x, el.norm_y,
            delta_norm=delta_norm, delta_dev=delta_dev, hit=hit,
        ))

    return results


def print_report(results: list[ElementResult], run_idx: int = 1):
    """Print formatted results table."""
    print(f"\n{'='*90}")
    print(f"  VLM Coordinate Accuracy Report (Run {run_idx})")
    print(f"{'='*90}")
    print(f"  {'Element':<20s} {'Δxy':>7s} {'Δx':>7s} {'Δy':>7s} "
          f"{'Δdev':>8s} {'Hit':>6s}  {'Pred':>16s}  {'GT':>16s}")
    print(f"  {'─'*18}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*4}  "
          f"{'─'*14}  {'─'*14}")

    hit_count = 0
    total_delta = 0.0
    for r in results:
        hit_str = "✅" if r.hit else "❌"
        if r.hit:
            hit_count += 1
        if r.delta_norm < 999:
            total_delta += r.delta_norm
        pred_str = f"({r.pred_x:.0f}, {r.pred_y:.0f})"
        gt_str = f"({r.gt_x:.0f}, {r.gt_y:.0f})"
        print(f"  {r.name:<20s} {r.delta_norm:>5.1f}  {r.pred_x - r.gt_x:>+5.1f}  "
              f"{r.pred_y - r.gt_y:>+5.1f}  {r.delta_dev:>6.0f}px  {hit_str:>4s}  "
              f"{pred_str:>16s}  {gt_str:>16s}")

    valid = [r for r in results if r.delta_norm < 999]
    n = len(valid)
    avg_delta = total_delta / n if n > 0 else 999
    hit_rate = hit_count / len(results) * 100 if results else 0

    print(f"  {'─'*18}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*4}")
    print(f"  {'SUMMARY':20s} {avg_delta:>5.1f}  {'':>5s}  {'':>5s}  "
          f"{'':>6s}  {hit_count}/{len(results)}  "
          f"Hit rate: {hit_rate:.0f}%  Avg error: {avg_delta:.1f} norm units")
    print()

    # Categorize
    small_edge = [r for r in valid if r.name in ("back_button", "search_icon")]
    thin_bar = [r for r in valid if r.name == "text_input"]
    medium = [r for r in valid if r.name in ("left_card", "center_card", "right_card")]
    large = [r for r in valid if r.name == "cta_button"]
    bottom = [r for r in valid if r.name.startswith("nav_")]

    def _avg(items):
        return sum(x.delta_norm for x in items) / len(items) if items else 0
    def _avg_dev(items):
        return sum(x.delta_dev for x in items) / len(items) if items else 0

    print(f"  By element type:")
    print(f"    Small edge elements:   avg Δ={_avg(small_edge):.1f}  (Δdev={_avg_dev(small_edge):.0f}px)  hit={sum(1 for r in small_edge if r.hit)}/{len(small_edge)}")
    print(f"    Thin horizontal bar:   avg Δ={_avg(thin_bar):.1f}  (Δdev={_avg_dev(thin_bar):.0f}px)  hit={sum(1 for r in thin_bar if r.hit)}/{len(thin_bar)}")
    print(f"    Medium cards:          avg Δ={_avg(medium):.1f}  (Δdev={_avg_dev(medium):.0f}px)  hit={sum(1 for r in medium if r.hit)}/{len(medium)}")
    print(f"    Large CTA button:      avg Δ={_avg(large):.1f}  (Δdev={_avg_dev(large):.0f}px)  hit={sum(1 for r in large if r.hit)}/{len(large)}")
    print(f"    Bottom nav items:      avg Δ={_avg(bottom):.1f}  (Δdev={_avg_dev(bottom):.0f}px)  hit={sum(1 for r in bottom if r.hit)}/{len(bottom)}")
    print()


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="VLM coordinate accuracy test")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of test runs (for consistency check)")
    parser.add_argument("--no-image", action="store_true",
                        help="Don't save the test image")
    parser.add_argument("--out-dir", type=str, default="/tmp/vlm_coord_test",
                        help="Output directory for test images")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    # ── Generate test image ──
    img_path = os.path.join(args.out_dir, "test_ui.png")
    ground_truth = generate_test_image(img_path)
    element_names = {e.name for e in ELEMENTS}

    if args.no_image and os.path.exists(img_path):
        os.remove(img_path)

    # Print ground truth
    print("\n  Ground truth (device px → 0-1000 norm):")
    for el in ELEMENTS:
        print(f"    {el.name:<20s}  dev({el.dev_x:>4d}, {el.dev_y:>4d})  "
              f"→ norm({el.norm_x:>3.0f}, {el.norm_y:>3.0f})  "
              f"{el.width}×{el.height}px")

    # ── Check VLM config ──
    cfg = load_skill_config()
    print(f"\n  VLM: model={cfg['model']}  base_url={cfg['base_url']}")
    print(f"  Image encoding: NORM_MAX_DIM imported from agent")

    # ── Run tests ──
    all_results: list[list[ElementResult]] = []

    for run_idx in range(1, args.runs + 1):
        print(f"\n{'─'*90}")
        print(f"  Run {run_idx}/{args.runs}: sending image to VLM...")

        user_prompt = build_user_prompt(ELEMENTS)
        t0 = time.time()

        raw, reason, norm_w, norm_h = _call_vlm(
            SYSTEM_PROMPT, user_prompt, img_path, max_tokens=2048,
        )

        elapsed = time.time() - t0
        print(f"  VLM response in {elapsed:.1f}s  finish={reason}  "
              f"norm=({norm_w},{norm_h})  len={len(raw or '')} chars")

        if not raw:
            print("  ❌ VLM returned no response")
            continue

        predictions = parse_vlm_response(raw, element_names)
        if predictions is None:
            print("  ❌ Failed to parse VLM response")
            print(f"  Raw response:\n{raw[:500]}")
            continue

        results = compute_results(predictions, ELEMENTS)
        all_results.append(results)
        print_report(results, run_idx)

    # ── Aggregate summary (multi-run) ──
    if args.runs > 1 and all_results:
        print(f"\n{'='*90}")
        print(f"  AGGREGATE OVER {len(all_results)} RUNS")
        print(f"{'='*90}")

        for el in ELEMENTS:
            x_errs = []
            y_errs = []
            for run_results in all_results:
                for r in run_results:
                    if r.name == el.name and r.delta_norm < 999:
                        x_errs.append(abs(r.pred_x - r.gt_x))
                        y_errs.append(abs(r.pred_y - r.gt_y))
            if x_errs:
                avg_x = sum(x_errs) / len(x_errs)
                avg_y = sum(y_errs) / len(y_errs)
                avg_xy = math.sqrt(avg_x**2 + avg_y**2)
                dev_x = avg_x / 1000 * DEV_W
                dev_y = avg_y / 1000 * DEV_H
                dev_xy = math.sqrt(dev_x**2 + dev_y**2)
                print(f"  {el.name:<20s}  avg |Δx|={avg_x:.1f} |Δy|={avg_y:.1f}  "
                      f"Δxy={avg_xy:.1f}  ~{dev_xy:.0f} device-px  "
                      f"(N={len(x_errs)} runs)")


if __name__ == "__main__":
    main()