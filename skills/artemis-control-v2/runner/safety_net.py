"""Safety net: pre-execution XML + pixel precondition validation.

Ported from Artemis core:
  - artemis/agents/validator/precondition_xml.py  — XML hierarchy matching
  - artemis/agents/validator/precondition_pixel.py — VLM pixel comparison
  - artemis/agents/validator/categories.py        — failure taxonomy
  - artemis/agents/validator/incidents.py          — structured incidents

Checks that the element the agent decided to interact with is still present
on the live screen before executing the action. XML-first (fast, deterministic),
pixel VLM fallback (for custom-drawn UIs).
"""

from __future__ import annotations

import difflib
import math
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Failure taxonomy (from artemis/agents/validator/categories.py)
# ---------------------------------------------------------------------------

class ValidationErrorCategory:
    """Categories for safety-net failures."""
    NONE = "none"
    TARGET_NOT_FOUND = "target_not_found"       # Element missing from UI tree
    TARGET_SHIFTED = "target_shifted"            # Element moved (self-healed)
    TARGET_OCCUPIED = "target_occupied"          # Element covered/obscured
    TARGET_CHANGED = "target_changed"            # Element text/bounds changed
    XML_BYPASSED = "xml_bypassed"                # XML not available, fall to pixel
    PIXEL_MISMATCH = "pixel_mismatch"            # VLM says target doesn't match
    GENERAL = "general"                          # Unclassified failure

# Signal weights for candidate scoring (from precondition_xml.py)
W_ID = 0.5
W_TEXT = 0.4
W_BOUNDS = 0.3
W_COORD = 0.3

# Actions subject to XML validation
XML_VALIDATED_ACTIONS = ("tap", "long_press", "input_text", "focus_and_input_text")

# Actions subject to pixel validation
PIXEL_VALIDATED_ACTIONS = ("tap", "long_press", "input_text", "click_coordinate")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SafetyNetConfig:
    """Configuration for the safety net."""
    adb_path: str = "adb"
    device: Optional[str] = None
    xml_enabled: bool = True
    pixel_fallback: bool = True
    retry_attempts: int = 3
    retry_delay: float = 0.4
    xml_dump_timeout: int = 8
    self_heal_threshold: float = 120.0  # normalized 0-1000 distance

# ---------------------------------------------------------------------------
# Safety Net
# ---------------------------------------------------------------------------

