"""Coordinate scaling, bucketing, and drift-validation helpers.

Extracted from the runner's ``main()`` to reduce its line count and
make these pure functions independently testable.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from utils import find_matching_element

# Round to nearest 100 in 0-1000 normalized space so that coordinate
# jitter (±10-20 units) on the same button collapses to one bucket
# while clicks on different buttons (hundreds of units apart) stay
# distinct.
COORD_BUCKET_SIZE = 100


# ---------------------------------------------------------------------------
# Coordinate scaling
# ---------------------------------------------------------------------------


def rescale_coordinates(
    action_parameter: dict, resized_width: int, resized_height: int,
) -> dict:
    """Convert normalized (0-1000) coordinates to actual pixel coordinates
    based on the resized image dimensions.
    """
    _raw_coords: dict[str, list[int]] = {}
    for key in ("coordinate", "coordinate1", "coordinate2"):
        if key in action_parameter:
            _raw_coords[key] = list(action_parameter[key])
            # Guard against truncated VLM output where a coordinate array
            # has fewer than 2 elements (e.g. "[25" → repaired to "[25]").
            if len(action_parameter[key]) < 2:
                raise ValueError(
                    f"Coordinate key {key!r} has only "
                    f"{len(action_parameter[key])} element(s): "
                    f"{action_parameter[key]!r} — "
                    f"VLM output was likely truncated."
                )
            # Backward compatibility: old memory records may store absolute
            # pixels.  If either axis exceeds 1000, treat as already-resolved.
            if action_parameter[key][0] > 1000 or action_parameter[key][1] > 1000:
                action_parameter[key][0] = int(action_parameter[key][0])
                action_parameter[key][1] = int(action_parameter[key][1])
            else:
                action_parameter[key][0] = int(
                    action_parameter[key][0] / 1000 * resized_width
                )
                action_parameter[key][1] = int(
                    action_parameter[key][1] / 1000 * resized_height
                )
    if _raw_coords:
        print(
            f"[COORD DEBUG] raw={_raw_coords} -> "
            f"scaled={{{', '.join(f'{k}: {action_parameter[k]}' for k in _raw_coords)}}} "
            f"(resized={resized_width}x{resized_height})"
        )
    return action_parameter


# ---------------------------------------------------------------------------
# Coordinate validation
# ---------------------------------------------------------------------------


def action_has_out_of_range_coords(action_args: dict) -> bool:
    """Return True if any coordinate is outside the normalized 0-1000 range."""
    for key in ("coordinate", "coordinate1", "coordinate2"):
        if key not in action_args:
            continue
        value = action_args.get(key)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        try:
            x = float(value[0])
            y = float(value[1])
        except (TypeError, ValueError):
            continue
        if x < 0 or y < 0 or x > 1000 or y > 1000:
            return True
    return False


# ---------------------------------------------------------------------------
# Distance & bucketing
# ---------------------------------------------------------------------------


def normalized_click_distance(a: object, b: object) -> float | None:
    """Euclidean distance between two normalized click coordinates (0-1000)."""
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return None
    if len(a) != 2 or len(b) != 2:
        return None
    try:
        ax = float(a[0]);  ay = float(a[1])
        bx = float(b[0]);  by = float(b[1])
    except (TypeError, ValueError):
        return None
    dx = ax - bx
    dy = ay - by
    return (dx * dx + dy * dy) ** 0.5


def bucket_coord(coord: object) -> str:
    """Round a normalized (0-1000) [x, y] to a coarse bucket string.

    Clicks on different screen regions produce different buckets, while
    jitter on the same button (±10-20 units) collapses to the same bucket.
    Returns ``""`` when the input is not a valid 2-element list/tuple.
    """
    if not isinstance(coord, (list, tuple)) or len(coord) != 2:
        return ""
    try:
        x = round(float(coord[0]) / COORD_BUCKET_SIZE) * COORD_BUCKET_SIZE
        y = round(float(coord[1]) / COORD_BUCKET_SIZE) * COORD_BUCKET_SIZE
    except (TypeError, ValueError):
        return ""
    return f"{int(x)},{int(y)}"


# ---------------------------------------------------------------------------
# Action signatures (for loop detection)
# ---------------------------------------------------------------------------


def bucketed_action_sig(action_type: str, action_args: dict) -> str:
    """Build a relaxed action signature with bucketed coordinates.

    For coordinate-based actions (click, long_press, swipe) appends a
    bucketed coordinate suffix so taps on different screen regions produce
    different signatures.  For non-coordinate actions returns just the
    action type.
    """
    coord_keys = ("coordinate", "coordinate1", "coordinate2")
    buckets: list[str] = []
    for key in coord_keys:
        if key in action_args:
            b = bucket_coord(action_args[key])
            if b:
                buckets.append(b)
    if buckets:
        return f"{action_type}|{'|'.join(buckets)}"
    return action_type


# ---------------------------------------------------------------------------
# Coordinate-drift validation (for memory fastpath)
# ---------------------------------------------------------------------------


def validate_coordinate_drift(
    cached_action_args: dict,
    target_element_sig: Optional[dict],
    current_ui_xml: str,
    current_screenshot_path: str,
    max_drift_threshold: float = 120.0,
    *,
    log_func: Callable[[str], None] | None = None,
) -> bool:
    """Check that cached coords are still close to the target element."""
    if not target_element_sig or not current_ui_xml:
        return True  # cannot validate — assume valid

    def _log(msg: str) -> None:
        if log_func:
            log_func(msg)

    try:
        current_element = find_matching_element(target_element_sig, current_ui_xml)
        if not current_element:
            return False

        cached_coord = cached_action_args.get("coordinate", [0, 0])

        from PIL import Image
        img = Image.open(current_screenshot_path)
        current_width, current_height = img.size

        bounds = current_element["bounds"]
        center_x = (bounds[0] + bounds[2]) // 2
        center_y = (bounds[1] + bounds[3]) // 2
        normalized_current_x = center_x * 1000 / current_width
        normalized_current_y = center_y * 1000 / current_height

        drift_distance = normalized_click_distance(
            [normalized_current_x, normalized_current_y],
            cached_coord,
        )

        if drift_distance is not None and drift_distance <= max_drift_threshold:
            return True
        else:
            _log(f"[MEMORY] Coordinate drift too large: {drift_distance} > {max_drift_threshold}")
            return False

    except Exception as e:
        _log(f"[MEMORY] Error validating coordinate drift: {e}")
        return False


# ---------------------------------------------------------------------------
# Relaxed cycle detection (for loop detector)
# ---------------------------------------------------------------------------


def detect_relaxed_cycle(
    seq: list[str],
    min_period: int = 2,
    max_period: int = 4,
) -> tuple[bool, int, list[str]]:
    """Detect whether the tail of *seq* forms a repeated cycle pattern."""
    n = len(seq)
    for period in range(min_period, max_period + 1):
        if n < period * 2:
            continue
        tail1 = seq[-period:]
        tail2 = seq[-2 * period:-period]
        if tail1 == tail2:
            return True, period, tail1
    return False, 0, []
