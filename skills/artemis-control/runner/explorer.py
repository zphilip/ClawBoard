"""Element indexer: builds a searchable index from the Android UI hierarchy.

Ported from Artemis core:
  - artemis/agents/explorer/screen_index.py  — ScreenIndex, ScreenElement
  - artemis/agents/explorer/geometry.py      — coordinate normalization
  - artemis/utils/ocr_xml_fusion.py          — OCR + UI tree fusion
  - artemis/agents/explorer/tiers.py         — flash/pro/ultra tier profiles

The indexer reads the uiautomator XML dump, extracts all visible/interactive
elements with their resource IDs, text, content descriptions, bounds, and
class names. The VLM can then reference elements by index number instead of
guessing pixel coordinates. Coordinates remain the fallback for custom-drawn
UIs (Canvas, Compose, Flutter) where the accessibility tree is sparse.
"""

from __future__ import annotations

import difflib
import math
import re
import subprocess
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
    bounds: list[int]  # [left, top, right, bottom]
    center: list[int]  # [cx, cy]
    clickable: bool = False
    enabled: bool = True
    checkable: bool = False
    focusable: bool = False
    scrollable: bool = False
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

    def render_index_line(self, width: int = 1080, height: int = 2400) -> str:
        """Render one element as an index line for the VLM prompt.

        Format: ``[N] "label" (resource_id) [cx,cy] clickable=enabled``
        """
        label = self.display_text
        rid = self.resource_id or ""
        rid_short = rid.split("/")[-1] if "/" in rid else rid
        flags = []
        if self.clickable:
            flags.append("clickable")
        if not self.enabled:
            flags.append("disabled")
        flag_str = " " + ",".join(flags) if flags else ""
        nx = round(self.normalized_center[0])
        ny = round(self.normalized_center[1])
        return (
            f'[{self.index}] "{label}"'
            + (f" ({rid_short})" if rid_short else "")
            + f" [{nx},{ny}]{flag_str}"
        )


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

    The index is used by the VLM to reference elements by number, which is
    more precise than guessing pixel coordinates. Elements are filtered to
    include only visible, reasonably-sized interactive elements.

    Three tiers (from artemis/agents/explorer/tiers.py):
    - flash: one-shot, index-only (fastest)
    - pro: index + coordinate audit + visual detection (balanced)
    - ultra: index + OCR + zoom + object detection (most thorough)
    """

    def __init__(self, adb_path: str = "adb", device: Optional[str] = None):
        self._adb_path = adb_path
        self._device = device

    def _adb(self, args: list[str], timeout: int = 10) -> tuple[int, str, str]:
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

    def get_foreground_app(self) -> str:
        """Get the currently foreground app package name."""
        rc, out, _ = self._adb(
            ["shell", "dumpsys", "window", "windows"],
            timeout=5,
        )
        if rc != 0:
            return ""
        for line in out.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                m = re.search(r'(\S+)/(\S+)', line)
                if m:
                    return m.group(1)
        return ""

    def dump_hierarchy(self) -> Optional[str]:
        """Dump the current UI hierarchy as XML."""
        rc, _, _ = self._adb(
            ["shell", "uiautomator", "dump", "/sdcard/_artemis_ex_ui.xml"],
            timeout=8,
        )
        if rc != 0:
            return None
        rc2, out, _ = self._adb(
            ["shell", "cat", "/sdcard/_artemis_ex_ui.xml"],
            timeout=5,
        )
        if rc2 != 0 or not out:
            return None
        return out

    def build_index(
        self,
        screenshot_path: Optional[Path] = None,
        screen_width: int = 1080,
        screen_height: int = 2400,
    ) -> list[ScreenElement]:
        """Build a searchable element index from the current UI hierarchy.

        Args:
            screenshot_path: Optional path to the screenshot (for dimension detection)
            screen_width: Fallback screen width
            screen_height: Fallback screen height

        Returns:
            List of ScreenElement, each with an index number for VLM reference.
        """
        # Try to get real screen dimensions from screenshot
        if screenshot_path and screenshot_path.exists():
            try:
                from PIL import Image
                img = Image.open(screenshot_path)
                screen_width, screen_height = img.size
            except Exception:
                pass

        xml_str = self.dump_hierarchy()
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
            m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if not m:
                continue
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

            # Skip elements that are too small, zero-sized, or off-screen
            w, h = x2 - x1, y2 - y1
            if w <= 0 or h <= 0 or w > screen_width or h > screen_height:
                continue
            if w < 10 and h < 10 and not node.attrib.get("text", ""):
                continue

            text = node.attrib.get("text", "")
            content_desc = node.attrib.get("content-desc", "")
            resource_id = node.attrib.get("resource-id", "")
            class_name = node.attrib.get("class", "")
            package = node.attrib.get("package", "")

            # Skip low-value placeholder-only elements
            label = text or content_desc
            if label.lower().strip() in _PLACEHOLDERS and not resource_id:
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
                package=package,
                normalized_center=[
                    round(cx * 1000.0 / screen_width),
                    round(cy * 1000.0 / screen_height),
                ],
            )
            elements.append(elem)

        return elements

    def render_index_for_prompt(
        self,
        elements: list[ScreenElement],
        max_elements: int = 80,
    ) -> str:
        """Render the element index as a prompt block for the VLM.

        Format:
        ```
        --- Visible UI Elements ---
        [0] "Baidu Maps" (icon) [160,376] clickable
        [1] "Search" (search_box) [540,200] clickable
        ...
        ```
        """
        if not elements:
            return "--- Visible UI Elements ---\n(no elements indexed)"

        lines = ["--- Visible UI Elements ---"]
        # Sort: interactive first, then by Y position (top to bottom)
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

    def search(
        self,
        elements: list[ScreenElement],
        query: str,
        top_k: int = 5,
    ) -> list[ScreenElement]:
        """Search the element index by text, resource_id, or content description.

        Uses fuzzy matching: exact matches rank highest, then prefix, then substring.
        """
        query_lower = query.lower().strip()
        if not query_lower:
            return []

        scored: list[tuple[float, ScreenElement]] = []

        for elem in elements:
            score = 0.0
            # Search fields
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
                # Exact match
                if query_lower == field_lower:
                    score = max(score, 1.0)
                # Prefix match
                elif field_lower.startswith(query_lower):
                    score = max(score, 0.8)
                # Substring match
                elif query_lower in field_lower:
                    score = max(score, 0.6)
                # Fuzzy match
                else:
                    ratio = difflib.SequenceMatcher(None, query_lower, field_lower).ratio()
                    if ratio > 0.5:
                        score = max(score, ratio * 0.5)

            if score > 0:
                scored.append((score, elem))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def resolve_index(
        self,
        elements: list[ScreenElement],
        index: int,
    ) -> Optional[ScreenElement]:
        """Resolve an element index to its ScreenElement.

        Returns None if the index is out of range (element may have disappeared
        between indexing and action — safety net will catch this).
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
            # Prefer clickable, then smallest area
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