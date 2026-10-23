#!/usr/bin/env python3
"""End-to-end VLM coordinate accuracy test — fake UI + full agent pipeline.

Pipeline (mirrors artemis_agent.py exactly):
  OPTION A — synthetic direct:
    PIL image (device resolution) → _encode_image() → _call_vlm() → compare

  OPTION B — device roundtrip:
    PIL image → adb push → display on device → screencap -p → _encode_image() → compare

UI is a realistic-looking mobile app mockup with English text (no CJK fonts
needed). Element positions are expressed as screen fractions so the layout
adapts to any resolution and orientation (portrait / landscape).

Usage:
    python3 vlm_coord_e2e.py [--runs N] [--device SERIAL]
    python3 vlm_coord_e2e.py --synthetic-only --runs 2
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


# ── Fonts ──────────────────────────────────────────────────────────────────

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}

def _get_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]:
        if os.path.exists(p):
            _FONT_CACHE[size] = ImageFont.truetype(p, size)
            return _FONT_CACHE[size]
    _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def _tc(draw, xy, text, font, fill="#e0e0e0"):
    """Draw text centered at (x, y)."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((xy[0] - tw // 2, xy[1] - th // 2), text, font=font, fill=fill)


def _rbox(draw, x1, y1, x2, y2, r, fill, outline=None):
    draw.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=fill, outline=outline)


# ── Resolutions ─────────────────────────────────────────────────────────────

# Default device resolution (overridden by --device screenshot or --resolution)
DEV_W, DEV_H = 1440, 3120   # portrait default


# ── Element definitions (fractional, adapts to any resolution) ─────────────

@dataclass
class UIElement:
    """UI element with fractional position, pixel center computed at build time."""
    name: str
    description: str                  # what VLM is asked to find
    x_frac: float                     # 0..1, center X as fraction of screen width
    y_frac: float                     # 0..1, center Y as fraction of screen height
    w_frac: float                     # 0..1, width as fraction of screen width
    h_frac: float                     # 0..1, height as fraction of screen height
    # Computed after resolution is known:
    dev_x: int = 0
    dev_y: int = 0
    dev_w: int = 0
    dev_h: int = 0
    norm_x: float = 0.0
    norm_y: float = 0.0

    def compute(self, w: int, h: int):
        self.dev_x = round(self.x_frac * w)
        self.dev_y = round(self.y_frac * h)
        self.dev_w = round(self.w_frac * w)
        self.dev_h = round(self.h_frac * h)
        self.norm_x = round(self.dev_x / w * 1000, 1)
        self.norm_y = round(self.dev_y / h * 1000, 1)

    def bbox(self):
        """Return (x1, y1, x2, y2) for drawing."""
        hw, hh = self.dev_w // 2, self.dev_h // 2
        return (self.dev_x - hw, self.dev_y - hh,
                self.dev_x + hw, self.dev_y + hh)

    @property
    def tolerance_dev_px(self) -> float:
        """Half the element dimension — tap must be within this to hit."""
        return min(self.dev_w, self.dev_h) / 2


# Element definitions as screen fractions
ELEMENT_SPECS = [
    # (name, description, x_frac, y_frac, w_frac, h_frac)
    # All positions kept within top 88% of image — bottom ~12% is padding
    # that gets cropped by Gallery viewer chrome during device roundtrip.
    ("back_btn",     "the back arrow '<' button in the top-left corner",
     0.05, 0.07, 0.06, 0.06),
    ("search_icon",  "the magnifier icon in the top-right corner of the header",
     0.95, 0.07, 0.05, 0.05),
    ("text_input",   "the wide text input/search bar below the header with placeholder text",
     0.50, 0.15, 0.75, 0.07),
    ("left_card",    "the card on the left side titled 'Driving' with a red accent bar",
     0.17, 0.38, 0.20, 0.18),
    ("center_card",  "the card in the center titled 'Transit' with a purple accent bar",
     0.50, 0.38, 0.20, 0.18),
    ("right_card",   "the card on the right side titled 'Walking' with a green accent bar",
     0.83, 0.38, 0.20, 0.18),
    ("cta_button",   "the large red 'Start Navigation' button in the lower-center area",
     0.50, 0.58, 0.60, 0.12),
    ("nav_home",     "the bottom navigation 'Home' button (leftmost, house icon)",
     0.12, 0.87, 0.14, 0.06),
    ("nav_route",    "the bottom navigation 'Route' button (second from left, pin icon)",
     0.38, 0.87, 0.14, 0.06),
    ("nav_profile",  "the bottom navigation 'Profile' button (third from left, person icon)",
     0.64, 0.87, 0.14, 0.06),
]


def build_elements(w: int, h: int) -> list[UIElement]:
    els = []
    for spec in ELEMENT_SPECS:
        e = UIElement(*spec)
        e.compute(w, h)
        els.append(e)
    return els


# ── Image generation ────────────────────────────────────────────────────────

