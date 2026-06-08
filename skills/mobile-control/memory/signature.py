from __future__ import annotations

import hashlib
import re
from typing import Iterable


# Patterns that match transient / ad / per-run data.  These are applied
# to individual text tokens inside UI element labels so that "跳过 02",
# "跳过 03", etc. all collapse to the same placeholder.
_VOLATILE_TEXT_PATTERNS = [
    # Clock / time patterns
    r"\b\d{1,2}:\d{2}\b",                          # clock text  "02:07"
    r"\b\d{1,2}:\d{2}:\d{2}\b",                    # clock with secs
    # Percentages & numbers
    r"\b\d+%\b",                                     # battery / progress
    r"\b\d+\.\d+\s*km\b",                           # distance with decimals
    r"\b\d+\.\d+\s*公里\b",                          # Chinese distance
    r"\b\d+\s*公里\b",                                # integer distance
    r"\b\d+\s*米\b",                                  # metres
    # Duration / countdown
    r"\b\d+\s*(秒|分钟|小时|min|mins|h|s|sec)\b",    # durations
    # Ad skip buttons: "跳过 02", "跳过", "skip ad", "skip 3s"
    r"\b(跳过|skip)\s*\d*\s*(s|秒|ad|ads)?\b",
    # Timer emoji + digits: "⏳16", "⏳ 16"
    r"[⏳⏰🔔⌛]\s*\d+",
    # Prices
    r"\b[¥￥]\d+",
    # ETA / arrival estimates
    r"\b\d{1,2}:\d{2}\s*(到达|arrive|arrival)",
    r"\b(预计|est\.?)\s*\d{1,2}:\d{2}\b",
    # Pure-numeric standalone labels (step counters, badge counts)
    r"^\d{1,4}$",
]

# Lines whose text (after normalisation) matches any of these keywords
# are EXCLUDED entirely from the fingerprint.  They typically correspond
# to ad banners, promo cards, or suggestions that differ every run.
_AD_EXCLUSION_KEYWORDS = [
    "跳过", "skip", "广告", "ad", "promo", "promotion",
    "推荐", "recommend", "热门", "hot", "活动", "campaign",
    "领券", "coupon", "优惠", "discount", "红包", "red packet",
    "签到", "check-in", "打卡",
    # Timer / countdown overlays
    "⏳", "跳过广告",
]

