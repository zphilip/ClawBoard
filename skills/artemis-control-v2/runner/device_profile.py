"""Per-device state baseline and profile cache.

Why this exists: the VLM decides *what to click* from pixels, and it cannot know
a device-specific truth such as "on this ROM the ring stream never reads 0 while
the ringer mode is NORMAL" or "DND is what actually silences it". That knowledge
is not general Android lore to be searched for — it is DEVICE STATE, and the only
authoritative source is the device itself.

So before planning we take a cheap read-only snapshot:

* the run gets a **baseline** to compare against, which makes relative criteria
  judgeable ("调低亮度" = lower than the value we measured at start);
* the same snapshot answers "is the goal already satisfied?" before a single
  step is spent;
* the facts are cached per device fingerprint so later runs start informed
  instead of re-deriving the same quirks.

Nothing here is task-specific: it only reads the enumerated, regex-validated
settings/streams a probe is allowed to read.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

#: (namespace, key) pairs that describe muting / audio / display state.
_SETTING_KEYS: tuple[tuple[str, str], ...] = (
    ("global", "zen_mode"),
    ("system", "mode_ringer"),
    ("system", "vibrate_when_ringing"),
    ("global", "wifi_on"),
    ("global", "airplane_mode_on"),
    ("global", "bluetooth_on"),
    ("system", "screen_brightness"),
    ("system", "screen_brightness_mode"),
)

#: (label, stream id) for the audio streams worth baselining.
_VOLUME_STREAMS: tuple[tuple[str, str], ...] = (
    ("ring", "2"),
    ("music", "3"),
    ("alarm", "4"),
    ("notification", "5"),
)

_PROFILE_DIR = Path(__file__).resolve().parent.parent / "memory" / "data" / "device_profiles"


@dataclass
class DeviceProfile:
    """A read-only snapshot of the device's relevant state."""

    fingerprint: str = "default"
    model: str = ""
    sdk: Optional[int] = None
    captured_at: str = ""
    #: stream name → {"value": int, "min": int, "max": int}
    volumes: dict[str, dict[str, int]] = field(default_factory=dict)
    #: namespace.key → int (or raw string when non-numeric)
    settings: dict[str, object] = field(default_factory=dict)
    ringer_mode: Optional[int] = None
    zen_mode: Optional[int] = None

    # ── derived, task-agnostic consequences ────────────────────────────
    @property
    def ring_value(self) -> Optional[int]:
        v = self.volumes.get("ring") or {}
        return v.get("value")

    @property
    def ring_min(self) -> Optional[int]:
        v = self.volumes.get("ring") or {}
        return v.get("min")

    @property
    def ring_max(self) -> Optional[int]:
        v = self.volumes.get("ring") or {}
        return v.get("max")

    @property
    def ring_range(self) -> str:
        """``"1 of [0..7]"`` — the denominator matters as much as the value."""
        val, lo, hi = self.ring_value, self.ring_min, self.ring_max
        if val is None:
            return "?"
        if lo is None or hi is None:
            return f"{val} of [?..?]"
        return f"{val} of [{lo}..{hi}]"

    @property
    def ring_is_at_minimum(self) -> bool:
        """True when the ring stream already sits on its lower bound.

        On ROMs that clamp STREAM_RING to >= 1 in NORMAL mode this is the
        *effective* minimum: dragging the slider further does nothing, and a
        volume-only acceptance criterion is unreachable.
        """
        val, lo = self.ring_value, self.ring_min
        return val is not None and lo is not None and val <= lo

    def baseline_for(self, key: str) -> Optional[int]:
        """Numeric baseline for a subject key (see ``runner.acceptance``)."""
        if key == "screen_brightness":
            return _as_int(self.settings.get("system.screen_brightness"))
        if key in ("ring_volume", "volume"):
            return self.ring_value
        if key == "zen_mode":
            return self.zen_mode
        return _as_int(self.settings.get(key) if "." not in key else self.settings.get(key))

    def describe_for_prompt(self) -> str:
        """Compact, factual block for the planner — measured, not remembered."""
        lines = ["## Device facts (measured read-only before planning)"]
        if self.model:
            lines.append(f"- model: {self.model}" + (f" (sdk {self.sdk})" if self.sdk else ""))
        for name, v in self.volumes.items():
            lines.append(
                f"- {name} volume: {v.get('value')} of range "
                f"[{v.get('min')}..{v.get('max')}]"
            )
        if self.ringer_mode is not None:
            mode_name = {0: "SILENT", 1: "VIBRATE", 2: "NORMAL"}.get(self.ringer_mode, "?")
            lines.append(f"- ringer mode: {self.ringer_mode} ({mode_name})")
        if self.zen_mode is not None:
            lines.append(
                f"- do-not-disturb zen_mode: {self.zen_mode}"
                + (" (OFF)" if self.zen_mode == 0 else " (ACTIVE — silences sound)")
            )
        for key, val in self.settings.items():
            if key in ("global.zen_mode",):
                continue
            lines.append(f"- {key}: {val}")
        if self.ring_is_at_minimum:
            lines.append(
                "- NOTE: the ring stream is ALREADY at its lower bound. A "
                "criterion that requires the ring volume to reach 0 may be "
                "unreachable on this ROM; satisfying it via the ringer mode or "
                "DND is equally valid."
            )
        lines.append(
            "- These are measurements, not assumptions. Prefer them over "
            "estimating what the device's state is."
        )
        return "\n".join(lines)