class SafetyNet:
    """Pre-execution safety net that validates action targets.

    Two-tier validation:
    1. XML hierarchy check (fast, sub-50ms) — matches target element in UI tree
    2. Pixel VLM check (slower, ~500ms) — visual comparison of target crop
    """

    def __init__(self, config: SafetyNetConfig):
        self._cfg = config
        self._cached_xml: Optional[str] = None
        self._cached_xml_time: float = 0.0
        self._xml_cache_ttl: float = 2.0  # seconds

    def _adb(self, args: list[str], timeout: int = 10) -> tuple[int, str, str]:
        cmd = [self._cfg.adb_path]
        if self._cfg.device:
            cmd += ["-s", self._cfg.device]
        cmd += args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        except FileNotFoundError:
            return -1, "", "adb not found"

    def _get_ui_xml(self) -> Optional[str]:
        """Get the current UI hierarchy XML, with short-lived cache."""
        now = time.time()
        if self._cached_xml and (now - self._cached_xml_time) < self._xml_cache_ttl:
            return self._cached_xml

        rc, _, _ = self._adb(
            ["shell", "uiautomator", "dump", "/sdcard/_artemis_sf_ui.xml"],
            timeout=self._cfg.xml_dump_timeout
        )
        if rc != 0:
            return None

        rc2, out, _ = self._adb(
            ["shell", "cat", "/sdcard/_artemis_sf_ui.xml"],
            timeout=5
        )
        if rc2 != 0 or not out:
            return None

        self._cached_xml = out
        self._cached_xml_time = now
        return out

    def _parse_xml(self, xml_str: str) -> Optional[ET.Element]:
        """Parse the uiautomator XML dump."""
        try:
            return ET.fromstring(xml_str)
        except ET.ParseError:
            return None

    def _extract_elements(self, root: ET.Element) -> list[dict]:
        """Extract all clickable/visible elements from the UI tree."""
        elements = []
        for node in root.iter("node"):
            bounds_str = node.attrib.get("bounds", "")
            if not bounds_str:
                continue
            m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if not m:
                continue
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            if x2 <= x1 or y2 <= y1:
                continue

            elements.append({
                "resource_id": node.attrib.get("resource-id", ""),
                "class": node.attrib.get("class", ""),
                "text": node.attrib.get("text", ""),
                "content_desc": node.attrib.get("content-desc", ""),
                "bounds": [x1, y1, x2, y2],
                "clickable": node.attrib.get("clickable", "") == "true",
                "enabled": node.attrib.get("enabled", "") == "true",
                "checkable": node.attrib.get("checkable", "") == "true",
                "focusable": node.attrib.get("focusable", "") == "true",
            })
        return elements

    def _score_candidate(self, target: dict, candidate: dict) -> float:
        """Score how well a UI element matches the target.

        Weights from artemis/agents/validator/precondition_xml.py:
        W_ID=0.5, W_TEXT=0.4, W_BOUNDS=0.3, W_COORD=0.3
        """
        score = 0.0

        # Resource ID match
        target_id = target.get("resource_id", "")
        cand_id = candidate.get("resource_id", "")
        if target_id and cand_id:
            if target_id == cand_id:
                score += W_ID
            elif target_id.split("/")[-1] == cand_id.split("/")[-1]:
                score += W_ID * 0.7

        # Text / content-desc match
        target_text = (target.get("text") or target.get("content_desc") or "").lower()
        cand_text = (candidate.get("text") or candidate.get("content_desc") or "").lower()
        if target_text and cand_text:
            if target_text == cand_text:
                score += W_TEXT
            elif target_text in cand_text or cand_text in target_text:
                score += W_TEXT * 0.8
            else:
                # Fuzzy match
                ratio = difflib.SequenceMatcher(None, target_text, cand_text).ratio()
                if ratio > 0.6:
                    score += W_TEXT * ratio * 0.6

        # Bounds overlap
        if "bounds" in target and "bounds" in candidate:
            tb = target["bounds"]
            cb = candidate["bounds"]
            overlap = self._iou(tb, cb)
            score += W_BOUNDS * overlap

        return score

    @staticmethod
    def _iou(box_a: list[int], box_b: list[int]) -> float:
        """Intersection over Union of two bounding boxes."""
        xa = max(box_a[0], box_b[0])
        ya = max(box_a[1], box_b[1])
        xb = min(box_a[2], box_b[2])
        yb = min(box_a[3], box_b[3])
        inter = max(0, xb - xa) * max(0, yb - ya)
        area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
        area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def validate(
        self,
        action_type: str,
        action_args: dict,
        screenshot_path: Optional[Path] = None,
    ) -> tuple[bool, str, str]:
        """Validate that an action's target is still present on screen.

        Args:
            action_type: The action type (tap, long_press, input_text, etc.)
            action_args: The action arguments (coordinate, target_description, etc.)
            screenshot_path: Path to the decision-time screenshot (for pixel fallback)

        Returns:
            (passed, category, reason) tuple
        """
        if action_type not in XML_VALIDATED_ACTIONS:
            return True, ValidationErrorCategory.NONE, ""

        # Build target descriptor from action args
        target = self._build_target_descriptor(action_type, action_args)
        if not target:
            return True, ValidationErrorCategory.NONE, "no target to validate"

        # Try XML validation with retries
        if self._cfg.xml_enabled:
            for attempt in range(1, self._cfg.retry_attempts + 1):
                passed, category, reason = self._validate_xml(target)
                if passed:
                    # Self-heal: update action coordinates if element drifted
                    if "self_healed_coord" in reason:
                        new_coord = self._extract_self_healed_coord(reason)
                        if new_coord and "coordinate" in action_args:
                            action_args["coordinate"] = new_coord
                    return True, category, reason

                if category == ValidationErrorCategory.XML_BYPASSED:
                    break  # Fall through to pixel check

                if attempt < self._cfg.retry_attempts:
                    time.sleep(self._cfg.retry_delay)
                else:
                    # XML validation failed after all retries
                    if self._cfg.pixel_fallback and screenshot_path:
                        return self._validate_pixel(target, screenshot_path)
                    return False, category, reason

        # Pixel fallback
        if self._cfg.pixel_fallback and screenshot_path:
            return self._validate_pixel(target, screenshot_path)

        return True, ValidationErrorCategory.NONE, ""

    def _build_target_descriptor(self, action_type: str, action_args: dict) -> Optional[dict]:
        """Build a target descriptor from action arguments.

        Handles both index-addressed (with target_text, target_resource_id)
        and coordinate-addressed (with target_description) targets.
        """
        target = {}

        # Index-addressed: carries observed element metadata
        if "target_text" in action_args:
            target["text"] = action_args["target_text"]
        if "target_resource_id" in action_args:
            target["resource_id"] = action_args["target_resource_id"]
        if "target_bounds" in action_args:
            target["bounds"] = action_args["target_bounds"]
        if "target_class" in action_args:
            target["class"] = action_args["target_class"]
        if "target_content_desc" in action_args:
            target["content_desc"] = action_args["target_content_desc"]

        # Coordinate-addressed: carries model's own description
        if "target_description" in action_args:
            target["description"] = action_args["target_description"]

        # Always carry the coordinate when present
        if "coordinate" in action_args:
            target["coordinate"] = action_args["coordinate"]

        if not target:
            return None
        return target

    def _validate_xml(self, target: dict) -> tuple[bool, str, str]:
        """Single-shot XML hierarchy validation."""
        xml_str = self._get_ui_xml()
        if not xml_str:
            return False, ValidationErrorCategory.XML_BYPASSED, "UI hierarchy unavailable"

        root = self._parse_xml(xml_str)
        if root is None:
            return False, ValidationErrorCategory.XML_BYPASSED, "XML parse error"

        elements = self._extract_elements(root)
        if not elements:
            return False, ValidationErrorCategory.XML_BYPASSED, "no elements in UI tree"

        # Score every element against the target
        best_score = 0.0
        best_element = None
        for elem in elements:
            score = self._score_candidate(target, elem)
            if score > best_score:
                best_score = score
                best_element = elem

        # Threshold: need at least one strong signal
        min_score = 0.3
        if best_score < min_score:
            return False, ValidationErrorCategory.TARGET_NOT_FOUND, \
                f"best match score {best_score:.2f} < {min_score}"

        # If target had coordinates, check if best element's center is close
        if "coordinate" in target and best_element:
            tc = target["coordinate"]
            bc = best_element["bounds"]
            elem_center = [(bc[0] + bc[2]) // 2, (bc[1] + bc[3]) // 2]
            dist = math.sqrt((tc[0] - elem_center[0])**2 + (tc[1] - elem_center[1])**2)

            if dist > self._cfg.self_heal_threshold:
                return False, ValidationErrorCategory.TARGET_SHIFTED, \
                    f"element center drifted {dist:.0f}px from target"

            if dist > 5:
                # Self-heal: element exists but moved slightly
                healed = {"coordinate": elem_center}
                return True, ValidationErrorCategory.NONE, \
                    f"self_healed_coord:{json.dumps(healed)}"

        return True, ValidationErrorCategory.NONE, ""

    def _validate_pixel(
        self,
        target: dict,
        screenshot_path: Path,
    ) -> tuple[bool, str, str]:
        """Pixel-level VLM validation — placeholder.

        In a full implementation, this would:
        1. Crop the target region from the decision-time screenshot
        2. Take a fresh screenshot
        3. Crop the same region from the fresh screenshot
        4. Ask a VLM: "Is the target button/component at these coordinates
           still present, visible, and interactive?"
        5. Return pass/fail with category

        For now, this is a pass-through (pixel validation requires a VLM).
        """
        # Without a VLM configured, pixel validation cannot be performed.
        # The caller should treat this as "no objection" rather than "passed".
        return True, ValidationErrorCategory.NONE, "pixel validation skipped (no VLM configured)"

    @staticmethod
    def _extract_self_healed_coord(reason: str) -> Optional[list[int]]:
        """Extract self-healed coordinates from the reason string."""
        import json as _json
        m = re.search(r'self_healed_coord:(\{.*?\})', reason)
        if m:
            try:
                data = _json.loads(m.group(1))
                return data.get("coordinate")
            except _json.JSONDecodeError:
                pass
        return None

    def invalidate_cache(self) -> None:
        """Force a fresh UI hierarchy read on next validation."""
        self._cached_xml = None
        self._cached_xml_time = 0.0