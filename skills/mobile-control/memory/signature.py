from __future__ import annotations

import hashlib
import re
from typing import Iterable


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
    # Use only the foreground package name for cross-run stability.
    # The full UI layout text changes between runs (ads, timestamps, search
    # history items) so including it produces different hashes on every run,
    # defeating memory lookup.  The package alone is the stable signal for
    # "which app/screen context are we in".
    return hash_tokens([foreground_pkg])


def build_state_key(intent_signature: str, ui_fingerprint: str, device_bucket: str) -> str:
    return f"{intent_signature}:{ui_fingerprint}:{device_bucket}"
