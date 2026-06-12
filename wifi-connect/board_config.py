#!/usr/bin/env python3
"""
board_config.py
===============
Shared board detection and GPIO helpers for Radxa Cubie boards.

Used by both ``wifi-connect-gpio-launch-radxa.py`` and ``gpio-button-test.py``
so the two scripts stay in sync across A7A / A7Z / future Radxa boards.

Usage as a module::

    from board_config import detect_board, get_button_pin, open_button, scan_free_lines

Usage as a script (prints the detected board and default pin)::

    sudo python3 board_config.py
"""

import os
import glob
import fcntl
import struct
import sys

# ── ioctl constants (gpio character device v1) ─────────────────────────────────
_GPIO_GET_CHIPINFO_IOCTL = 0x8044B401   # _IOR(0xB4, 0x01, 68)  gpiochip_info
_GPIO_GET_LINEINFO_IOCTL = 0xC048B402   # _IOWR(0xB4, 0x02, 72) gpioline_info
_GPIOLINE_FLAG_IS_OUT     = 1 << 0
_GPIOLINE_FLAG_ACTIVE_LOW = 1 << 1
_GPIOLINE_FLAG_OPEN_DRAIN = 1 << 2
_GPIOLINE_FLAG_OPEN_SOURCE = 1 << 3
_GPIOLINE_FLAG_KERNEL     = 1 << 4
_GPIOLINE_FLAG_PULL_UP    = 1 << 5
_GPIOLINE_FLAG_PULL_DOWN  = 1 << 6


# ── Board detection ────────────────────────────────────────────────────────────

# (chip_path, line_offset, label) for the recommended default button pin per board.
BUTTON_PINS = {
    'a7a':   ('/dev/gpiochip0', 32,  'PIN_7'),
    'a7z':   ('/dev/gpiochip1', 35,  'PIN_33'),
    'radxa': ('/dev/gpiochip1', 35,  'PIN_33'),   # generic Radxa fallback → A7Z mapping
}


def _chip_info(chip_path):
    """Return (name, label, ngpio) for a gpiochip device."""
    with open(chip_path, 'rb') as f:
        buf = fcntl.ioctl(f, _GPIO_GET_CHIPINFO_IOCTL, b'\x00' * 68)
    name  = buf[0:32].rstrip(b'\x00').decode(errors='replace')
    label = buf[32:64].rstrip(b'\x00').decode(errors='replace')
    ngpio = struct.unpack_from('<I', buf, 64)[0]
    return name, label, ngpio


def _line_info(chip_path, offset):
    """Return (line_name, consumer, flags) for one GPIO line."""
    req = struct.pack('<I', offset) + b'\x00' * 68
    with open(chip_path, 'rb') as f:
        buf = fcntl.ioctl(f, _GPIO_GET_LINEINFO_IOCTL, req)
    flags    = struct.unpack_from('<I', buf, 0)[0]
    name     = buf[4:36].rstrip(b'\x00').decode(errors='replace')
    consumer = buf[36:68].rstrip(b'\x00').decode(errors='replace')
    return name, consumer, flags


def detect_board():
    """Identify the Radxa board from the device-tree compatible string.

    Returns one of ``'a7a'``, ``'a7z'``, ``'radxa'``, or ``'unknown'``.
    """
    try:
        with open('/proc/device-tree/compatible', 'rb') as f:
            raw = f.read()
        compat = raw.replace(b'\x00', b' ').decode(errors='replace').lower()
    except Exception:
        return 'unknown'

    if 'cubie-a7a' in compat:
        return 'a7a'
    if 'cubie-a7z' in compat:
        return 'a7z'
    if 'radxa' in compat or 'sun60iw2p1' in compat:
        return 'radxa'
    return 'unknown'


def get_button_pin(board=None):
    """Return ``(chip_path, line_offset, label)`` for the board's default button pin.

    If *board* is None it is auto-detected via :func:`detect_board`.
    """
    if board is None:
        board = detect_board()
    if board in BUTTON_PINS:
        return BUTTON_PINS[board]
    # Last-resort fallback — try the A7Z mapping
    return BUTTON_PINS['radxa']


# ── GPIO opener with robust bias fallback ──────────────────────────────────────

