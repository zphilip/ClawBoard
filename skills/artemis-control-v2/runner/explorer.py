"""Element indexer: builds a searchable index from the Android UI hierarchy.

Ported from Artemis core:
  - artemis/agents/explorer/screen_index.py  — ScreenIndex, ScreenElement
  - artemis/agents/explorer/geometry.py      — coordinate normalization
  - artemis/utils/visualization.py           — format_minimal_list, draw_dots

Plus mobile-control's reliable 3-tier UI dump strategy:
  - skills/mobile-control/utils.py           — get_ui_dump, summarise_ui_dump

The indexer reads the uiautomator XML dump, extracts all visible/interactive
elements with their text, bounds, class names, and resource IDs. The VLM can
then reference elements by index number (target=N) instead of guessing pixel
coordinates.

Three-tier dump (most reliable first):
  1. uiautomator2 Python library (primary — faster, more stable)
  2. ADB ``uiautomator dump`` with 4-attempt exponential backoff (0.5s, 1s, 2s, 4s)
  3. Cached dump from the last successful attempt (graceful degradation)
"""

from __future__ import annotations

import difflib
import math
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ScreenElement:
    """One indexed element from the UI hierarchy."""

    index: int
    resource_id: str
    class_name: str
    text: str
    content_desc: str
    bounds: list[int]  # [left, top, right, bottom] in screen pixels
    center: list[int]  # [cx, cy] in screen pixels
    clickable: bool = False
    enabled: bool = True
    checkable: bool = False
    focusable: bool = False
    scrollable: bool = False
    long_clickable: bool = False
    package: str = ""
    normalized_center: list[float] = field(default_factory=lambda: [0.0, 0.0])

    @property
    def display_text(self) -> str:
        """Best human-readable label for this element."""
        if self.text:
            return self.text[:60]
        if self.content_desc:
            return self.content_desc[:60]
        rid = self.resource_id
        if rid:
            return rid.split("/")[-1].replace("_", " ")[:60]
        cls = self.class_name
        if cls:
            return cls.split(".")[-1][:60]
        return f"[{self.index}]"

    @property
    def class_short(self) -> str:
        """Short class name, e.g. ``EditText`` from ``android.widget.EditText``."""
        return (self.class_name or "").rsplit(".", 1)[-1]

    def render_index_line(self, width: int = 1080, height: int = 2400) -> str:
        """Render one element as a compact index line for the VLM prompt.

        Format: ``[N] [ClassName] "label" center=(cx,cy) clickable``

        The class prefix (e.g. ``[EditText]``, ``[Button]``, ``[ImageView]``)
        lets the VLM distinguish an input field from an icon at a glance.
        """
        label = self.display_text
        rid = self.resource_id or ""
        rid_short = rid.split("/")[-1] if "/" in rid else rid
        cls = self.class_short or ""
        flags = []
        if self.clickable:
            flags.append("clickable")
        if self.long_clickable:
            flags.append("long-clickable")
        if not self.enabled:
            flags.append("disabled")
        if self.scrollable:
            flags.append("scrollable")
        flag_str = " " + ",".join(flags) if flags else ""
        nx = round(self.normalized_center[0])
        ny = round(self.normalized_center[1])
        parts = [f'[{self.index}]']
        if cls:
            parts.append(f'[{cls}]')
        parts.append(f'"{label}"')
        parts.append(f'center=({nx},{ny})')
        if rid_short:
            parts.append(f'rid={rid_short}')
        return " ".join(parts) + flag_str


#: How far outside its labeled element a point may fall and still inherit
#: that element's bounds (as fraction of screen, per side).
LABEL_TOLERANCE_RATIO = 0.02

#: Low-value text that should not appear as element labels.
_PLACEHOLDERS = {"search", "enter text", "placeholder"}


# ---------------------------------------------------------------------------
# Element Indexer
# ---------------------------------------------------------------------------

