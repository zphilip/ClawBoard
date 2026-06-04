from __future__ import annotations

import hashlib
import re
from typing import Iterable


_VOLATILE_TEXT_PATTERNS = [
    r"\b\d{1,2}:\d{2}\b",             # clock text
    r"\b\d+%\b",                      # battery/progress percentages
    r"\b\d+\s*(秒|分钟|小时|s|sec|min)\b",  # countdowns/durations
    r"\b\d+\.\d+\s*km\b",            # distance with decimals
    r"\b\d+\s*公里\b",                # distance in Chinese
    r"\b¥\d+",                        # prices
]


def normalize_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def hash_tokens(tokens: Iterable[str]) -> str:
    joined = "|".join(normalize_text(t) for t in tokens if t)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def build_intent_signature(instruction: str) -> str:
    return hash_tokens([instruction])


def build_ui_fingerprint(foreground_pkg: str, ui_summary: str) -> str:
    """
    Build a screen-level fingerprint for memory lookup.

    Foreground package alone is too broad: it treats every screen inside an app
    as the same state and can replay stale actions forever. Include a compact,
    normalized slice of the UI dump so cache hits require the same app and the
    same visible/interactable scene.
    
    Only clickable/long-clickable elements are included in the fingerprint.
    Text-only elements (labels, descriptions) are often persistent across
    screens (status bar, nav buttons) or change dynamically (timestamps, ads),
    making the fingerprint too coarse and unstable.
    
    Enhanced to also include key non-clickable elements that define screen state
    (like search results, navigation titles) while filtering volatile content.
    """
    stable_lines: list[str] = []
    for raw_line in (ui_summary or "").splitlines():
        line = normalize_text(raw_line)
        if not line or line.startswith("ui elements on screen"):
            continue
        
        # Include both interactive elements AND key state-defining elements
        # Key state elements: search results, navigation titles, route info
        is_interactive = "clickable" in line or "long-clickable" in line
        is_state_element = (
            "text=" in line or 
            "content-desc=" in line or
            "search" in line.lower() or
            "route" in line.lower() or
            "navigation" in line.lower() or
            "destination" in line.lower()
        )
        
        if not (is_interactive or is_state_element):
            continue
        
        for pattern in _VOLATILE_TEXT_PATTERNS:
            line = re.sub(pattern, "<volatile>", line)
        # Strip pixel bounds — they vary between runs and screen sizes.
        line = re.sub(r"bounds=\[[\d,]+\]\[[\d,]+\]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            stable_lines.append(line)

    # Cap the number of elements to avoid tiny dynamic tail changes dominating
    # the fingerprint while still distinguishing real app screens.
    # Sort to ensure consistent ordering regardless of UI dump order
    stable_lines.sort()
    return hash_tokens([foreground_pkg, *stable_lines[:40]])


def build_state_key(intent_signature: str, ui_fingerprint: str, device_bucket: str) -> str:
    return f"{intent_signature}:{ui_fingerprint}:{device_bucket}"