# Suggestions / search autocomplete — strip the suggestion text but keep
# the element structure (bounds, resource-id) for fingerprinting.
_SUGGESTION_KEYWORDS = [
    "suggestion", "suggest", "autocomplete",
    "搜索发现", "热搜", "搜索历史",
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

        # Exclude ad / promo / suggestion lines entirely — their text
        # changes every run and produces a different fingerprint.
        if any(kw in line for kw in _AD_EXCLUSION_KEYWORDS):
            continue

        # Include both interactive elements AND key state-defining elements.
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

        # For suggestion / autocomplete text, keep only the element structure
        # (class, resource-id, bounds) and strip the suggestion labels.
        if any(kw in line for kw in _SUGGESTION_KEYWORDS):
            line = re.sub(r'"text=[^"]*"', '"text=<suggestion>"', line)
            line = re.sub(r'"content-desc=[^"]*"', '"content-desc=<suggestion>"', line)

        # Collapse volatile text (clock, countdown, distance, price, etc.)
        for pattern in _VOLATILE_TEXT_PATTERNS:
            line = re.sub(pattern, "<volatile>", line)
        # Strip pixel bounds — they vary between runs and screen sizes.
        line = re.sub(r"bounds=\[[\d,]+\]\[[\d,]+\]", "", line)
        # Collapse multiple <volatile> tags into one.
        line = re.sub(r"(<volatile>\s*){2,}", "<volatile>", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            stable_lines.append(line)

    # Cap the number of elements to avoid tiny dynamic tail changes
    # dominating the fingerprint while still distinguishing real app
    # screens.  Sort to ensure consistent ordering regardless of UI
    # dump order.
    stable_lines.sort()
    return hash_tokens([foreground_pkg, *stable_lines[:40]])


def build_state_key(intent_signature: str, ui_fingerprint: str, device_bucket: str) -> str:
    return f"{intent_signature}:{ui_fingerprint}:{device_bucket}"


# ---------------------------------------------------------------------------
# Canonical intent key — groups semantically similar instructions
# ---------------------------------------------------------------------------

# Tier 1: Polite / helper prefixes — always safe to strip iteratively.
# These carry no action semantics, just politeness.
_POLITE_PREFIXES_ZH = ["请帮我", "帮我", "请", "帮忙"]
_POLITE_PREFIXES_EN = ["help me", "please"]

# Tier 2: Synonymous action verb groups — normalise to a canonical verb.
# Two instructions with different action verbs (播放 vs 搜索) are
# genuinely different intents, so we normalise rather than strip.
# Each group maps to its first member (the canonical form).
_VERB_SYNONYMS_ZH: list[list[str]] = [
    ["打开", "开启", "启动", "运行"],   # open / launch
    ["关闭", "退出", "结束"],           # close / quit
    ["搜索", "查", "查找", "搜"],       # search
    ["设置", "设定"],                   # settings
    ["导航", "带我去"],                # navigate
]

_VERB_SYNONYMS_EN: list[list[str]] = [
    ["open", "launch", "start", "run"],
    ["close", "quit", "exit"],
    ["search", "find", "look up"],
    ["set", "configure"],
    ["play"],
    ["navigate"],
]

# Prepositions / particles that don't affect intent
_NOISE_WORDS_ZH = {"的", "了", "一下", "一个", "吧", "呢", "啊", "哦", "嘛", "哈"}
_NOISE_WORDS_EN = {"the", "a", "an", "my", "for me", "on my phone"}


def build_canonical_intent_key(instruction: str) -> str:
    """Build a canonical intent key that groups similar instructions.

    Strategy:
      1. Strip noise words
      2. Iteratively strip polite helpers ("帮我", "please") — multiple passes
      3. Strip at most ONE action verb ("打开", "open") — single pass
      4. Normalise whitespace and case, then hash

    The key design decision: action verbs are stripped only once because
    they carry real semantic meaning.  "播放音乐" (play music) and
    "搜索音乐" (search music) must produce different keys.

    Examples that should map to the same key:
      - "打开微信" / "帮我打开微信"           → "微信" core
      - "导航回家" / "帮我导航回家"           → "回家" core
      - "Open WeChat" / "Please open WeChat"  → "wechat" core

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

    # --- Pass 1: Iteratively strip polite helpers (Chinese) ---
    _sorted_polite_zh = sorted(_POLITE_PREFIXES_ZH, key=len, reverse=True)
    _changed = True
    while _changed:
        _changed = False
        text = text.strip()
        if len(text) <= 1:
            break
        for prefix in _sorted_polite_zh:
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix):]
                _changed = True
                break

    # --- Pass 1b: Iteratively strip polite helpers (English) ---
    _sorted_polite_en = sorted(_POLITE_PREFIXES_EN, key=len, reverse=True)
    _changed = True
    while _changed:
        _changed = False
        text = text.strip()
        if len(text) <= 1:
            break
        for prefix in _sorted_polite_en:
            _pl = prefix.lower()
            if text.startswith(_pl) and len(text) > len(_pl):
                text = text[len(_pl):]
                _changed = True
                break

    # --- Pass 2: Normalise synonymous action verbs (single pass) ---
    # Map different verbs that mean the same thing to a canonical form,
    # so "open WeChat" and "launch WeChat" produce the same key.
    # We do NOT strip verbs — that would collapse "play music" and
    # "search music" into the same key (both would become just "music").
    text = text.strip()
    text = _normalise_synonymous_verbs(text)

    # Final normalisation
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'[,，.。!！?？:：;；]', '', text)  # strip punctuation

    if not text:
        # Fallback: use the raw normalised instruction if stripping removed everything
        text = normalize_text(instruction)

    return hash_tokens([text])


def _normalise_synonymous_verbs(text: str) -> str:
    """Replace synonymous leading verbs with a canonical form.

    E.g.  "开启微信" → "打开微信",  "launch youtube" → "open youtube"

    Important: match the *longest* synonym first to avoid partial replacement.
    Also check the canonical form first — if the text already starts with it,
    skip replacement to avoid corrupting the text.
    """
    for group in _VERB_SYNONYMS_ZH:
        canonical = group[0]
        # Build a longest-first list of all verb forms (canonical + synonyms)
        all_forms = sorted([canonical] + group[1:], key=len, reverse=True)
        matched_form = None
        for form in all_forms:
            if text.startswith(form) and len(text) > len(form):
                matched_form = form
                break
        if matched_form and matched_form != canonical:
            text = canonical + text[len(matched_form):]
            return text
        # If matched_form == canonical, text is already normal — skip

    # English — word-boundary matching, longest first
    for group in _VERB_SYNONYMS_EN:
        canonical = group[0]
        all_forms = sorted([canonical] + group[1:], key=len, reverse=True)
        matched_form = None
        for form in all_forms:
            pattern = r'^' + re.escape(form) + r'\b'
            if re.match(pattern, text):
                matched_form = form
                break
        if matched_form and matched_form != canonical:
            text = re.sub(r'^' + re.escape(matched_form) + r'\b', canonical, text, count=1)
            return text

    return text