class ElementIndexer:
    """Builds a searchable index of UI elements from the accessibility tree.

    The index is used by the VLM to reference elements by index number
    (``target=N``), which is more precise than guessing pixel coordinates.
    Elements are filtered to include only visible, reasonably-sized
    interactive elements.

    Three tiers (from artemis/agents/explorer/tiers.py):
    - flash: one-shot, index-only (fastest)
    - pro: index + coordinate audit + visual detection (balanced)
    - ultra: index + OCR + zoom + object detection (most thorough)
    """

    def __init__(self, adb_path: str = "adb", device: Optional[str] = None):
        self._adb_path = adb_path
        self._device = device
        # UI dump cache for graceful degradation
        self._last_successful_dump: str = ""
        self._last_dump_timestamp: float = 0

    # ------------------------------------------------------------------ #
    # ADB helpers
    # ------------------------------------------------------------------ #

    def _adb_cmd(self, args: list[str], timeout: int = 10) -> tuple[int, str, str]:
        """Run an ADB command with ``subprocess.run`` (list form, no shell)."""
        cmd = [self._adb_path]
        if self._device:
            cmd += ["-s", self._device]
        cmd += args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        except FileNotFoundError:
            return -1, "", "adb not found"

    def _adb_shell(self, sh_cmd: str, timeout: int = 15) -> tuple[int, str, str]:
        """Run an ADB shell command (string form, uses ``shell=True`` for pipes/redirects)."""
        cmd = f"{self._adb_path}"
        if self._device:
            cmd += f" -s {self._device}"
        cmd += f" {sh_cmd}"
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, shell=True, timeout=timeout)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        except FileNotFoundError:
            return -1, "", "adb not found"

    # ------------------------------------------------------------------ #
    # uiautomator2 helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _u2_connect(device: Optional[str] = None):
        """Connect to a device via uiautomator2, backward-compatibly."""
        import uiautomator2 as u2

        try:
            return (
                u2.connect(device, init_atx_agent=False)
                if device
                else u2.connect(init_atx_agent=False)
            )
        except TypeError:
            return u2.connect(device) if device else u2.connect()

    @staticmethod
    def _u2_disable_fast_ime(d) -> None:
        """Disable ATX FastInputIME so the system IME is used instead."""
        try:
            d.set_input_ime(False)
        except AttributeError:
            d.set_fastinput_ime(False)  # deprecated but still functional

    def _kill_stale_uiautomation_service(self) -> None:
        """Kill stale ``uiautomator dump`` processes blocking uiautomator2.

        Only kills /system/bin/uiautomator (ADB dump binary) and
        com.wetest.uia2 — never touches atx-agent or the uiautomator2 APK.
        """
        device_flag = ["-s", self._device] if self._device else []
        adb = [self._adb_path] + device_flag

        for pattern in ["uiautomator dump", "com.wetest.uia2"]:
            subprocess.run(
                adb + ["shell", "pkill", "-f", pattern],
                capture_output=True, text=True, timeout=5,
            )

        # PID scan fallback
        r = subprocess.run(
            adb + ["shell", "ps", "-A"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout:
            for line in r.stdout.splitlines():
                _lower = line.lower()
                if (
                    "uiautomator" in _lower
                    and "com.github.uiautomator" not in _lower
                    and "atx-agent" not in _lower
                ):
                    parts = line.split()
                    if len(parts) >= 2:
                        subprocess.run(
                            adb + ["shell", "kill", "-9", parts[1]],
                            capture_output=True, text=True, timeout=5,
                        )

    # ------------------------------------------------------------------ #
    # 3-Tier UI dump (uiautomator2 → ADB retry → cache)
    # ------------------------------------------------------------------ #

    def _get_ui_dump_u2(self) -> str:
        """Primary: dump UI hierarchy via uiautomator2 Python library.

        Returns '' if the library is not installed or the connection fails.
        Retries on UiAutomationService conflicts (from stale ADB dumps).
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                d = self._u2_connect(self._device)
                self._u2_disable_fast_ime(d)
                xml = d.dump_hierarchy()
                return xml or ""
            except ImportError:
                return ""
            except Exception as e:
                msg = str(e)
                if "already registered" in msg or "UiAutomationService" in msg:
                    if attempt < max_retries - 1:
                        delay = 0.5 * (attempt + 1)  # 0.5s, 1.0s, 1.5s
                        self._kill_stale_uiautomation_service()
                        time.sleep(delay)
                        continue
                    else:
                        self._kill_stale_uiautomation_service()
                        return ""
                else:
                    return ""
        return ""

    def _get_ui_dump_adb(self) -> str:
        """Fallback: dump UI hierarchy via ``adb shell uiautomator dump``.

        Returns the raw XML string or '' on failure.
        """
        device_flag = f" -s {self._device}" if self._device else ""
        remote = "/sdcard/_artemis_ex_ui.xml"
        local = ""

        try:
            # Write XML to device storage
            rc, stdout, stderr = self._adb_shell(
                f"shell uiautomator dump {remote}", timeout=15,
            )
            if rc != 0:
                return ""
            if "ERROR" in stdout or "ERROR" in stderr:
                return ""

            # Pull to a temp file
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
                local = tf.name

            rc2, _, _ = self._adb_shell(
                f"pull {remote} {local}", timeout=15,
            )
            if rc2 != 0:
                return ""

            with open(local, encoding="utf-8", errors="replace") as f:
                xml = f.read()

            if xml.count("<node") == 0:
                return ""
            return xml
        except Exception:
            return ""
        finally:
            if local:
                try:
                    os.unlink(local)
                except OSError:
                    pass

    def get_ui_dump(self) -> str:
        """Dump the current UI accessibility hierarchy.

        Multi-tier fallback strategy for maximum reliability:

        1. **uiautomator2** Python library (primary — more stable, no SIGKILL issues)
        2. **ADB** ``uiautomator dump`` with 4-attempt exponential backoff
           (0.5s, 1s, 2s, 4s)
        3. **Cached dump** from the last successful attempt (graceful degradation)
        4. Empty string if all methods fail

        Returns the raw XML string with element bounds, text, and resource IDs.
        """
        # Tier 1: uiautomator2
        xml_u2 = self._get_ui_dump_u2()
        if xml_u2 and xml_u2.count("<node") >= 5:
            self._last_successful_dump = xml_u2
            self._last_dump_timestamp = time.time()
            return xml_u2

        # Tier 2: ADB with exponential backoff (4 attempts)
        max_attempts = 4
        base_delay = 0.5
        for attempt in range(max_attempts):
            xml = self._get_ui_dump_adb()
            if xml and xml.count("<node") >= 5:
                self._last_successful_dump = xml
                self._last_dump_timestamp = time.time()
                return xml
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)  # 0.5, 1, 2, 4
                time.sleep(delay)

        # Tier 3: Cached dump
        if self._last_successful_dump and self._last_successful_dump.count("<node") >= 5:
            return self._last_successful_dump

        return ""

    # ------------------------------------------------------------------ #
    # Element index building
    # ------------------------------------------------------------------ #

    def dump_hierarchy(self) -> Optional[str]:
        """Dump the current UI hierarchy as XML (legacy single-shot — use ``get_ui_dump`` instead)."""
        return self.get_ui_dump() or None

    def build_index(
        self,
        screenshot_path: Optional[Path] = None,
        screen_width: int = 1080,
        screen_height: int = 2400,
    ) -> list[ScreenElement]:
        """Build a searchable element index from the current UI hierarchy.

        Uses the 3-tier ``get_ui_dump()`` for maximum reliability.

        Args:
            screenshot_path: Optional path to the screenshot (for dimension detection).
            screen_width: Fallback screen width.
            screen_height: Fallback screen height.

        Returns:
            List of ScreenElement, each with an index number for VLM reference.
            Returns empty list when the UI dump is unavailable.
        """
        # Try to get real screen dimensions from screenshot
        if screenshot_path and screenshot_path.exists():
            try:
                from PIL import Image

                img = Image.open(screenshot_path)
                screen_width, screen_height = img.size
            except Exception:
                pass

        xml_str = self.get_ui_dump()
        if not xml_str:
            return []

        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            return []

        elements = []
        seen_signatures = set()  # deduplicate by (text, resource_id, bounds)

        for node in root.iter("node"):
            bounds_str = node.attrib.get("bounds", "")
            if not bounds_str:
                continue
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
            if not m:
                continue
            x1, y1, x2, y2 = (
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
            )

            # Skip zero-sized or off-screen elements
            w, h = x2 - x1, y2 - y1
            if w <= 0 or h <= 0 or w > screen_width or h > screen_height:
                continue

            text = node.attrib.get("text", "")
            content_desc = node.attrib.get("content-desc", "")
            resource_id = node.attrib.get("resource-id", "")
            class_name = node.attrib.get("class", "")
            package = node.attrib.get("package", "")

            # Skip invisible tiny elements without text
            if w < 10 and h < 10 and not text and not content_desc and not resource_id:
                continue

            # Skip placeholder-only elements with no resource-id
            label = (text or content_desc).lower().strip()
            if label in _PLACEHOLDERS and not resource_id:
                continue

            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2

            sig = (text, resource_id, x1, y1, x2, y2)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)

            elem = ScreenElement(
                index=len(elements),
                resource_id=resource_id,
                class_name=class_name,
                text=text,
                content_desc=content_desc,
                bounds=[x1, y1, x2, y2],
                center=[cx, cy],
                clickable=node.attrib.get("clickable", "") == "true",
                enabled=node.attrib.get("enabled", "") == "true",
                checkable=node.attrib.get("checkable", "") == "true",
                focusable=node.attrib.get("focusable", "") == "true",
                scrollable=node.attrib.get("scrollable", "") == "true",
                long_clickable=node.attrib.get("long-clickable", "") == "true",
                package=package,
                normalized_center=[
                    round(cx * 1000.0 / screen_width),
                    round(cy * 1000.0 / screen_height),
                ],
            )
            elements.append(elem)

        return elements

    # ------------------------------------------------------------------ #
    # Prompt rendering
    # ------------------------------------------------------------------ #

    def render_index_for_prompt(
        self,
        elements: list[ScreenElement],
        max_elements: int = 80,
    ) -> str:
        """Render the element index as a prompt block for the VLM.

        Format::

            --- Visible UI Elements ---
            [0] [EditText] "Search" center=(500,80) rid=search_box clickable
            [1] [Button] "Send" center=(900,80) rid=send_btn clickable
            ...

        Elements are sorted: interactive first, then by Y position (top→bottom).
        The ``[ClassName]`` prefix lets the VLM distinguish input fields from
        icons at a glance.
        """
        if not elements:
            return "--- Visible UI Elements ---\n(no elements indexed)"

        lines = ["--- Visible UI Elements ---"]
        # Sort: interactive first, then by Y (top to bottom)
        sorted_elems = sorted(
            elements[:max_elements],
            key=lambda e: (
                not e.clickable,
                not e.enabled,
                e.bounds[1],  # top
                e.bounds[0],  # left
            ),
        )
        for elem in sorted_elems:
            lines.append(elem.render_index_line())
        if len(elements) > max_elements:
            lines.append(f"... and {len(elements) - max_elements} more elements")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def search(
        self,
        elements: list[ScreenElement],
        query: str,
        top_k: int = 5,
    ) -> list[ScreenElement]:
        """Search the element index by text, resource-id, or content description.

        Uses fuzzy matching: exact > prefix > substring > SequenceMatcher.
        """
        query_lower = query.lower().strip()
        if not query_lower:
            return []

        scored: list[tuple[float, ScreenElement]] = []

        for elem in elements:
            score = 0.0
            fields = [
                elem.text.lower(),
                elem.content_desc.lower(),
                elem.resource_id.lower(),
                elem.class_name.lower().split(".")[-1],
            ]

            for field in fields:
                if not field:
                    continue
                field_lower = field.lower()
                if query_lower == field_lower:
                    score = max(score, 1.0)
                elif field_lower.startswith(query_lower):
                    score = max(score, 0.8)
                elif query_lower in field_lower:
                    score = max(score, 0.6)
                else:
                    ratio = difflib.SequenceMatcher(
                        None, query_lower, field_lower
                    ).ratio()
                    if ratio > 0.5:
                        score = max(score, ratio * 0.5)

            if score > 0:
                scored.append((score, elem))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    # ------------------------------------------------------------------ #
    # Index resolution
    # ------------------------------------------------------------------ #

    def resolve_index(
        self,
        elements: list[ScreenElement],
        index: int,
    ) -> Optional[ScreenElement]:
        """Resolve an element index to its ScreenElement.

        Returns None if the index is out of range (element may have disappeared
        between indexing and action — check a fresh index if this happens).
        """
        if 0 <= index < len(elements):
            return elements[index]
        return None

    def find_element_at_coord(
        self,
        elements: list[ScreenElement],
        x: int,
        y: int,
    ) -> Optional[ScreenElement]:
        """Find the element at screen coordinates (x, y).

        Returns the smallest containing element, preferring clickable ones.
        Falls back to the nearest element by center distance.
        """
        containing = []
        for elem in elements:
            b = elem.bounds
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                containing.append(elem)

        if containing:
            containing.sort(
                key=lambda e: (
                    not e.clickable,
                    (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]),
                )
            )
            return containing[0]

        # No containing element — find nearest by center distance
        best = None
        best_dist = float("inf")
        for elem in elements:
            dist = math.sqrt(
                (elem.center[0] - x) ** 2 + (elem.center[1] - y) ** 2
            )
            if dist < best_dist:
                best_dist = dist
                best = elem
        return best

    def get_foreground_app(self) -> str:
        """Get the currently foreground app package name."""
        rc, out, _ = self._adb_cmd(
            ["shell", "dumpsys", "window", "windows"],
            timeout=5,
        )
        if rc != 0:
            return ""
        for line in out.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                m = re.search(r"(\S+)/(\S+)", line)
                if m:
                    return m.group(1)
        return ""


# ---------------------------------------------------------------------------
# Screenshot annotation — draw indexed dots on the image
# ---------------------------------------------------------------------------

# ── Dot quality scoring helpers ──────────────────────────────────────────

def _element_dot_score(elem: ScreenElement) -> float:
    """Return a quality score for whether an element deserves a dot and,
    when two elements overlap, which one to keep.

    Heuristic (higher = more likely the leaf-most, actionable element):
      - clickable / long_clickable  → +3 (most actionable)
      - has text or content_desc     → +2 (has a readable label)
      - has resource_id              → +1 (identifiable)
      - checkable / focusable        → +1 (interactive)
      - scrollable                   → -1  (usually a container, not a target)
      - area (bounds)                → smaller = leaf node bonus (up to +2)
    """
    score = 0.0
    if elem.clickable or elem.long_clickable:
        score += 3.0
    if elem.text or elem.content_desc:
        score += 2.0
    if elem.resource_id:
        score += 1.0
    if elem.checkable or elem.focusable:
        score += 1.0
    if elem.scrollable:
        score -= 1.0
    # Smaller elements are more likely to be leaf nodes → bonus
    area = (elem.bounds[2] - elem.bounds[0]) * (elem.bounds[3] - elem.bounds[1])
    if area < 10_000:       # < 100×100 → tiny, likely leaf
        score += 2.0
    elif area < 40_000:      # < 200×200 → small
        score += 1.0
    elif area > 400_000:     # large container
        score -= 1.0
    return score


def _deduplicate_dots(
    elements: list[ScreenElement],
    proximity_px: float,
) -> list[ScreenElement]:
    """Remove elements whose center lies within *proximity_px* of an
    already-kept element's center, keeping the higher-quality one."""
    kept: list[ScreenElement] = []
    kept_scores: list[float] = []
    for elem in elements:
        cx, cy = elem.center[0], elem.center[1]
        score = _element_dot_score(elem)
        collision_idx = None
        for i, (k, ks) in enumerate(zip(kept, kept_scores)):
            dist = math.hypot(cx - k.center[0], cy - k.center[1])
            if dist <= proximity_px:
                # Collision  —  keep the higher-scoring element
                if score > ks:
                    collision_idx = i
                else:
                    collision_idx = -1  # skip this one, kept is better
                break
        if collision_idx is None:
            # No collision — add
            kept.append(elem)
            kept_scores.append(score)
        elif collision_idx >= 0:
            # Replace the kept element with this better one
            kept[collision_idx] = elem
            kept_scores[collision_idx] = score
        # else collision_idx == -1: skip (kept is better)
    return kept


# ── Dot drawing ─────────────────────────────────────────────────────────

def draw_indexed_dots(
    screenshot_path: str,
    elements: list[ScreenElement],
    output_path: str,
    dot_radius: Optional[int] = None,
    font_size: Optional[int] = None,
    max_dots: int = 80,
) -> bool:
    """Draw numbered labels at the center of each indexed element on a screenshot.

    This is the core of the Vision-First mechanism: the VLM sees the numbered
    dots drawn directly on the screenshot image and can visually match each dot
    to the element it marks.  The system resolves ``target=N`` → exact bounds
    mechanically, so the VLM never needs to output raw pixel coordinates.

    Important design decisions that keep dots readable:

    * **Matches the VLM's view** — elements are sorted and truncated the same
      way ``render_index_for_prompt()`` does (interactive first, top → bottom,
      max ``max_dots``).  The VLM never sees a dot without a corresponding
      element line.
    * **Deduplicates by proximity** — two elements whose centres are within
      ``dot_radius × 3`` pixels collide.  The higher-quality leaf element
      (clickable, labeled, small bounds) replaces the wrapper.  This prevents
      the classic Android problem where a single button is wrapped in 3–4
      layout containers and would otherwise get 4 overlapping dots.
    * **Adaptive sizing** — when *dot_radius* / *font_size* are left at the
      default ``None``, they are scaled from the image diagonal so dots look
      the same on a phone (1080 px) and a tablet (2560 px).

    Args:
        screenshot_path: Path to the original screenshot.
        elements: Indexed elements (from ``ElementIndexer.build_index()``).
        output_path: Where to save the annotated image.
        dot_radius: Radius of the numbered circles (auto-scaled when ``None``).
        font_size: Font size for the index numbers (auto-scaled when ``None``).
        max_dots: Maximum number of dots to draw.  Default matches
            ``render_index_for_prompt(max_elements)`` so the VLM sees the
            same set of elements in text and on the image.

    Returns:
        True on success, False on failure.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False

    try:
        img = Image.open(screenshot_path).convert("RGBA")
    except Exception:
        return False

    w_img, h_img = img.size
    diag = math.hypot(w_img, h_img)

    # Adaptive sizing: scale dot size to screen diagonal
    if dot_radius is None:
        dot_radius = max(8, min(22, round(diag / 150.0)))
    if font_size is None:
        font_size = max(10, min(24, dot_radius * 2))

    # ── Step 1: filter to meaningful elements and sort like the VLM sees ──
    # Keep elements that are interactive OR have a readable label.
    # Drop pure-layout wrappers with no identity.
    meaningful = [
        e for e in elements
        if e.clickable or e.long_clickable or e.checkable or e.focusable
        or e.scrollable or e.text or e.content_desc or e.resource_id
    ]
    if not meaningful:
        meaningful = elements  # fallback: draw everything if nothing filtered

    # Sort identically to render_index_for_prompt: interactive first, then Y, then X
    meaningful.sort(
        key=lambda e: (
            not e.clickable,
            not e.enabled,
            e.bounds[1],
            e.bounds[0],
        ),
    )

    # ── Step 2: deduplicate overlapping dots ──
    proximity_px = dot_radius * 3
    deduped = _deduplicate_dots(meaningful, proximity_px)

    # ── Step 3: limit to max_dots ──
    visible = deduped[:max_dots]

    # ── Step 4: draw ──
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size,
        )
    except (OSError, IOError):
        font = ImageFont.load_default()

    for elem in visible:
        cx, cy = elem.center[0], elem.center[1]
        if cx <= 0 or cy <= 0:
            continue

        r = dot_radius
        # Black outer ring (visible on any background)
        draw.ellipse(
            [cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2],
            fill=(0, 0, 0, 220),
        )
        # Colored fill: green for clickable, blue otherwise
        fill_color = (76, 175, 80, 240) if elem.clickable else (33, 150, 243, 240)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill_color)

        # Draw index number centred in the circle
        label = str(elem.index)
        text_w = font_size * len(label) * 0.6
        text_h = font_size * 0.8
        tx = cx - text_w // 2
        ty = cy - text_h // 2
        draw.text((tx, ty), label, fill=(255, 255, 255, 255), font=font)

    # Composite overlay onto original
    result = Image.alpha_composite(img, overlay)
    result = result.convert("RGB")
    try:
        result.save(output_path, "JPEG", quality=92)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Screenshot similarity — detect unchanged screens
# ---------------------------------------------------------------------------

def screenshot_diff_count(
    img1_path: str,
    img2_path: str,
    color_tolerance: int = 8,
    max_allowed: int = 3,
) -> int:
    """Count pixels that differ between two screenshots.

    Returns the count of differing pixels, up to ``max_allowed + 1``
    (early-exits for performance once the threshold is exceeded).
    """
    try:
        from PIL import Image
    except ImportError:
        return 0

    try:
        img1 = Image.open(img1_path).convert("RGB")
        img2 = Image.open(img2_path).convert("RGB")
    except Exception:
        return 0

    if img1.size != img2.size:
        return 999  # different dimensions → definitely different

    data1 = img1.getdata()
    data2 = img2.getdata()
    diff_count = 0
    for p1, p2 in zip(data1, data2):
        if (
            abs(p1[0] - p2[0]) > color_tolerance
            or abs(p1[1] - p2[1]) > color_tolerance
            or abs(p1[2] - p2[2]) > color_tolerance
        ):
            diff_count += 1
            if diff_count > max_allowed:
                break
    return diff_count