def open_button(chip, line):
    """Open a GPIO line as input, preferring internal pull-up.

    Tries ``bias='pull_up'`` first.  If that fails with *any* exception
    (older periphery without the kwarg, or kernel rejecting the bias flag)
    we fall back to a plain input — on some boards internal pull resistors
    are already configured by the pin controller so the line may still work.

    Returns a ``periphery.GPIO`` instance.
    """
    from periphery import GPIO

    biases_to_try = ['pull_up', None]
    last_exc = None

    for bias in biases_to_try:
        try:
            if bias is not None:
                return GPIO(chip, line, 'in', bias=bias)
            else:
                return GPIO(chip, line, 'in')
        except TypeError:
            # Old periphery (no 'bias' kwarg) — plain input is the best we can do
            return GPIO(chip, line, 'in')
        except Exception as exc:
            last_exc = exc
            continue

    raise RuntimeError(
        f"Cannot open {chip} line {line}: {last_exc}"
    )


# ── Free-line scanner ──────────────────────────────────────────────────────────

def scan_free_lines():
    """Yield ``(chip_path, offset, name, consumer, flags)`` for every free input line."""
    chips = sorted(glob.glob('/dev/gpiochip*'),
                   key=lambda p: int(p.replace('/dev/gpiochip', '')))
    for chip_path in chips:
        try:
            _, _, ngpio = _chip_info(chip_path)
            for offset in range(ngpio):
                try:
                    lname, consumer, flags = _line_info(chip_path, offset)
                    is_out    = bool(flags & _GPIOLINE_FLAG_IS_OUT)
                    is_kernel = bool(flags & _GPIOLINE_FLAG_KERNEL)
                    if not is_out and not is_kernel and not consumer:
                        yield chip_path, offset, lname, consumer, flags
                except Exception:
                    pass
        except Exception:
            pass


def list_pins():
    """Print a human-readable table of all free GPIO lines.

    Used by ``--list`` / ``--scan`` flags in the launcher and test scripts.
    """
    board = detect_board()
    _, default_line, _ = get_button_pin(board)

    print(f"Board detected: {board}")
    print()
    print(f"{'CHIP':<18} {'LINE':>4}  {'NAME':<22} {'BIAS':>12} {'BASELINE':>9}  NOTE")
    print('-' * 90)

    for chip_path, offset, lname, consumer, flags in scan_free_lines():
        # Decode bias flags
        if flags & _GPIOLINE_FLAG_PULL_UP:
            bias = 'PULL_UP'
        elif flags & _GPIOLINE_FLAG_PULL_DOWN:
            bias = 'PULL_DOWN'
        else:
            bias = 'NONE'

        # Read the actual level (try with pull-up, fall back to no bias)
        try:
            g = open_button(chip_path, offset)
            val = g.read()
            g.close()
            baseline = 'HIGH' if val else 'LOW'
        except Exception:
            baseline = 'ERR'

        # Flag interesting / problematic lines
        notes = []
        if chip_path.endswith(str(default_line)) and offset == default_line:
            notes.append('★ DEFAULT BUTTON PIN')
        if baseline == 'ERR':
            notes.append('⚠ cannot read')
        elif baseline == 'LOW':
            if flags & _GPIOLINE_FLAG_PULL_DOWN:
                notes.append('⚠ pull-down holds LOW — not usable for active-LOW button')
            else:
                notes.append('resting LOW')

        note_str = '  '.join(notes) if notes else ''
        print(f"{chip_path:<18} {offset:4}  {lname:<22} {bias:>12} {baseline:>9}  {note_str}")

    print()
    print("★  = recommended default for this board (override with RADXA_BTN_CHIP / RADXA_BTN_LINE)")
    print("⚠  = likely won't work as a button input without rewiring or external pull resistor")


# ── CLI (run as script) ────────────────────────────────────────────────────────

if __name__ == '__main__':
    board = detect_board()
    chip, line, label = get_button_pin(board)
    print(f"Board       : {board}")
    print(f"Default pin : {label}")
    print(f"Chip        : {chip}")
    print(f"Line offset : {line}")
    print(f"Use:  RADXA_BTN_CHIP={chip} RADXA_BTN_LINE={line}")
    print()
    if '--list' in sys.argv or '-l' in sys.argv:
        list_pins()