def _as_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_volume(out: str) -> dict[str, int]:
    """Parse ``media volume --stream N --get``.

    Two output shapes exist: the bare integer (AOSP) and a verbose form
    (``[v] volume is 1 in range [0..7]``). Parse both; an unparseable shape
    simply yields no entry rather than a wrong number.
    """
    import re

    info: dict[str, int] = {}
    m = re.search(r"volume is (-?\d+)\s+in range \[(-?\d+)\.\.(-?\d+)\]", out)
    if m:
        info = {"value": int(m.group(1)), "min": int(m.group(2)), "max": int(m.group(3))}
    else:
        m = re.search(r"^\s*(-?\d+)\s*$", out.strip())
        if m:
            info = {"value": int(m.group(1))}
    return info


def capture(
    run: Callable[[list[str]], tuple[int, str, str]],
    *,
    adb_path: str = "adb",
    device: Optional[str] = None,
) -> DeviceProfile:
    """Take a read-only baseline snapshot.

    ``run`` executes an ``adb shell`` argv and returns (rc, stdout, stderr); it is
    injected so this module stays independent of the agent and is unit-testable.
    """
    prof = DeviceProfile(captured_at=datetime.now().isoformat(timespec="seconds"))

    rc, model, _ = run(["shell", "getprop", "ro.product.model"])
    if rc == 0:
        prof.model = model.strip()
    rc, sdk, _ = run(["shell", "getprop", "ro.build.version.sdk"])
    num = _as_int(sdk)
    if rc == 0 and num is not None:
        prof.sdk = num
    prof.fingerprint = _fingerprint(prof.model, prof.sdk)

    for name, stream in _VOLUME_STREAMS:
        rc, out, _ = run(["shell", "media", "volume", "--stream", stream, "--get"])
        if rc == 0:
            info = _parse_volume(out)
            if info:
                prof.volumes[name] = info

    for ns, key in _SETTING_KEYS:
        rc, out, _ = run(["shell", "settings", "get", ns, key])
        if rc != 0:
            continue
        raw = (out or "").strip()
        if not raw or raw == "null":
            continue
        prof.settings[f"{ns}.{key}"] = _as_int(raw) if _as_int(raw) is not None else raw

    rc, out, _ = run(["shell", "dumpsys", "audio"])
    if rc == 0:
        prof.ringer_mode, prof.zen_mode = _parse_audio(out)

    # ``dumpsys audio`` spells the ringer mode in ways that vary by ROM — some
    # print nothing at all after the colon. The setting is the documented
    # writable mirror of the same state, so use it as a fallback.
    if prof.ringer_mode is None:
        for ns in ("global", "system", "secure"):
            rc, out, _ = run(["shell", "settings", "get", ns, "mode_ringer"])
            if rc == 0:
                num = _as_int(out)
                if num is not None and 0 <= num <= 2:
                    prof.ringer_mode = num
                    break

    # ``0`` is a MEANINGFUL zen value (DND off), so it must be assigned
    # explicitly — `a or b` silently discards it and reports "unknown".
    zen_setting = _as_int(prof.settings.get("global.zen_mode"))
    if zen_setting is not None:
        prof.zen_mode = zen_setting
    return prof


