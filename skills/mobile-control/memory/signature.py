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


# ---------------------------------------------------------------------------
# Canonical intent key — groups semantically similar instructions
# ---------------------------------------------------------------------------

# Common action verbs / prefixes that don't change the intent.
_ACTION_PREFIXES_ZH = [
    "帮我", "请帮我", "请", "帮忙",
    "打开", "开启", "启动", "运行",
    "关闭", "退出", "结束",
    "发送", "发", "给",
    "搜索", "查", "查找", "搜",
    "设置", "设定",
    "播放", "放",
    "导航", "带我去",
]

_ACTION_PREFIXES_EN = [
    "please", "help me",
    "open", "launch", "start", "run",
    "close", "quit", "exit",
    "send", "type",
    "search", "find", "look up",
    "set", "configure",
    "play",
    "navigate",
]

# Prepositions / particles that don't affect intent
_NOISE_WORDS_ZH = {"的", "了", "一下", "一个", "吧", "呢", "啊", "哦", "嘛", "哈"}
_NOISE_WORDS_EN = {"the", "a", "an", "my", "for me", "on my phone"}


def build_canonical_intent_key(instruction: str) -> str:
    """Build a canonical intent key that groups similar instructions.

    Strategy:
      1. Iteratively strip common action prefixes ("打开", "open", "帮我", "please")
      2. Strip noise words
      3. Normalise whitespace and case
      4. Hash the result

    Examples that should map to the same key:
      - "打开微信" / "开微信" / "帮我打开微信"  → "微信" core
      - "导航回家" / "帮我导航回家"             → "回家" core
      - "Open WeChat" / "Please open WeChat"   → "wechat" core

    NOTE: This is a lightweight heuristic.  It won't perfectly group every
    semantically equivalent instruction, but it significantly improves hit
    rates compared to hashing the raw instruction string.
    """
    text = (instruction or "").strip().lower()

    # Remove noise words (Chinese)
    for noise in _NOISE_WORDS_ZH:
        text = text.replace(noise, "")

    # Remove noise words (English) — whole-word only
    for noise in _NOISE_WORDS_EN:
        text = re.sub(r'\b' + re.escape(noise) + r'\b', '', text, flags=re.IGNORECASE)

    # Iteratively strip action prefixes (Chinese — longest first).
    # Multiple passes so "帮我打开微信" → strip "帮我" → "打开微信" → strip "打开" → "微信"
    # Guard: never strip if it would leave the text empty (the last prefix IS the intent).
    _sorted_zh = sorted(_ACTION_PREFIXES_ZH, key=len, reverse=True)
    _changed = True
    while _changed:
        _changed = False
        text = text.strip()
        if len(text) <= 1:
            break
        for prefix in _sorted_zh:
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix):]
                _changed = True
                break

    # Iteratively strip action prefixes (English — longest first)
    _sorted_en = sorted(_ACTION_PREFIXES_EN, key=len, reverse=True)
    _changed = True
    while _changed:
        _changed = False
        text = text.strip()
        if len(text) <= 1:
            break
        for prefix in _sorted_en:
            _pl = prefix.lower()
            if text.startswith(_pl) and len(text) > len(_pl):
                text = text[len(_pl):]
                _changed = True
                break

    # Final normalisation
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'[,，.。!！?？:：;；]', '', text)  # strip punctuation

    if not text:
        # Fallback: use the raw normalised instruction if stripping removed everything
        text = normalize_text(instruction)

    return hash_tokens([text])