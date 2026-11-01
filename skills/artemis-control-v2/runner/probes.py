"""Enumerated read-only device probes (mirrors artemis/agents/checker/checker.py).

The Checker may only read persistent device state — never mutate it.  Probes are
defined as fixed argv templates with regex-validated parameters, so a probe can
never pass shell metacharacters or arbitrary commands.  This file is the single
source of truth for what state the Checker (and the wrapper) may query.

Key additions over upstream's preset:

- ``audio``  — ``dumpsys audio`` (ringer mode / stream volumes), the reliable
  read-only signal for "mute / silent" tasks where no "静音" button exists.
- ``foreground`` — ``dumpsys activity activities`` (which app is on screen).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Validation patterns
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_URI_RE = re.compile(r"^content://[A-Za-z0-9./_-]+$")
_SETTING_NAMESPACES = frozenset({"system", "secure", "global"})

#: AudioManager stream index or a name alias; the value is additionally checked
#: against :data:`_STREAM_ALIASES`, and only digits ever reach the argv after
#: :func:`_canonical_stream` maps the name.
_STREAM_RE = re.compile(r"^[A-Za-z0-9]+$")
#: Named aliases so a plan can say ``[probe: volume ring]`` instead of
#: remembering that ring == stream 2.
_STREAM_ALIASES = frozenset(
    {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
     "voice", "system", "ring", "music", "alarm", "notification"}
)


def _canonical_stream(value: str) -> str:
    """Map a stream alias to its AudioManager index (ring → 2)."""
    return {
        "voice": "0", "system": "1", "ring": "2", "music": "3",
        "alarm": "4", "notification": "5",
    }.get(str(value).strip().lower(), str(value).strip())


@dataclass(frozen=True)
class ProbeParam:
    name: str
    pattern: re.Pattern
    allowed: Optional[frozenset[str]] = None


@dataclass(frozen=True)
class ProbeSpec:
    """A fixed argv template. ``<name>`` placeholders are substituted with
    regex-validated parameters; argv is built as a list and never passes
    through shell string concatenation."""

    argv: tuple[str, ...]
    params: tuple[ProbeParam, ...] = field(default=())


#: The enumerated probe table. Anything not in here is structurally impossible
#: to run — there is no free-form command channel.
PROBES: dict[str, ProbeSpec] = {
    "alarms": ProbeSpec(argv=("dumpsys", "alarm")),
    "battery": ProbeSpec(argv=("dumpsys", "battery")),
    "foreground": ProbeSpec(argv=("dumpsys", "activity", "activities")),
    "notifications": ProbeSpec(argv=("dumpsys", "notification")),
    "audio": ProbeSpec(argv=("dumpsys", "audio")),
    "setting": ProbeSpec(
        argv=("settings", "get", "<namespace>", "<key>"),
        params=(
            ProbeParam("namespace", _KEY_RE, _SETTING_NAMESPACES),
            ProbeParam("key", _KEY_RE),
        ),
    ),
    "content": ProbeSpec(
        argv=("content", "query", "--uri", "<uri>"),
        params=(ProbeParam("uri", _URI_RE),),
    ),
    "packages": ProbeSpec(argv=("pm", "list", "packages")),
    "prop": ProbeSpec(argv=("getprop", "<key>"), params=(ProbeParam("key", _KEY_RE),)),
    # ``media volume --stream N --get`` prints a single integer, which is far
    # more decisive than parsing ``dumpsys audio``. Only the read-only ``--get``
    # operation is expressible: ``--set``/``--adj`` can never be produced because
    # the argv template is fixed.
    "volume": ProbeSpec(
        argv=("media", "volume", "--stream", "<stream>", "--get"),
        params=(ProbeParam("stream", _STREAM_RE, _STREAM_ALIASES),),
    ),
}


def build_probe_argv(kind: str, params: Optional[dict] = None) -> list[str]:
    """Build the argv list for an enumerated probe.

    Raises ``ValueError`` for unknown probe kinds, missing/extra parameters, or
    parameter values that fail their regex (whitespace and shell metacharacters
    can never pass).
    """
    spec = PROBES.get(kind)
    if spec is None:
        raise ValueError(
            f"Unknown probe kind '{kind}'. Allowed: {', '.join(sorted(PROBES))}."
        )
    params = dict(params or {})
    expected = {p.name for p in spec.params}
    extra = set(params) - expected
    if extra:
        raise ValueError(f"Probe '{kind}' accepts no parameter(s): {sorted(extra)}")
    missing = expected - set(params)
    if missing:
        raise ValueError(f"Probe '{kind}' requires parameter(s): {sorted(missing)}")

    values: dict[str, str] = {}
    for p in spec.params:
        raw = params[p.name]
        if not isinstance(raw, str) or not p.pattern.match(raw.strip()):
            raise ValueError(f"Probe '{kind}' parameter '{p.name}' has an invalid value.")
        if p.allowed is not None and raw.strip() not in p.allowed:
            raise ValueError(
                f"Probe '{kind}' parameter '{p.name}' must be one of {sorted(p.allowed)}."
            )
        values[p.name] = raw.strip()

    if kind == "volume":
        values["stream"] = _canonical_stream(values["stream"])

    argv: list[str] = []
    for token in spec.argv:
        if token.startswith("<") and token.endswith(">"):
            argv.append(values[token[1:-1]])
        else:
            argv.append(token)
    return argv


# ---------------------------------------------------------------------------
# Deriving probes from check-item text
# ---------------------------------------------------------------------------

#: Explicit probe hint: ``[probe: audio]``, ``[probe: setting global zen_mode]``,
#: or ``(probe: setting, global, zen_mode)``.
_PROBE_HINT_RE = re.compile(r"[\[(]probe\s*:\s*([^\]\)]+)[\])]")


#: Keyword → probe factory.  Each value returns (kind, params).  First matching
#: keyword wins (list is ordered by specificity).
KEYWORD_PROBE_RULES: list[tuple[tuple[str, ...], str, dict]] = [
    (("zen", "勿扰", "dnd", "do not disturb", "免打扰"), "setting", {"namespace": "global", "key": "zen_mode"}),
    (("airplane", "飞行模式", "飞行"), "setting", {"namespace": "global", "key": "airplane_mode_on"}),
    (("wifi", "wlan", "无线"), "setting", {"namespace": "global", "key": "wifi_on"}),
    (("bluetooth", "蓝牙", "bt"), "setting", {"namespace": "global", "key": "bluetooth_on"}),
    (("brightness", "亮度"), "setting", {"namespace": "system", "key": "screen_brightness"}),
    # Static/visual intents first: "静音时振动已关闭" is a setting, not a volume read.
    (("静音时振动", "vibrate when ringing", "振动"), "setting", {"namespace": "system", "key": "vibrate_when_ringing"}),
    # Ring-stream volume is the decisive signal for mute/silent; aliases keep the
    # emitted tag short (``[probe: volume ring]``).
    (("静音", "mute", "silent", "volume", "音量", "音响", "audible", "rintone", "ringtone", "ringer", "响铃", "铃声"),
     "volume", {"stream": "ring"}),
    (("battery", "电量", "电池", "percentage", "充电"), "battery", {}),
    (("foreground", "前台", "foreground app", "当前应用", "on screen"), "foreground", {}),
    (("alarm", "闹钟"), "alarms", {}),
    (("notification", "通知"), "notifications", {}),
    (("package", "installed", "已安装", "安装"), "packages", {}),
]


def _parse_probe_hint(text: str) -> list[tuple[str, dict]]:
    """Extract explicit probe hints from check-item text.

    Accepts ``[probe: audio]``, ``[probe: setting global zen_mode]``,
    ``[probe: setting, global, zen_mode]``.  Malformed hints are ignored
    (the caller falls back to keyword matching).
    """
    found: list[tuple[str, dict]] = []
    for m in _PROBE_HINT_RE.finditer(text):
        body = m.group(1).strip()
        parts = [p.strip() for p in re.split(r"[,\s]+", body) if p.strip()]
        if not parts:
            continue
        kind = parts[0]
        if kind not in PROBES:
            continue
        params: dict = {}
        spec = PROBES[kind]
        expected = [p.name for p in spec.params]
        if len(parts) - 1 <= len(expected):
            for name, value in zip(expected, parts[1:]):
                params[name] = value
        found.append((kind, params))
    return found


def derive_probes(check_text: str) -> list[tuple[str, dict]]:
    """Return the list of (kind, params) probes that could judge ``check_text``.

    Explicit ``[probe: ...]`` hints win; otherwise keyword matching maps common
    state intents to read-only probes.  Empty list means "no probe derivable".
    """
    explicit = _parse_probe_hint(check_text)
    if explicit:
        return explicit

    lowered = check_text.lower()
    for keywords, kind, params in KEYWORD_PROBE_RULES:
        for kw in keywords:
            if kw in lowered:
                return [(kind, dict(params))]
    return []


# ---------------------------------------------------------------------------
# Output compaction (keep probe text dense but bounded)
# ---------------------------------------------------------------------------

_PROBE_OUTPUT_LIMIT = 4000
#: For verbose dumps (audio, activities), keep only lines matching these
#: patterns so the Checker sees the decisive state without a wall of text.
_KEEP_LINES: dict[str, re.Pattern] = {
    "audio": re.compile(r"ring(er)?[_-]?mode|stream|volume|mode|audio_mode|state", re.IGNORECASE),
    "foreground": re.compile(r"mResumedActivity|mFocusedApp|mCurrentFocus|topResumedActivity|ResumedActivity", re.IGNORECASE),
    "battery": re.compile(r"level|status|present|charged|AC powered|USB powered", re.IGNORECASE),
    # ``media volume --get`` prints just the integer; keep any digit line.
    "volume": re.compile(r"\d", re.IGNORECASE),
}


def summarize_probe_output(kind: str, text: str) -> str:
    """Compress verbose probe output to the decisive lines, then truncate."""
    if not text:
        return "(no output)"
    keep = _KEEP_LINES.get(kind)
    if keep:
        lines = [ln for ln in text.splitlines() if keep.search(ln)]
        if lines:
            text = "\n".join(lines)
    if len(text) > _PROBE_OUTPUT_LIMIT:
        text = text[:_PROBE_OUTPUT_LIMIT] + "\n... [output truncated]"
    return text