def _fingerprint(model: str, sdk: Optional[int]) -> str:
    base = (model or "unknown").strip().lower().replace(" ", "_")
    return f"{base}_sdk{sdk}" if sdk else base


def _parse_audio(out: str):
    """Ringer mode + zen mode from ``dumpsys audio`` (numeric AND word form).

    Four shapes are seen in the field, so all four are parsed:
      ``mRingerMode=2``            (AOSP dump)
      ``Ringer mode: normal``      (word form)
      ``Ringer mode: 2``           (numeric, other dumps)
      ``Ringer mode:``             (EMPTY value — several ColorOS builds)
    The last one is why an empty match must yield ``None`` rather than a default:
    reporting a made-up NORMAL would silently contradict the real state.
    """
    import re

    ringer = None
    m = re.search(r"mRingerMode\s*=\s*(\d+)", out)
    if m:
        ringer = int(m.group(1))
    else:
        m = re.search(r"ringer\s*mode\s*[:=]\s*([a-z_]+)", out, re.IGNORECASE)
        if m:
            ringer = {
                "silent": 0, "vibrate": 1, "normal": 2,
                "ringer_mode_silent": 0, "ringer_mode_vibrate": 1,
                "ringer_mode_normal": 2,
            }.get(m.group(1).lower())
        else:
            m = re.search(r"ringer\s*mode\s*[:=]\s*([0-2])\b", out, re.IGNORECASE)
            if m:
                ringer = int(m.group(1))

    zen = None
    m = re.search(r"zen[ _]mode\s*[:=]\s*(zen_mode_[a-z_]+)", out, re.IGNORECASE)
    if m:
        zen = {
            "zen_mode_off": 0,
            "zen_mode_important_interruptions": 1,
            "zen_mode_no_interruptions": 2,
            "zen_mode_alarms": 3,
            "zen_mode_priority": 1,
            "zen_mode_none": 0,
            "zen_mode_all": 0,
        }.get(m.group(1).lower())
    if zen is None:
        m = re.search(r"zen[ _]mode\s*[:=]\s*(-?\d+)", out, re.IGNORECASE)
        if m:
            zen = int(m.group(1))
    return ringer, zen


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def profile_path(fingerprint: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in fingerprint)
    return _PROFILE_DIR / f"{safe}.json"


def load(fingerprint: str) -> Optional[DeviceProfile]:
    p = profile_path(fingerprint)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    prof = DeviceProfile()
    for k, v in data.items():
        if hasattr(prof, k):
            setattr(prof, k, v)
    return prof


def save(prof: DeviceProfile) -> None:
    """Persist the profile. Best-effort: a read-only FS must not break a run."""
    try:
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        profile_path(prof.fingerprint).write_text(
            json.dumps(asdict(prof), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def load_or_capture(
    run: Callable[[list[str]], tuple[int, str, str]],
    *,
    adb_path: str = "adb",
    device: Optional[str] = None,
    refresh: bool = True,
) -> DeviceProfile:
    """Always capture fresh state; the cache only adds cross-run hints.

    The *state* (volume, zen, brightness) must be current, so a cached profile is
    never reused as the baseline. The cache exists for the cheaper cross-run
    knowledge (which shapes/limits this device has), which callers can read with
    :func:`load`.
    """
    prof = capture(run, adb_path=adb_path, device=device)
    save(prof)
    return prof
