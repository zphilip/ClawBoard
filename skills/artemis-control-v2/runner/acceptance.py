"""Direction-aware acceptance evaluation from read-only probe evidence.

An acceptance criterion is a *predicate on device state*, and a predicate has a
DIRECTION: "silent" wants the ring volume DOWN, "turn Wi-Fi on" wants a setting
UP, "turn Do-not-disturb off" wants ``zen_mode`` DOWN. Hard-coding one direction
(the old code did ``volume == 0 → satisfied`` and ``zen_mode != 0 → satisfied``)
silently inverts every reverse task: "关闭勿扰" would be reported as DONE the
moment DND was still ON, and "调高音量" could never be satisfied.

So the direction is parsed from the criterion text (with an explicit
``[want: …]`` escape hatch), the evidence is parsed into typed *facts*, and the
two are compared. When a criterion needs a baseline to judge ("调低亮度" — lower
than WHAT?), the answer is ``None`` (inconclusive) so the final Checker decides,
never a guessed ``False``.

This module is deliberately task-agnostic: it knows about *subjects* (ringer,
zen, wifi, airplane, bluetooth, brightness) because those are the enumerated
probe kinds, not because any particular task exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Goal model
# ---------------------------------------------------------------------------

#: Polarity of the wanted state change.
POL_SILENT = "silent"      # ringer muted (volume 0 / ringer silent|vibrate / DND)
POL_ON = "on"              # a boolean switch should be enabled
POL_OFF = "off"            # a boolean switch should be disabled
POL_NUMERIC = "numeric"    # explicit comparator against an absolute value
POL_DOWN = "down"          # "lower it" — needs a baseline, cannot judge alone
POL_UP = "up"              # "raise it"  — needs a baseline, cannot judge alone

#: Subjects, keyed by the setting name they read (or a synthetic name).
_SUBJECT_SETTINGS: dict[str, str] = {
    "zen": "zen_mode",
    "wifi": "wifi_on",
    "airplane": "airplane_mode_on",
    "bluetooth": "bluetooth_on",
    "brightness": "screen_brightness",
    "ringer": "",  # synthetic: ring volume + ringer mode
    "volume": "",  # synthetic: ring volume
    "generic": "",
}

#: Earliest keyword wins, so the verb of the sentence decides the polarity
#: ("关闭勿扰" → off, "打开勿扰" → on).
_POLARITY_TOKENS: list[tuple[str, tuple[str, ...]]] = [
    (POL_SILENT, ("静音", "无声", "免打扰时静音", "mute", "silent", "silenced")),
    (POL_OFF, ("关闭", "关掉", "取消", "禁用", "停用", "关", "turn off", "switch off",
               "disable", "deactivate")),
    (POL_ON, ("打开", "开启", "启用", "开", "turn on", "switch on", "enable",
              "activate")),
    (POL_DOWN, ("调低", "降低", "减小", "下调", "减少", "调暗", "变暗", "下降",
                "lower", "decrease", "reduce", "dim", "darken")),
    (POL_UP, ("调高", "提高", "增大", "上调", "增加", "调亮", "变亮", "上升",
              "raise", "increase", "louder", "brighter", "brighten")),
]

_SUBJECT_TOKENS: list[tuple[str, tuple[str, ...]]] = [
    ("zen", ("勿扰", "免打扰", "dnd", "do not disturb", "zen")),
    ("ringer", ("铃声", "响铃", "ringer", "ringtone", "响铃模式", "铃声模式")),
    ("media", ("媒体", "music", "media volume", "音乐")),
    ("volume", ("音量", "volume")),
    ("wifi", ("wifi", "wlan", "无线")),
    ("airplane", ("飞行", "airplane", "flight mode")),
    ("bluetooth", ("蓝牙", "bluetooth")),
    ("brightness", ("亮度", "brightness")),
]

_WANT_HINT_RE = re.compile(r"[\[(]\s*want\s*:\s*([^\]\)]+)[\])]", re.IGNORECASE)
#: App-presence hint: ``[app: com.baidu.BaiduMap]`` on a ``- verify:`` line names the
#: package that must be in the foreground. This is how open-app / navigate /
#: search / play tasks are judged from the ``probe foreground`` evidence without
#: a numeric setting.
_APP_HINT_RE = re.compile(r"[\[(]\s*app\s*:\s*([A-Za-z0-9._]+)\s*[\])]", re.IGNORECASE)
_COMPARATOR_RE = re.compile(r"^\s*(<=|>=|==|!=|<|>|=)\s*(-?\d+)\s*$")


@dataclass(frozen=True)
class Goal:
    """Parsed acceptance criterion."""

    subject: str = "generic"
    polarity: str = POL_NUMERIC
    comparator: str = ""
    target: Optional[int] = None
    target_pkg: str = ""


def _earliest(text: str, tokens: tuple[str, ...]) -> tuple[int, str]:
    """Position of the earliest token occurrence, or (-1, "")."""
    best_pos, best_tok = -1, ""
    for tok in tokens:
        pos = text.find(tok)
        if pos >= 0 and (best_pos < 0 or pos < best_pos):
            best_pos, best_tok = pos, tok
    return best_pos, best_tok


def parse_goal(text: str) -> Goal:
    """Parse a ``- verify:`` criterion into a subject + polarity.

    Order of authority:
      1. explicit ``[want: <op> <n>]`` / ``(want: …)`` → absolute numeric test;
      2. the earliest polarity keyword (the sentence's verb);
      3. the subject keywords (what the predicate is about).

    A criterion whose verb is "lower/raise" cannot be judged from a single
    reading, so it stays ``POL_DOWN``/``POL_UP`` and evaluation returns None.
    """
    raw = text or ""
    lowered = raw.lower()

    app_pkg = ""
    am = _APP_HINT_RE.search(raw)
    if am:
        app_pkg = am.group(1)

    m = _WANT_HINT_RE.search(raw)
    if m:
        cm = _COMPARATOR_RE.match(m.group(1))
        if cm:
            op = cm.group(1)
            return Goal(
                subject="app" if app_pkg else _subject_of(lowered),
                polarity=POL_NUMERIC,
                comparator="==" if op == "=" else op,
                target=int(cm.group(2)),
                target_pkg=app_pkg,
            )

    polarity = POL_NUMERIC
    best_pos = -1
    for pol, tokens in _POLARITY_TOKENS:
        pos, _tok = _earliest(lowered, tokens)
        if pos >= 0 and (best_pos < 0 or pos < best_pos):
            best_pos, polarity = pos, pol

    return Goal(
        subject="app" if app_pkg else _subject_of(lowered),
        polarity=polarity,
        target_pkg=app_pkg,
    )


def _subject_of(lowered: str) -> str:
    for subject, tokens in _SUBJECT_TOKENS:
        for tok in tokens:
            if tok in lowered:
                return subject
    return "generic"


# ---------------------------------------------------------------------------
# Evidence model
# ---------------------------------------------------------------------------

_VOLUME_RE = re.compile(r"volume is (-?\d+)", re.IGNORECASE)
_VOLUME_LABEL_RE = re.compile(r"probe\s+volume\s+([A-Za-z0-9_]+)\s*:", re.IGNORECASE)
#: Volume probe stream aliases that mean the MEDIA stream (music == 3), as emitted
#: in the probe label ``probe volume music:`` — a ring/top-level reading must not
#: be counted as the media stream and vice versa.
_MEDIA_STREAMS = frozenset({"music", "media", "3"})
_RINGER_EQ_RE = re.compile(r"mringermode\s*=\s*(\d+)", re.IGNORECASE)
_RINGER_WORD_RE = re.compile(r"ringer\s*mode\s*[:=]\s*([a-z_]+)", re.IGNORECASE)
_ZEN_WORD_RE = re.compile(r"zen[ _]mode\s*[:=]\s*(zen_mode_[a-z_]+)", re.IGNORECASE)
_SETTING_LINE_RE = re.compile(
    r"probe\s+setting\s+([A-Za-z0-9_]+)\s+([A-Za-z0-9._\-]+)\s*:\s*(.+)",
    re.IGNORECASE,
)

_ZEN_WORD_VALUES = {
    "zen_mode_off": 0,
    "zen_mode_important_interruptions": 1,
    "zen_mode_no_interruptions": 2,
    "zen_mode_alarms": 3,
    "zen_mode_priority": 1,
    "zen_mode_default": 1,
    "zen_mode_none": 0,
    "zen_mode_all": 0,
}

_RINGER_WORD_VALUES = {"silent": 0, "vibrate": 1, "normal": 2}

#: Foreground package from the ``probe foreground`` evidence: the RESUMED activity
#: (or the focused window) name is the thing that identifies which app is on screen.
#: ``ActivityRecord{… u0 com.baidu.BaiduMap/.maps…}`` / ``Window{… u0 com.pkg/.Act}``.
_FOREGROUND_RE = re.compile(
    r"(?:mResumedActivity|mFocusedApp|mCurrentFocus)[^\n]*?[\s{]([A-Za-z0-9._]+)/",
    re.IGNORECASE,
)


@dataclass
class Facts:
    """Typed facts extracted from one criterion's probe evidence."""

    ring_volume: Optional[int] = None
    media_volume: Optional[int] = None
    ringer_mode: Optional[int] = None
    foreground_pkg: Optional[str] = None
    settings: dict[str, int] = field(default_factory=dict)

    def get(self, key: str) -> Optional[int]:
        if key == "zen_mode":
            return self.settings.get("zen_mode")
        return self.settings.get(key)


def parse_evidence(evidence: str) -> Facts:
    """Extract typed facts from a probe evidence blob.

    Understands every shape the enumerated probes actually emit:
      ``probe volume ring: [v] will control stream=2 … / [v] volume is 1 …``
      ``probe audio: … Ringer mode: silent / … mRingerMode=2 / - zen mode:ZEN_MODE_…``
      ``probe setting global zen_mode: 1``
    """
    facts = Facts()
    for line in (evidence or "").splitlines():
        sm = _SETTING_LINE_RE.match(line.strip())
        if sm:
            key, raw_val = sm.group(2), sm.group(3).strip()
            num = _first_int(raw_val)
            if num is not None:
                facts.settings[key] = num
            for word, val in _ZEN_WORD_VALUES.items():
                if word in raw_val.lower().replace(" ", "_"):
                    facts.settings.setdefault("zen_mode", val)
            continue

        if facts.foreground_pkg is None:
            fm = _FOREGROUND_RE.search(line)
            if fm:
                facts.foreground_pkg = fm.group(1)

        m = _VOLUME_RE.search(line)
        if m:
            val = int(m.group(1))
            lbl = _VOLUME_LABEL_RE.search(line)
            stream = lbl.group(1).lower() if lbl else ""
            if stream in _MEDIA_STREAMS:
                facts.media_volume = val
            else:
                facts.ring_volume = val
        m = _RINGER_EQ_RE.search(line)
        if m:
            facts.ringer_mode = int(m.group(1))
        m = _RINGER_WORD_RE.search(line)
        if m:
            word = m.group(1).lower()
            if word in _RINGER_WORD_VALUES:
                facts.ringer_mode = _RINGER_WORD_VALUES[word]
        m = _ZEN_WORD_RE.search(line)
        if m:
            val = _ZEN_WORD_VALUES.get(m.group(1).lower())
            if val is not None:
                facts.settings["zen_mode"] = val
        elif "zen" in line.lower() and "zen_mode" not in facts.settings:
            # ``- zen mode: 1`` (numeric form)
            zm = re.search(r"zen[ _]mode\s*[:=]\s*(-?\d+)", line, re.IGNORECASE)
            if zm:
                facts.settings["zen_mode"] = int(zm.group(1))
    return facts


def _first_int(text: str) -> Optional[int]:
    m = re.search(r"-?\d+", text or "")
    return int(m.group(0)) if m else None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_goal(goal: Goal, facts: Facts, baseline=None) -> Optional[bool]:
    """Compare facts against the goal. True / False (decisive) / None (unknown).

    ``baseline`` (optional) is the pre-run snapshot — a :class:`DeviceProfile`,
    any object exposing ``baseline_for(key)``, or a plain ``{key: value}`` map.
    It is what makes a RELATIVE criterion judgeable: "调低亮度" means "lower than
    the value measured before we started", which a single reading cannot express.
    """
    if goal.polarity == POL_SILENT:
        return _eval_silent(facts)
    if goal.subject == "app":
        return _eval_app(goal, facts)
    if goal.polarity in (POL_ON, POL_OFF):
        return _eval_switch(goal, facts)
    if goal.polarity == POL_NUMERIC:
        return _eval_numeric(goal, facts)
    if goal.polarity in (POL_DOWN, POL_UP):
        return _eval_relative(goal, facts, baseline)
    return None


def _eval_app(goal: Goal, facts: Facts) -> Optional[bool]:
    """Judge "the named app is in the foreground".

    The package comes from the ``[app: com.pkg]`` hint on the criterion; the
    evidence comes from the resumed/focused activity in ``probe foreground``.
    Inconclusive (None) when either side is missing — never guessed.
    """
    if not goal.target_pkg or facts.foreground_pkg is None:
        return None
    return facts.foreground_pkg == goal.target_pkg


def _baseline_lookup(baseline, key: str) -> Optional[int]:
    """Read one numeric baseline value from any of the supported shapes."""
    if baseline is None:
        return None
    getter = getattr(baseline, "baseline_for", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:  # pragma: no cover - defensive
            return None
    if isinstance(baseline, dict):
        val = baseline.get(key)
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _subject_values(goal: Goal, facts: Facts) -> list[int]:
    """The numeric reading(s) the subject refers to, most specific first."""
    if goal.subject == "brightness":
        v = facts.settings.get("screen_brightness")
        return [v] if v is not None else []
    if goal.subject == "media":
        return [facts.media_volume] if facts.media_volume is not None else []
    if goal.subject in ("ringer", "volume"):
        return [facts.ring_volume] if facts.ring_volume is not None else []
    if goal.subject == "zen":
        v = facts.settings.get("zen_mode")
        return [v] if v is not None else []
    return []


def _eval_relative(goal: Goal, facts: Facts, baseline) -> Optional[bool]:
    """Judge a "lower it / raise it" goal against the pre-run baseline.

    Without a baseline there is nothing to compare to, so the verdict stays
    inconclusive rather than being guessed.
    """
    if baseline is None:
        return None
    keys = {
        "brightness": "screen_brightness",
        "ringer": "ring_volume",
        "volume": "ring_volume",
        "media": "media_volume",
        "zen": "zen_mode",
    }
    key = keys.get(goal.subject)
    if not key:
        return None
    base = _baseline_lookup(baseline, key)
    if base is None:
        return None
    values = _subject_values(goal, facts)
    if not values:
        return None
    current = values[0]
    if goal.polarity == POL_DOWN:
        return current < base
    return current > base


def _eval_silent(facts: Facts) -> Optional[bool]:
    """Silent is satisfiable by ANY silencing mechanism.

    - ring volume 0, OR
    - ringer mode SILENT(0)/VIBRATE(1), OR
    - any DND mode (zen_mode != 0), which suppresses the ringer too.

    Android clamps STREAM_RING to >= 1 while the ringer mode is NORMAL, so a
    volume-only predicate is unreachable on such ROMs — hence the union.
    """
    decisive = False
    if facts.ring_volume is not None:
        decisive = True
        if facts.ring_volume == 0:
            return True
    if facts.ringer_mode is not None:
        decisive = True
        if facts.ringer_mode in (0, 1):
            return True
    if "zen_mode" in facts.settings:
        decisive = True
        if facts.settings["zen_mode"] != 0:
            return True
    return False if decisive else None


def _switch_value(goal: Goal, facts: Facts) -> Optional[int]:
    key = _SUBJECT_SETTINGS.get(goal.subject, "")
    if key:
        return facts.get(key)
    # Generic subject: if exactly one boolean setting was probed, use it.
    bools = {k: v for k, v in facts.settings.items() if v in (0, 1)}
    if len(bools) == 1:
        return next(iter(bools.values()))
    return None


def _eval_switch(goal: Goal, facts: Facts) -> Optional[bool]:
    val = _switch_value(goal, facts)
    if val is None:
        return None
    if goal.polarity == POL_OFF:
        return val == 0
    return val != 0


def _eval_numeric(goal: Goal, facts: Facts) -> Optional[bool]:
    if goal.target is None:
        return None
    key = _SUBJECT_SETTINGS.get(goal.subject, "")
    val = facts.get(key) if key else None
    if val is None:
        # Fall back to the single numeric fact available (brightness, volume…).
        candidates = [
            v for v in (facts.settings.get("screen_brightness"), facts.ring_volume)
            if v is not None
        ]
        if len(candidates) != 1:
            return None
        val = candidates[0]
    op = goal.comparator or "=="
    target = goal.target
    return {
        "<": val < target,
        "<=": val <= target,
        ">": val > target,
        ">=": val >= target,
        "==": val == target,
        "!=": val != target,
    }.get(op, None)


def evaluate_acceptance(
    verify_texts: list[str], probe_results: dict, baseline=None
) -> Optional[bool]:
    """Combine every verify criterion: True if any is satisfied, False if all
    are decisively contradicted, None when nothing was decidable.

    ``baseline`` (pre-run snapshot) enables relative criteria; pass the object
    returned by ``runner.device_profile.capture``.
    """
    decisive_any = False
    for text in verify_texts or []:
        evidence = probe_results.get(text, "")
        if not evidence:
            continue
        verdict = evaluate_goal(parse_goal(text), parse_evidence(evidence), baseline)
        if verdict is True:
            return True
        if verdict is False:
            decisive_any = True
    return False if decisive_any else None
