from __future__ import annotations

import hashlib
import re
from typing import Iterable


_VOLATILE_TEXT_PATTERNS = [
    r"\b\d{1,2}:\d{2}\b",             # clock text
    r"\b\d+%\b",                      # battery/progress percentages
    r"\b\d+\s*(秒|分钟|小时|s|sec|min)\b",  # countdowns/durations
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
    
    Pixel bounds are stripped since they vary between runs and screen sizes.
    """
    stable_lines: list[str] = []
    for raw_line in (ui_summary or "").splitlines():
        line = normalize_text(raw_line)
        if not line or line.startswith("ui elements on screen"):
            continue
        
        # Only include interactive elements (clickable/long-clickable).
        # This makes the fingerprint more specific to the screen's action model
        # rather than persistent UI elements that appear on all screens.
        if "clickable" not in line:
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
    return hash_tokens([foreground_pkg, *stable_lines[:40]])


def build_state_key(intent_signature: str, ui_fingerprint: str, device_bucket: str) -> str:
    return f"{intent_signature}:{ui_fingerprint}:{device_bucket}"