def generate_test_image(path: Path, elements: list[UIElement],
                        w: int, h: int) -> None:
    """Draw a realistic-looking mobile app UI at device resolution."""

    img = Image.new("RGB", (w, h), "#1a1a2e")
    draw = ImageDraw.Draw(img)
    sf = min(w, h)  # short-side scale for font sizes

    # Font sizes scale with short edge
    def fs(ratio): return max(12, round(sf * ratio))
    f_status = _get_font(fs(0.022))   # ~32px on 1440
    f_title  = _get_font(fs(0.028))   # ~40px
    f_body   = _get_font(fs(0.024))   # ~35px
    f_small  = _get_font(fs(0.019))   # ~27px
    f_btn    = _get_font(fs(0.030))   # ~43px
    f_large  = _get_font(fs(0.033))   # ~48px

    # Colors
    BG       = "#1a1a2e"
    STAT_BG  = "#0d0d1a"
    HEAD_BG  = "#16213e"
    INPUT_BG = "#2a2a4a"
    CARD_BG  = "#0f3460"
    CARD_OL  = "#1a3a60"
    ACCENT   = "#e94560"
    ACCENT2  = "#533483"
    ACCENT3  = "#00c853"
    WHITE    = "#e0e0e0"
    GRAY     = "#888888"
    NAV_BG   = "#16213e"
    NAV_LINE = "#1e2d50"

    # Status bar (top 4% of height)
    sb_h = round(h * 0.04)
    draw.rectangle([0, 0, w, sb_h], fill=STAT_BG)
    _tc(draw, (round(w * 0.06), sb_h // 2), "10:30", f_small, fill=GRAY)
    _tc(draw, (round(w * 0.85), sb_h // 2),
        "WiFi  ----  [] 92%", f_small, fill=GRAY)

    # Header bar (4-11% of height)
    hb_y1, hb_y2 = sb_h, round(h * 0.11)
    draw.rectangle([0, hb_y1, w, hb_y2], fill=HEAD_BG)

    # Title
    _tc(draw, (w // 2, (hb_y1 + hb_y2) // 2), "Navigation", f_title, fill=WHITE)

    # Back button
    e = elements[0]
    bx1, by1, bx2, by2 = e.bbox()
    _rbox(draw, bx1, by1, bx2, by2, round(sf * 0.009), fill="#222244")
    _tc(draw, (e.dev_x, e.dev_y), "<", f_large, fill=WHITE)

    # Search icon
    e = elements[1]
    _tc(draw, (e.dev_x, e.dev_y), "Q", f_title, fill=WHITE)

    # Search/input bar
    e = elements[2]
    ix1, iy1, ix2, iy2 = e.bbox()
    _rbox(draw, ix1, iy1, ix2, iy2, round(sf * 0.018), fill=INPUT_BG,
          outline="#3a3a5a")
    _tc(draw, (e.dev_x - round(e.dev_w * 0.32), e.dev_y),
        "Q", f_body, fill=GRAY)
    draw.text((e.dev_x - round(e.dev_w * 0.28), e.dev_y - round(e.dev_h * 0.2)),
              "Enter destination", font=f_body, fill="#666")

    # Map-like content area with texture
    content_y1, content_y2 = round(h * 0.22), round(h * 0.84)
    draw.rectangle([0, content_y1, w, content_y2], fill="#0d1525")

    # Grid lines for map texture
    grid_spacing = round(sf * 0.12)
    for gx in range(0, w, grid_spacing):
        draw.line([gx, content_y1, gx, content_y2], fill="#111a2a", width=1)
    for gy in range(content_y1, content_y2, grid_spacing):
        draw.line([0, gy, w, gy], fill="#111a2a", width=1)

    # Winding route line
    route = [
        (round(w * 0.10), round(h * 0.55)),
        (round(w * 0.25), round(h * 0.42)),
        (round(w * 0.45), round(h * 0.50)),
        (round(w * 0.65), round(h * 0.35)),
        (round(w * 0.85), round(h * 0.40)),
        (round(w * 0.95), round(h * 0.32)),
    ]
    for i in range(len(route) - 1):
        draw.line([route[i], route[i+1]], fill="#4a90d9", width=round(sf * 0.008))

    # Cards
    card_data = [
        (elements[3], ACCENT,  "Driving",  "Fastest - 35 min"),
        (elements[4], ACCENT2, "Transit",  "About 1h 20m"),
        (elements[5], ACCENT3, "Walking",  "About 2h 45m"),
    ]
    for el, accent, title, sub in card_data:
        cx1, cy1, cx2, cy2 = el.bbox()
        _rbox(draw, cx1, cy1, cx2, cy2, round(sf * 0.015), fill=CARD_BG,
              outline=CARD_OL)
        # Accent bar at top
        draw.rectangle([cx1, cy1, cx2, cy1 + round(sf * 0.006)], fill=accent)
        _tc(draw, (el.dev_x, el.dev_y - round(el.dev_h * 0.10)),
            title, f_body, fill=WHITE)
        _tc(draw, (el.dev_x, el.dev_y + round(el.dev_h * 0.15)),
            sub, f_small, fill=GRAY)

    # CTA button
    e = elements[6]
    cx1, cy1, cx2, cy2 = e.bbox()
    _rbox(draw, cx1, cy1, cx2, cy2, round(sf * 0.025), fill=ACCENT)
    _tc(draw, (e.dev_x, e.dev_y - round(e.dev_h * 0.08)),
        "Start Navigation", f_btn, fill="#ffffff")
    _tc(draw, (e.dev_x, e.dev_y + round(e.dev_h * 0.23)),
        "35 km . Est. 42 min", f_small, fill="#ffcccc")

    # Bottom navigation
    nav_y1, nav_y2 = round(h * 0.84), h
    draw.rectangle([0, nav_y1, w, nav_y2], fill=NAV_BG)
    draw.line([0, nav_y1, w, nav_y1], fill=NAV_LINE, width=2)

    nav_data = [
        (elements[7], "[]", "Home"),
        (elements[8], "@",  "Route"),
        (elements[9], "O",  "Profile"),
    ]
    for el, icon, label in nav_data:
        _tc(draw, (el.dev_x, el.dev_y - round(el.dev_h * 0.20)),
            icon, f_body, fill=GRAY)
        _tc(draw, (el.dev_x, el.dev_y + round(el.dev_h * 0.28)),
            label, f_small, fill=GRAY)

    # ── Fiducial corner markers ──────────────────────────────────────────
    # 4 colored squares at known screen-fraction positions.  After device
    # roundtrip, we find these in the screenshot to compute the exact
    # offset+scale of the displayed image — no hard-coded assumptions.
    marker_size = max(6, round(sf * 0.007))  # ~10px on 1440-wide
    # Safe positions (avoid extreme edges that get cropped by viewer chrome)
    markers = [
        (round(w * 0.05), round(h * 0.05),   "#ff00ff"),  # TL magenta
        (round(w * 0.95), round(h * 0.05),   "#00ffff"),  # TR cyan
        (round(w * 0.05), round(h * 0.90),   "#ffff00"),  # BL yellow
        (round(w * 0.95), round(h * 0.90),   "#00ff00"),  # BR green
        (round(w * 0.50), round(h * 0.05),   "#ff8800"),  # TC orange (spare)
    ]
    for mx, my, color in markers:
        draw.rectangle(
            [mx - marker_size, my - marker_size,
             mx + marker_size, my + marker_size],
            fill=color, outline="#000000", width=1,
        )
    # Store marker positions for later detection
    img.info["fiducial_markers"] = json.dumps([
        {"x": mx, "y": my, "x_frac": mx/w, "y_frac": my/h}
        for mx, my, _ in markers
    ])

    img.save(path, format="PNG")
    print(f"  Test UI: {path} ({w}x{h})")


# ── Screenshot / device helpers ─────────────────────────────────────────────

def _find_adb() -> str:
    r = subprocess.run(["which", "adb"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "adb"


def take_screenshot(device: str, dest: Path) -> bool:
    adb = _find_adb()
    r = subprocess.run(
        [adb, "-s", device, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=15,
    )
    if r.returncode == 0 and r.stdout:
        dest.write_bytes(r.stdout)
        return True
    return False


def display_on_device(device: str, local: Path) -> bool:
    """Push image to device and open fullscreen with no viewer chrome.

    Uses immersive mode to hide status/nav bars so the image fills the
    entire screen — matching the agent's pipeline where screenshot = screen.
    """
    adb = _find_adb()
    remote = "/sdcard/vlm_test_ui.png"

    # Enable immersive mode to hide status + nav bars
    subprocess.run(
        [adb, "-s", device, "shell", "settings", "put", "global",
         "policy_control", "immersive.full=*"],
        capture_output=True, timeout=10,
    )

    r = subprocess.run(
        [adb, "-s", device, "push", str(local), remote],
        capture_output=True, timeout=15,
    )
    if r.returncode != 0:
        print(f"  Push failed: {r.stderr.decode()}")
        return False

    # Open image — tap center to dismiss viewer chrome after load
    subprocess.run(
        [adb, "-s", device, "shell", "am", "start",
         "-a", "android.intent.action.VIEW",
         "-d", f"file://{remote}", "-t", "image/png"],
        capture_output=True, timeout=15,
    )
    time.sleep(2.5)

    # Multiple taps to dismiss any viewer UI
    for _ in range(3):
        subprocess.run(
            [adb, "-s", device, "shell", "input", "tap",
             str(DEV_W // 2), str(DEV_H // 2)],
            capture_output=True, timeout=5,
        )
        time.sleep(0.8)

    return True


def detect_image_offset(screenshot_path: Path, original_path: Path
                        ) -> tuple[float, float, float, float, float, float]:
    """Detect where the test image appears in the device screenshot using
    fiducial corner markers (colored squares at known positions).

    Returns (scale_x, scale_y, offset_x, offset_y, ss_w, ss_h) — the
    affine transform that maps original-image coordinates to screenshot
    coordinates:  ss_x = ox * scale_x + offset_x

    If detection fails, returns (1, 1, 0, 0, ss_w, ss_h) — identity.
    """
    from PIL import Image
    orig = Image.open(original_path)
    ss = Image.open(screenshot_path)
    sw, sh = ss.size
    ow, oh = orig.size

    # First fast path: if same size and top-left pixel matches exactly
    if (sw, sh) == (ow, oh):
        opx = orig.getpixel((5, 5))
        spx = ss.getpixel((5, 5))
        if opx == spx:
            return 1.0, 1.0, 0.0, 0.0, sw, sh

    # Fiducial marker colors to search for (known positions in original)
    markers_orig = [
        (round(ow * 0.05), round(oh * 0.05), (255, 0, 255)),    # TL magenta
        (round(ow * 0.95), round(oh * 0.05), (0, 255, 255)),    # TR cyan
        (round(ow * 0.05), round(oh * 0.90), (255, 255, 0)),    # BL yellow
        (round(ow * 0.95), round(oh * 0.90), (0, 255, 0)),      # BR green
        (round(ow * 0.50), round(oh * 0.05), (255, 136, 0)),    # TC orange (spare)
    ]

    # Search for markers in screenshot (subsample for speed)
    step = max(1, min(sw, sh) // 400)  # ~3-4 px step
    found = []  # (ss_x, ss_y, orig_x, orig_y)

    for ox, oy, target_color in markers_orig:
        best_x, best_y, best_dist = None, None, 999
        # Search in a reasonable region
        expected_ss_x = int(ox * sw / ow)
        expected_ss_y = int(oy * sh / oh)
        search_r = max(80, min(sw, sh) // 10)

        for sy in range(max(0, expected_ss_y - search_r),
                        min(sh, expected_ss_y + search_r), step):
            for sx in range(max(0, expected_ss_x - search_r),
                            min(sw, expected_ss_x + search_r), step):
                px = ss.getpixel((sx, sy))
                # Color distance
                dist = sum(abs(int(px[i]) - target_color[i]) for i in range(3))
                if dist < best_dist:
                    best_dist = dist
                    best_x, best_y = sx, sy

        if best_dist < 180:  # reasonable color match
            found.append((best_x, best_y, ox, oy))
            print(f"  Marker at orig({ox},{oy}) → ss({best_x},{best_y}) dist={best_dist}")
        else:
            print(f"  !! Marker at orig({ox},{oy}) NOT FOUND (best dist={best_dist})")

    if len(found) < 2:
        print(f"  !! Fiducial detection failed ({len(found)}/4 markers found)")
        return 1.0, 1.0, 0.0, 0.0, sw, sh

    # Compute affine transform from original → screenshot
    # We solve for: ss_x = orig_x * scale_x + offset_x
    # Using least squares across found markers
    n = len(found)
    sum_ox, sum_oy = sum(f[2] for f in found), sum(f[3] for f in found)
    sum_sx, sum_sy = sum(f[0] for f in found), sum(f[1] for f in found)
    sum_ox2, sum_oy2 = sum(f[2]*f[2] for f in found), sum(f[3]*f[3] for f in found)
    sum_ox_sx = sum(f[2]*f[0] for f in found)
    sum_oy_sy = sum(f[3]*f[1] for f in found)

    denom_x = n * sum_ox2 - sum_ox * sum_ox
    denom_y = n * sum_oy2 - sum_oy * sum_oy

    if abs(denom_x) < 0.001 or abs(denom_y) < 0.001:
        # Degenerate — use simple average ratio
        scale_x = sum_sx / sum_ox if sum_ox != 0 else 1.0
        scale_y = sum_sy / sum_oy if sum_oy != 0 else 1.0
        offset_x = (sum_sx - scale_x * sum_ox) / n
        offset_y = (sum_sy - scale_y * sum_oy) / n
    else:
        scale_x = (n * sum_ox_sx - sum_ox * sum_sx) / denom_x
        offset_x = (sum_sx - scale_x * sum_ox) / n
        scale_y = (n * sum_oy_sy - sum_oy * sum_sy) / denom_y
        offset_y = (sum_sy - scale_y * sum_oy) / n

    print(f"  Detected transform: scale=({scale_x:.4f},{scale_y:.4f}) "
          f"offset=({offset_x:.1f},{offset_y:.1f})")
    return scale_x, scale_y, offset_x, offset_y, sw, sh


def restore_immersive(device: str) -> None:
    """Restore normal display mode."""
    subprocess.run(
        [_find_adb(), "-s", device, "shell", "settings", "put", "global",
         "policy_control", "null"],
        capture_output=True, timeout=10,
    )


# ── VLM calling ─────────────────────────────────────────────────────────────

def _resolve_model(requested: str | None) -> str:
    """Resolve which model to use.  If requested, patch config cache."""
    if requested:
        import artemis_agent as _agent
        cfg = _agent.load_skill_config()
        cfg["model"] = requested
        # Also clear the cached prompt selection so _get_operator_system_prompt picks
        # the right prompt for the new model
        return requested
    cfg = load_skill_config()
    return cfg.get("model", "?")


# Direct import of the agent's system prompt selection + the prompts themselves,
# so --agent-prompt mode uses the EXACT same prompts the real agent sends.

from artemis_agent import (
    _get_operator_system_prompt,
    SYSTEM_PROMPT_FALLBACK,
    SYSTEM_PROMPT_COMPACT,
)

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a precision coordinate locator for mobile UI screenshots.

    COORDINATE SPACE: 1000x1000. Output ONLY coordinates in 0-1000 range.
    (0,0) is top-left. (1000,1000) is bottom-right.
    Each (x,y) is the CENTER of the named element.

    RULES:
    - Output RAW JSON ONLY. No thinking. No explanation. No markdown.
    - Do NOT describe the image. Do NOT estimate pixel bounds.
    - Just output the JSON object. Nothing else — not even a newline before it.

    {"elements": [{"name": "<name>", "x": <int 0-1000>, "y": <int 0-1000>}, ...]}
    """)


def build_user_prompt(elements: list[UIElement], agent_mode: bool = False) -> str:
    """Build user prompt — clean test mode vs. agent-identical mode."""
    if not agent_mode:
        lines = [
            "This is a screenshot of a mobile navigation app.",
            "Locate the exact CENTER coordinate of each UI element below.",
            "Output in 0-1000 normalized space.",
            "",
            "Elements:",
        ]
        for el in elements:
            lines.append(f'  "{el.name}" — {el.description}')
        lines.append("")
        lines.append(
            'Output: {"elements": [{"name": "...", "x": <int>, "y": <int>}, ...]}'
        )
        return "\n".join(lines)

    # ── Agent-identical mode ───────────────────────────────────────────────
    # Matches the exact prompt structure of _decide_action_fallback():
    #   TASK + Step + PREVIOUS ACTIONS + UI ELEMENTS + COORDINATE SPACE
    #
    # CRITICAL: Element coordinates in the UI INDEX are DELIBERATELY NOISY
    # (±15-25 norm units off), simulating real accessibility-tree inaccuracies.
    # The VLM must use VISUAL understanding to correct them — if it just echoes
    # the prompt coordinates, accuracy will be poor and the test will catch it.
    import random as _random
    _noise_rng = _random.Random(42)  # fixed seed for reproducibility
    NOISE_RANGE = 20  # max norm-unit deviation per axis

    lines = [
        "TASK: Locate the center coordinate of each UI element listed below.",
        "Step: 1",
        "",
        "PREVIOUS ACTIONS (do NOT repeat these):",
        "  - (none — first step)",
    ]

    # Build a fake UI element index — same format the agent sends:
    #   [N] "label" center=(cx,cy) cls=ClassName rid=resource_id [clickable|enabled]
    lines.append("")
    lines.append(f"UI ELEMENTS ({len(elements)} shown of {len(elements)} indexed):")
    noisy_coords = {}  # record noisy coords for debugging
    for i, el in enumerate(elements):
        true_cx = round(el.dev_x * 1000 / max(1, DEV_W))
        true_cy = round(el.dev_y * 1000 / max(1, DEV_H))
        # Add deliberate noise (±NOISE_RANGE) to simulate real a11y tree errors
        nx = true_cx + _noise_rng.randint(-NOISE_RANGE, NOISE_RANGE)
        ny = true_cy + _noise_rng.randint(-NOISE_RANGE, NOISE_RANGE)
        nx = max(0, min(nx, 1000))
        ny = max(0, min(ny, 1000))
        noisy_coords[el.name] = (nx, ny, true_cx, true_cy)
        # Pick a plausible class name based on element type
        if "btn" in el.name or "icon" in el.name:
            cls = "ImageView" if "icon" in el.name else "Button"
        elif "input" in el.name:
            cls = "EditText"
        elif "card" in el.name:
            cls = "LinearLayout"
        elif "cta" in el.name:
            cls = "Button"
        else:
            cls = "TextView"
        lines.append(
            f'  [{i}] "{el.description.split(chr(34))[0] if chr(34) in el.description else el.name}"'
            f" center=({nx},{ny}) cls={cls} clickable enabled"
        )

    # Show noisy vs true for debugging
    print(f"\n  Agent-mode noisy element coordinates (±{NOISE_RANGE} norm units):")
    print(f"  {'Element':<16s} {'noisy':>12s} {'true':>12s} {'Δ':>6s}")
    for name, (nx, ny, tx, ty) in noisy_coords.items():
        d = math.sqrt((nx-tx)**2 + (ny-ty)**2)
        print(f"  {name:<16s} ({nx:>4d},{ny:>4d})   ({tx:>4d},{ty:>4d})   {d:>5.1f}")

    lines.append("")
    lines.append(
        "COORDINATE SPACE: 1000×1000. Output ALL coordinates in 0-1000 range. "
        "They will be auto-scaled to device pixels."
    )
    lines.append("")
    lines.append(
        "The UI element positions above may be approximate — use the SCREENSHOT "
        "to determine the TRUE visual center of each element. "
        "For each element, report its exact center coordinate. "
        'Output as a JSON list of tap actions, one per element. '
        'Format: [{"action":"tap","coordinate":[x,y],"target_description":"..."}, ...]'
    )
    return "\n".join(lines)


def parse_response(raw: str, names: set[str],
                   agent_mode: bool = False) -> dict[str, tuple[float, float]] | None:
    """Parse VLM response — clean test format or agent action format."""
    if not raw:
        print(f"  !! EMPTY response from VLM")
        return None
    text = raw.strip()

    # Debug: show response tail (VLM output often ends with JSON)
    print(f"  VLM response tail (last 600 chars): ...{text[-600:]}")

    # Find outermost JSON object or array by brace/bracket matching
    brace_starts = []
    depth = 0
    for i, c in enumerate(text):
        if c in ('{', '['):
            if depth == 0:
                brace_starts.append((i, c))
            depth += 1
        elif c in ('}', ']'):
            depth -= 1

    if not brace_starts:
        print(f"  !! JSON parse FAILED — no JSON found")
        print(f"  Raw (first 400): {raw[:400]}")
        return None

    # Try from last { or [ backward
    for start, opener in reversed(brace_starts):
        closer = '}' if opener == '{' else ']'
        depth = 0
        end = -1
        for j in range(start, len(text)):
            if text[j] in ('{', '['):
                depth += 1
            elif text[j] in ('}', ']'):
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end > start:
            try:
                data = json.loads(text[start:end])
                if agent_mode:
                    # Agent mode: parse [{"action":"tap","coordinate":[...]}, ...]
                    return _parse_agent_response(data, names)
                else:
                    # Clean mode: {"elements": [{"name":..., "x":..., "y":...}, ...]}
                    if isinstance(data, dict) and "elements" in data:
                        return _parse_clean_response(data, names)
                    if isinstance(data, list) and data and "action" in data[0]:
                        # VLM sometimes outputs list even in clean mode
                        return _parse_agent_response(data, names)
            except json.JSONDecodeError:
                continue

    print(f"  !! JSON parse FAILED — no valid object found")
    print(f"  Raw (first 400): {raw[:400]}")
    return None


def _parse_clean_response(data: dict, names: set[str]
                          ) -> dict[str, tuple[float, float]] | None:
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
        print(f"  !! VLM missed: {sorted(missing)}")
    return result


def _parse_agent_response(data, names: set[str]
                           ) -> dict[str, tuple[float, float]] | None:
    """Parse agent-format JSON: list of {"action":"tap","coordinate":[x,y],
    "target_description":"..."} objects.  Match target_description keywords
    to our known element names. Also handles {"action":"finish"}."""
    items = data if isinstance(data, list) else [data]

    # If data is a dict with "action" key, treat as single item
    if isinstance(data, dict) and "action" in data:
        items = [data]

    result: dict[str, tuple[float, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        action = item.get("action", "")
        if action == "finish":
            continue  # VLM wants to end — skip
        if action not in ("tap", "click"):
            continue
        coord = item.get("coordinate")
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue
        desc = (item.get("target_description", "") or "").lower()

        # Match target_description against element names
        matched = None
        for name in sorted(names, key=len, reverse=True):
            parts = name.split("_")
            if all(p in desc for p in parts):
                matched = name
                break
            if name in desc:
                matched = name
                break

        if matched:
            try:
                result[matched] = (float(coord[0]), float(coord[1]))
                print(f"    parsed: {matched} -> ({coord[0]}, {coord[1]})")
            except (TypeError, ValueError):
                pass

    missing = names - set(result.keys())
    if missing:
        print(f"  !! VLM missed (agent mode): {sorted(missing)}")
    if not result:
        print(f"  !! No elements matched — debug raw items:")
        for i, item in enumerate(items[:3]):
            print(f"    item[{i}]: {json.dumps(item, ensure_ascii=False)[:200]}")
    return result


# ── Results ─────────────────────────────────────────────────────────────────

@dataclass
class TResult:
    name: str
    pred_x: float; pred_y: float
    gt_x: float; gt_y: float
    delta_norm: float = 0.0
    delta_dev: float = 0.0
    hit: bool = False


def compute(preds: dict[str, tuple[float, float]],
            elements: list[UIElement]) -> list[TResult]:
    results = []
    for el in elements:
        p = preds.get(el.name)
        if p is None:
            results.append(TResult(el.name, 0, 0, el.norm_x, el.norm_y, 999, 99999, False))
            continue
        px, py = p
        dx = (px - el.norm_x) / 1000 * DEV_W
        dy = (py - el.norm_y) / 1000 * DEV_H
        dn = math.sqrt((px - el.norm_x)**2 + (py - el.norm_y)**2)
        dd = math.sqrt(dx*dx + dy*dy)
        hit = abs(dx) <= el.dev_w/2 and abs(dy) <= el.dev_h/2
        results.append(TResult(el.name, px, py, el.norm_x, el.norm_y, dn, dd, hit))
    return results


def print_report(results: list[TResult], label: str, run_i: int = 1):
    print(f"\n{'='*95}")
    print(f"  VLM Accuracy — {label} (Run {run_i})")
    print(f"{'='*95}")
    hdr = (f"  {'Element':<16s} {'Δxy':>6s} {'Δx':>8s} {'Δy':>8s} "
           f"{'Δdev':>7s} {'Hit':>5s}  {'Pred':>18s}  {'GT':>18s}")
    print(hdr)
    print("  " + "-" * 91)

    valid = [r for r in results if r.delta_norm < 999]
    for r in results:
        hit_s = "YES" if r.hit else "NO"
        print(f"  {r.name:<16s} {r.delta_norm:>5.1f}  "
              f"{r.pred_x - r.gt_x:>+7.1f}  {r.pred_y - r.gt_y:>+7.1f}  "
              f"{r.delta_dev:>6.0f}px  {hit_s:>4s}  "
              f"({r.pred_x:>6.0f},{r.pred_y:>6.0f})        "
              f"({r.gt_x:>6.0f},{r.gt_y:>6.0f})")

    n = len(valid)
    if n == 0:
        print("  SUMMARY: all elements missed — VLM output format mismatch")
        return
    avg = sum(r.delta_norm for r in valid) / n
    hits = sum(1 for r in results if r.hit)
    avg_dev = sum(r.delta_dev for r in valid) / n if n > 0 else 0

    print("  " + "-" * 91)
    print(f"  SUMMARY: avg Δ={avg:.1f} norm  Δdev={avg_dev:.0f}px  "
          f"hit={hits}/{n} ({hits/n*100:.0f}%)")

    # By category
    cats = [
        ("Small edge",      ["back_btn", "search_icon"]),
        ("Thin bar",        ["text_input"]),
        ("Medium cards",    ["left_card", "center_card", "right_card"]),
        ("Large CTA",       ["cta_button"]),
        ("Bottom nav",      ["nav_home", "nav_route", "nav_profile"]),
    ]
    print("\n  By category:")
    for cname, cnames in cats:
        items = [r for r in valid if r.name in cnames]
        if items:
            a = sum(r.delta_norm for r in items) / len(items)
            ad = sum(r.delta_dev for r in items) / len(items)
            h = sum(1 for r in items if r.hit)
            print(f"    {cname:<18s} Δ={a:4.1f}  Δdev={ad:5.0f}px  hit={h}/{len(items)}")

    # Axis bias
    x_err = [r.pred_x - r.gt_x for r in valid]
    y_err = [r.pred_y - r.gt_y for r in valid]
    ax = sum(x_err) / len(x_err) if x_err else 0
    ay = sum(y_err) / len(y_err) if y_err else 0
    print(f"\n  Axis bias:  X = {ax:+.1f}  Y = {ay:+.1f}")
    if abs(ay) > 5:
        print(f"  ** SIGNIFICANT Y-axis bias — VLM consistently off in Y **")
    if abs(ax) > 5:
        print(f"  ** SIGNIFICANT X-axis bias — VLM consistently off in X **")
    print()


# ── Disambiguation Test ──────────────────────────────────────────────────
# Tests whether the VLM can correctly distinguish between visually similar
# elements that are close together — the real failure mode in production
# (agent taps search icon instead of text input field).

DISAMBIG_ELEMENT_SPECS = [
    # (name, description, x_frac, y_frac, w_frac, h_frac)
    ("search_icon_hdr",   "the magnifier search icon in the top header bar (near the right side)",
     0.90, 0.07, 0.05, 0.05),
    ("text_input",        "the wide text input/search bar below the header with placeholder text 'Search'",
     0.50, 0.14, 0.75, 0.06),
    ("search_icon_card",  "the magnifier search icon inside a card lower on the screen, next to 'Explore' text",
     0.50, 0.42, 0.05, 0.05),
    ("submit_btn",        "the blue 'Submit' button on the right side of the button row",
     0.68, 0.62, 0.28, 0.08),
    ("cancel_btn",        "the gray 'Cancel' button on the left side of the button row",
     0.32, 0.62, 0.28, 0.08),
    ("nav_item_1",        "the first navigation tab 'Overview' at the bottom",
     0.25, 0.87, 0.20, 0.06),
    ("nav_item_2",        "the second navigation tab 'Details' at the bottom",
     0.50, 0.87, 0.20, 0.06),
    ("nav_item_3",        "the third navigation tab 'Settings' at the bottom",
     0.75, 0.87, 0.20, 0.06),
]

# Each task: (task_prompt, expected_element_name, confusing_alt_name, alt_description)
DISAMBIG_TASKS = [
    (
        "Tap the search text input field where you type your destination",
        "text_input",
        "search_icon_hdr",
        "search icon in header (confusable nearby magnifier)",
    ),
    (
        "Tap the magnifier search icon in the top header bar",
        "search_icon_hdr",
        "search_icon_card",
        "search icon in card lower down (second magnifier on screen)",
    ),
    (
        "Tap the magnifier search icon inside the Explore card",
        "search_icon_card",
        "search_icon_hdr",
        "search icon in header (first magnifier on screen)",
    ),
    (
        "Tap the Submit button to confirm",
        "submit_btn",
        "cancel_btn",
        "Cancel button (adjacent similar button)",
    ),
    (
        "Tap the Cancel button to go back",
        "cancel_btn",
        "submit_btn",
        "Submit button (adjacent similar button)",
    ),
    (
        "Tap the first navigation tab Overview at the bottom",
        "nav_item_1",
        "nav_item_2",
        "Details tab (adjacent nav tab)",
    ),
    (
        "Tap the Settings navigation tab at the bottom",
        "nav_item_3",
        "nav_item_2",
        "Details tab (adjacent nav tab)",
    ),
]


def generate_disambiguation_ui(path: Path, elements: list[UIElement],
                                w: int, h: int) -> None:
    """Draw a UI with deliberately confusable elements for disambiguation testing."""
    img = Image.new("RGB", (w, h), "#1a1a2e")
    draw = ImageDraw.Draw(img)
    sf = min(w, h)

    def fs(ratio): return max(12, round(sf * ratio))
    f_status = _get_font(fs(0.022))
    f_title  = _get_font(fs(0.026))
    f_body   = _get_font(fs(0.023))
    f_small  = _get_font(fs(0.018))
    f_btn    = _get_font(fs(0.028))
    f_large  = _get_font(fs(0.030))

    STAT_BG, HEAD_BG = "#0d0d1a", "#16213e"
    INPUT_BG = "#2a2a4a"
    CARD_BG = "#0f3460"
    ACCENT = "#e94560"
    WHITE, GRAY = "#e0e0e0", "#888888"
    NAV_BG = "#16213e"

    # Status bar
    sb_h = round(h * 0.04)
    draw.rectangle([0, 0, w, sb_h], fill=STAT_BG)
    _tc(draw, (round(w * 0.06), sb_h // 2), "10:30", f_small, fill=GRAY)
    _tc(draw, (round(w * 0.85), sb_h // 2), "WiFi  ----  [] 92%", f_small, fill=GRAY)

    # Header
    hb_y1, hb_y2 = sb_h, round(h * 0.11)
    draw.rectangle([0, hb_y1, w, hb_y2], fill=HEAD_BG)
    _tc(draw, (w // 2, (hb_y1 + hb_y2) // 2), "Search App", f_title, fill=WHITE)

    # Search icon in header (element 0)
    e = elements[0]
    _tc(draw, (e.dev_x, e.dev_y), "Q", f_large, fill=WHITE)

    # Text input bar (element 1)
    e = elements[1]
    ix1, iy1, ix2, iy2 = e.bbox()
    _rbox(draw, ix1, iy1, ix2, iy2, round(sf * 0.018), fill=INPUT_BG, outline="#3a3a5a")
    draw.text((e.dev_x - round(e.dev_w * 0.42), e.dev_y - round(e.dev_h * 0.22)),
              "Search destinations...", font=f_body, fill="#666")

    # Content area
    cy1, cy2 = round(h * 0.20), round(h * 0.84)
    draw.rectangle([0, cy1, w, cy2], fill="#0d1525")

    # Card with second search icon (element 2)
    e = elements[2]
    cx1, cy1_card, cx2, cy2_card = e.dev_x - round(w * 0.35), e.dev_y - round(h * 0.08), \
                                     e.dev_x + round(w * 0.35), e.dev_y + round(h * 0.08)
    _rbox(draw, cx1, cy1_card, cx2, cy2_card, round(sf * 0.015), fill=CARD_BG, outline="#1a3a60")
    # Card title
    _tc(draw, (e.dev_x - round(e.dev_w * 1.5), e.dev_y - round(e.dev_h * 0.3)),
        "Explore", f_body, fill=WHITE)
    # Second search icon inside card
    _tc(draw, (e.dev_x, e.dev_y), "Q", f_body, fill=WHITE)

    # Grid lines for background texture
    gs = round(sf * 0.12)
    for gx in range(0, w, gs):
        draw.line([gx, cy1, gx, cy2], fill="#111a2a", width=1)
    for gy in range(cy1, cy2, gs):
        draw.line([0, gy, w, gy], fill="#111a2a", width=1)

    # Button row: Cancel (element 4) + Submit (element 3)
    for el, label, color in [
        (elements[4], "Cancel", "#555577"),
        (elements[3], "Submit", "#3366cc"),
    ]:
        bx1, by1, bx2, by2 = el.bbox()
        _rbox(draw, bx1, by1, bx2, by2, round(sf * 0.015), fill=color)
        _tc(draw, (el.dev_x, el.dev_y), label, f_btn, fill=WHITE)

    # Bottom navigation
    nav_y1, nav_y2 = round(h * 0.84), h
    draw.rectangle([0, nav_y1, w, nav_y2], fill=NAV_BG)
    draw.line([0, nav_y1, w, nav_y1], fill="#1e2d50", width=2)

    nav_labels = ["Overview", "Details", "Settings"]
    for i, el in enumerate([elements[5], elements[6], elements[7]]):
        _tc(draw, (el.dev_x, el.dev_y - round(el.dev_h * 0.15)),
            chr(ord("A") + i), f_body, fill=GRAY)
        _tc(draw, (el.dev_x, el.dev_y + round(el.dev_h * 0.22)),
            nav_labels[i], f_small, fill=GRAY)

    # Fiducial markers
    marker_size = max(6, round(sf * 0.007))
    markers = [
        (round(w * 0.05), round(h * 0.05), "#ff00ff"),
        (round(w * 0.95), round(h * 0.05), "#00ffff"),
        (round(w * 0.05), round(h * 0.90), "#ffff00"),
        (round(w * 0.95), round(h * 0.90), "#00ff00"),
        (round(w * 0.50), round(h * 0.05), "#ff8800"),
    ]
    for mx, my, color in markers:
        draw.rectangle(
            [mx - marker_size, my - marker_size,
             mx + marker_size, my + marker_size],
            fill=color, outline="#000000", width=1,
        )

    img.save(path, format="PNG")
    print(f"  Disambiguation UI: {path} ({w}x{h})")


def run_disambiguation_test(test_path: Path, sys_prompt: str,
                             agent_mode: bool, cfg: dict) -> None:
    """Run targeted disambiguation tasks and report confusion rates."""
    from artemis_agent import _encode_image, _call_vlm, NORM_MAX_DIM

    print(f"\n{'='*95}")
    print(f"  DISAMBIGUATION TEST — Can VLM distinguish similar nearby elements?")
    print(f"{'='*95}")
    print(f"  Model: {cfg.get('model','?')}  Prompt: {'AGENT' if agent_mode else 'CLEAN'}")

    # Build elements for the disambiguation UI
    w, h = DEV_W, DEV_H
    elements = []
    for spec in DISAMBIG_ELEMENT_SPECS:
        e = UIElement(*spec)
        e.compute(w, h)
        elements.append(e)
    el_map = {e.name: e for e in elements}

    # Print ground truth
    print(f"\n  Elements ({w}x{h}):")
    print(f"  {'Name':<20s} {'dev(x,y)':>16s} {'norm(x,y)':>16s} {'size':>10s}")
    for el in elements:
        print(f"  {el.name:<20s} ({el.dev_x:>5d},{el.dev_y:>5d})    "
              f"({el.norm_x:>5.0f},{el.norm_y:>5.0f})    "
              f"{el.dev_w}x{el.dev_h}")

    # Build single-locate user prompt — in disambiguation mode we ask for ONE element
    # at a time, so the VLM must pick the right one from visually similar options
    def build_disambig_user_prompt(task_desc: str) -> str:
        coords_hint = (
            "Output your answer as a JSON object with a single coordinate. "
            'Format: {"x": <int 0-1000>, "y": <int 0-1000>}'
        )
        if agent_mode:
            return (
                f"TASK: {task_desc}\n"
                f"Step: 1\n\n"
                f"PREVIOUS ACTIONS (do NOT repeat these):\n"
                f"  - (none — first step)\n\n"
                f"COORDINATE SPACE: 1000x1000. Output the center coordinate of "
                f"the target element you choose.\n\n"
                f'Output as JSON: {{"action":"tap","coordinate":[x,y],"target_description":"<which element you chose>"}}\n'
            )
        else:
            return (
                f"{task_desc}\n\n"
                f"Output ONLY the normalized coordinate (0-1000 range) of the "
                f"element you choose.\n\n"
                f'Format: {{"x": <int>, "y": <int>}}'
            )

    # Results tracking
    @dataclass
    class DisambigResult:
        task: str
        expected: str
        expected_desc: str
        alt_name: str
        alt_desc: str
        pred_x: float = 0
        pred_y: float = 0
        closest_match: str = ""
        confusion: str = ""  # "correct", "confused_alt", "missed_both"
        delta_norm: float = 0
        delta_alt_norm: float = 0

    results: list[DisambigResult] = []

    for task_desc, expected_name, alt_name, alt_desc in DISAMBIG_TASKS:
        expected = el_map[expected_name]
        alt = el_map[alt_name]

        user_prompt = build_disambig_user_prompt(task_desc)

        print(f"\n  ── Task: \"{task_desc[:72]}...\" ──")
        print(f"     Expected: {expected_name} ({expected.norm_x:.0f},{expected.norm_y:.0f})")
        print(f"     Alt confuse: {alt_name} ({alt.norm_x:.0f},{alt.norm_y:.0f})")
        print(f"     Distance between: {math.sqrt((expected.norm_x-alt.norm_x)**2 + (expected.norm_y-alt.norm_y)**2):.0f} norm units")

        t0 = time.time()
        raw, reason, _, _ = _call_vlm(
            sys_prompt, user_prompt, str(test_path), max_tokens=2048,
        )
        elapsed = time.time() - t0
        print(f"     {elapsed:.1f}s  finish={reason}  chars={len(raw or '')}")

        r = DisambigResult(
            task=task_desc, expected=expected_name,
            expected_desc=expected.description,
            alt_name=alt_name, alt_desc=alt_desc,
        )

        if not raw:
            r.closest_match = "NO_RESPONSE"
            r.confusion = "missed_both"
            results.append(r)
            print(f"     !! NO RESPONSE")
            continue

        # Parse coordinate from response
        text = raw.strip()
        # Find JSON object with x,y
        m = re.search(r'[{]\s*"x"\s*:\s*(\d+)\s*,\s*"y"\s*:\s*(\d+)\s*[}]', text)
        if not m:
            # Try agent format: "coordinate":[x,y]
            m2 = re.search(r'"coordinate"\s*:\s*\[(\d+)\s*,\s*(\d+)\]', text)
            if m2:
                r.pred_x, r.pred_y = float(m2.group(1)), float(m2.group(2))
            else:
                r.closest_match = "PARSE_FAIL"
                r.confusion = "missed_both"
                results.append(r)
                print(f"     !! PARSE FAIL: ...{text[-200:]}")
                continue
        else:
            r.pred_x, r.pred_y = float(m.group(1)), float(m.group(2))

        # Compute deltas to expected and alt
        d_expected = math.sqrt(
            (r.pred_x - expected.norm_x)**2 + (r.pred_y - expected.norm_y)**2
        )
        d_alt = math.sqrt(
            (r.pred_x - alt.norm_x)**2 + (r.pred_y - alt.norm_y)**2
        )
        r.delta_norm = d_expected
        r.delta_alt_norm = d_alt

        # Determine which element was chosen
        half_dist = math.sqrt(
            (expected.norm_x - alt.norm_x)**2 + (expected.norm_y - alt.norm_y)**2
        ) / 2

        if d_expected <= d_alt:
            r.closest_match = expected_name
            if d_expected <= half_dist:
                r.confusion = "correct"
            else:
                r.confusion = "between"  # roughly equidistant
        else:
            r.closest_match = alt_name
            if d_alt <= half_dist:
                r.confusion = "confused_alt"
            else:
                r.confusion = "between"

        # Determine visual confusion status
        status = {
            "correct": "CORRECT",
            "confused_alt": f"CONFUSED with {alt_name}",
            "between": "BETWEEN (ambiguous)",
            "missed_both": "MISSED",
        }.get(r.confusion, r.confusion)

        print(f"     Pred: ({r.pred_x:.0f},{r.pred_y:.0f})  "
              f"Δexpected={d_expected:.1f}  Δalt={d_alt:.1f}  → {status}")
        results.append(r)

    # ── Summary ──
    print(f"\n{'='*95}")
    print(f"  DISAMBIGUATION RESULTS")
    print(f"{'='*95}")
    print(f"  {'Task':<50s} {'Expected':<14s} {'Result':<24s} {'Δexp':>5s} {'Δalt':>5s}")
    print(f"  {'-'*50} {'-'*14} {'-'*24} {'-'*5} {'-'*5}")

    correct = confused = missed = between = 0
    for r in results:
        exp_short = r.expected[:13]
        status_s = {
            "correct": "✓ CORRECT",
            "confused_alt": f"✗ CONFUSED→{r.alt_name}",
            "between": "? BETWEEN (ambig)",
            "missed_both": "✗ MISSED/NO RESP",
        }.get(r.confusion, r.confusion)
        print(f"  {r.task[:50]:<50s} {exp_short:<14s} {status_s:<24s} {r.delta_norm:>5.1f} {r.delta_alt_norm:>5.1f}")

        if r.confusion == "correct":
            correct += 1
        elif r.confusion == "confused_alt":
            confused += 1
        elif r.confusion == "between":
            between += 1
        else:
            missed += 1

    total = len(results)
    print(f"\n  {'─'*95}")
    print(f"  CORRECT:  {correct}/{total} ({correct/total*100:.0f}%)")
    print(f"  CONFUSED: {confused}/{total} ({confused/total*100:.0f}%)  ← picks wrong similar element")
    print(f"  BETWEEN:  {between}/{total} ({between/total*100:.0f}%)  ← ambiguous, between both")
    print(f"  MISSED:   {missed}/{total} ({missed/total*100:.0f}%)  ← parse fail or no response")
    print(f"  {'─'*95}")

    if confused > 0:
        print(f"\n  ** VLM CONFUSES similar nearby elements {confused}/{total} times **")
        print(f"  ** This matches the production failure: agent taps search icon")
        print(f"     instead of text input field. Root cause confirmed. **")

    if confused == 0 and correct == total:
        print(f"\n  ** VLM perfectly disambiguates all {total} similar elements. **")
        print(f"  ** The production coordinate problem is NOT a visual confusion issue. **")
        print(f"  ** Look elsewhere: a11y-tree coords, element indexer, or multi-step planning. **")

    print()


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="E2E VLM coordinate accuracy test")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--device", type=str, default="a60f6655")
    ap.add_argument("--synthetic-only", action="store_true",
                    help="Skip device — test with direct PIL image only")
    ap.add_argument("--resolution", type=str, default=None,
                    help="Force resolution WxH (e.g. 1440x3120)")
    ap.add_argument("--agent-prompt", action="store_true",
                    help="Use the EXACT agent operator prompt instead of clean test prompt")
    ap.add_argument("--model", type=str, default=None,
                    help="Override VLM model (e.g. GLM-5.3-Flash, MiniMax-M3)")
    ap.add_argument("--base-url", type=str, default=None,
                    help="Override VLM base URL (e.g. https://api.avian.io/v1)")
    ap.add_argument("--api-key", type=str, default=None,
                    help="Override API key")
    ap.add_argument("--disambiguation", action="store_true",
                    help="Run disambiguation test: can VLM distinguish similar nearby elements?")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Determine resolution ──
    global DEV_W, DEV_H

    if args.resolution:
        DEV_W, DEV_H = map(int, args.resolution.split("x"))

    # Generate test image at target resolution
    elements = build_elements(DEV_W, DEV_H)
    pil_path = OUT_DIR / "test_ui.png"
    generate_test_image(pil_path, elements, DEV_W, DEV_H)

    # Print ground truth
    print(f"\n  Ground truth ({DEV_W}x{DEV_H}, "
          f"{'landscape' if DEV_W > DEV_H else 'portrait'}):")
    print(f"  {'Element':<16s} {'dev(x,y)':>18s} {'norm(x,y)':>18s} {'size':>12s} {'tol':>6s}")
    print(f"  {'-'*16} {'-'*18} {'-'*18} {'-'*12} {'-'*6}")
    for el in elements:
        print(f"  {el.name:<16s} ({el.dev_x:>5d},{el.dev_y:>5d})     "
              f"({el.norm_x:>5.0f},{el.norm_y:>5.0f})        "
              f"{el.dev_w:>4d}x{el.dev_h:<4d} ±{el.tolerance_dev_px:.0f}")

    # ── Decide which screenshot to send to VLM ──
    test_path = pil_path   # default: direct PIL image

    if not args.synthetic_only:
        # Try device roundtrip
        src_path = OUT_DIR / "test_ui_source.png"
        import shutil
        shutil.copy(pil_path, src_path)

        print(f"\n  Displaying on device {args.device} (immersive mode)...")
        if display_on_device(args.device, src_path):
            dev_ss = OUT_DIR / "device_screenshot.png"
            print(f"  Taking device screenshot...")
            if take_screenshot(args.device, dev_ss):
                # Restore normal display
                restore_immersive(args.device)
                # Detect actual screenshot resolution
                with Image.open(dev_ss) as img:
                    sw, sh = img.size
                print(f"  Device screenshot: {dev_ss} ({sw}x{sh})")

                # Detect offset: where does test image appear in screenshot?
                off = detect_image_offset(dev_ss, src_path)
                scale_x, scale_y, off_x, off_y, sw, sh = off
                DEV_W, DEV_H = sw, sh

                if abs(scale_x - 1.0) > 0.005 or abs(scale_y - 1.0) > 0.005 \
                   or abs(off_x) > 2 or abs(off_y) > 2:
                    print(f"\n  Adjusting ground truth for transform: "
                          f"scale=({scale_x:.4f},{scale_y:.4f}) offset=({off_x:.1f},{off_y:.1f})")
                    visible = []
                    for el in elements:
                        # Map original coords to screenshot coords
                        el.dev_x = round(el.dev_x * scale_x + off_x)
                        el.dev_y = round(el.dev_y * scale_y + off_y)
                        el.dev_w = round(el.dev_w * scale_x)
                        el.dev_h = round(el.dev_h * scale_y)
                        el.norm_x = round(el.dev_x / sw * 1000, 1)
                        el.norm_y = round(el.dev_y / sh * 1000, 1)
                        # Check if element is visible in the screenshot
                        margin = min(el.dev_w, el.dev_h) // 2
                        if (0 - margin <= el.dev_x <= sw + margin and
                            0 - margin <= el.dev_y <= sh + margin):
                            visible.append(el)
                        else:
                            print(f"  !! {el.name} OFF-SCREEN ({el.dev_x},{el.dev_y}) — excluding")
                    print(f"  {len(visible)}/{len(elements)} elements visible after transform")
                    excluded = [e for e in elements if e not in visible]
                    # Update elements and names for the test
                    elements[:] = visible
                    names = {e.name for e in elements}
                    print(f"  {'Element':<16s} {'dev(x,y)':>18s} {'norm(x,y)':>18s}")
                    for el in elements:
                        print(f"  {el.name:<16s} ({el.dev_x:>5d},{el.dev_y:>5d})     "
                              f"({el.norm_x:>5.0f},{el.norm_y:>5.0f})")
                else:
                    if (sw, sh) != (DEV_W, DEV_H):
                        print(f"  NOTE: screenshot resolution ({sw}x{sh}) differs "
                              f"from generated ({DEV_W}x{DEV_H})")
                        DEV_W, DEV_H = sw, sh
                        elements = build_elements(DEV_W, DEV_H)
                test_path = dev_ss
            else:
                print("  Screenshot failed — falling back to synthetic")
        else:
            print("  Device display failed — falling back to synthetic")

    # ── Run VLM tests ──
    cfg = load_skill_config()
    if args.model:
        cfg["model"] = args.model
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.api_key:
        cfg["api_key"] = args.api_key
    from artemis_agent import NORM_MAX_DIM
    agent_mode = args.agent_prompt
    sys_prompt = _get_operator_system_prompt() if agent_mode else SYSTEM_PROMPT
    model_hint = cfg.get("model", "?")

    # ── Disambiguation mode: route to dedicated test ──
    if args.disambiguation:
        # Generate the disambiguation UI
        disambig_path = OUT_DIR / "test_ui_disambig.png"
        disambig_els = []
        for spec in DISAMBIG_ELEMENT_SPECS:
            e = UIElement(*spec)
            e.compute(DEV_W, DEV_H)
            disambig_els.append(e)
        generate_disambiguation_ui(disambig_path, disambig_els, DEV_W, DEV_H)
        # Use synthetic path for disambiguation test
        run_disambiguation_test(disambig_path, sys_prompt, agent_mode, cfg)
        return

    print(f"\n  Pipeline: {test_path}")
    print(f"    → _encode_image (NORM_MAX_DIM={NORM_MAX_DIM})")
    print(f"    → _call_vlm  (model={model_hint}, max_tokens=4096)")
    print(f"    → PROMPT MODE: {'AGENT-IDENTICAL' if agent_mode else 'CLEAN TEST'}")

    if agent_mode:
        # Show which system prompt the agent would select
        prompt_type = "COMPACT" if "flash" in model_hint.lower() else "FALLBACK"
        print(f"    → System prompt: {prompt_type} (based on model='{model_hint}')")

    # Show encoded size
    b64, nw, nh = _encode_image(str(test_path))
    print(f"    encoded: {len(b64)} chars base64, norm_space=({nw},{nh})\n")

    names = {e.name for e in elements}
    all_results: list[list[TResult]] = []

    for run_i in range(1, args.runs + 1):
        print(f"  Run {run_i}/{args.runs}...")

        t0 = time.time()
        max_tok = 8192 if agent_mode else 4096  # agent prompt needs more room
        raw, reason, _, _ = _call_vlm(
            sys_prompt, build_user_prompt(elements, agent_mode),
            str(test_path), max_tokens=max_tok,
        )
        elapsed = time.time() - t0
        ok = "OK" if raw else "EMPTY"
        print(f"    {elapsed:.1f}s  finish={reason}  chars={len(raw or '')}  status={ok}")

        if not raw:
            print(f"    -> SKIP (no content)\n")
            continue

        preds = parse_response(raw, names, agent_mode)
        if preds is None:
            continue

        results = compute(preds, elements)
        all_results.append(results)
        print_report(results, "FAKE UI", run_i)

    # Aggregate
    if args.runs > 1 and all_results:
        print(f"{'='*95}")
        print(f"  AGGREGATE ({len(all_results)} runs)")
        print(f"{'='*95}")
        for el in elements:
            xs, ys = [], []
            for rr in all_results:
                for r in rr:
                    if r.name == el.name and r.delta_norm < 999:
                        xs.append(abs(r.pred_x - r.gt_x))
                        ys.append(abs(r.pred_y - r.gt_y))
            if xs:
                ax = sum(xs)/len(xs); ay = sum(ys)/len(ys)
                print(f"  {el.name:<16s} |Δx|={ax:.1f} |Δy|={ay:.1f} "
                      f"~{ax/1000*DEV_W:.0f}x{ay/1000*DEV_H:.0f} px ({len(xs)} runs)")

    print(f"\n  Test image: {test_path}")


if __name__ == "__main__":
    main()