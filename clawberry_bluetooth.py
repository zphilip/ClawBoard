#!/usr/bin/env python3
"""clawberry_bluetooth.py — Bluetooth PAN internet sharing for ClawBerry.

Workflow
--------
1. Powers on the Bluetooth adapter and makes it permanently discoverable
   and pairable.
2. Registers a DBus "NoInputNoOutput" pairing agent — the phone can pair
   without any confirmation step required on the Pi side.
3. Listens for DBus PropertiesChanged signals:
     • Paired=True  → trust the device, then attempt a PAN connection after
                       a short delay (gives the phone time to enable tethering)
     • Connected=True → logged for info
4. Uses nmcli to bring up the Bluetooth PAN (NAP profile) so the Pi gets
   internet from the phone's Bluetooth tethering.
5. Re-asserts discoverability every 5 minutes (BlueZ resets it on some builds).

Phone setup
-----------
Android : Settings → Connections → Mobile Hotspot & Tethering
            → enable "Bluetooth Tethering"  (can be done before OR after pairing)
iOS     : Bluetooth PAN tethering is supported on iOS but may need a nmcli
          connection type of 'panu'. The script tries both automatically.

Requirements (standard Raspberry Pi OS Bookworm / Bullseye)
------------------------------------------------------------
    sudo apt install bluez python3-dbus python3-gi network-manager

Run
---
    sudo python3 /opt/clawboard/clawberry_bluetooth.py

Or install as a systemd service (see daemon/ directory).
"""

import os
import sys
import time
import signal
import logging
import subprocess
import threading

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [BT] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('clawberry_bt')

# ── Constants ──────────────────────────────────────────────────────────────
BT_SERVICE           = 'org.bluez'
BT_ADAPTER_PATH      = '/org/bluez/hci0'
BT_AGENT_PATH        = '/clawberry/bt_agent'
DISCOVERABLE_TIMEOUT = 0     # 0 = stay discoverable indefinitely
REDISCOVER_INTERVAL  = 300   # re-assert discoverable every 5 min (seconds)
PAN_CONNECT_DELAY    = 4.0   # seconds to wait after pairing before PAN connect

# ── DBus adapter helpers ───────────────────────────────────────────────────

def _adapter_props():
    bus = dbus.SystemBus()
    return dbus.Interface(
        bus.get_object(BT_SERVICE, BT_ADAPTER_PATH),
        'org.freedesktop.DBus.Properties',
    )


def _set_adapter(key, value):
    try:
        _adapter_props().Set('org.bluez.Adapter1', key, value)
    except dbus.DBusException as exc:
        log.warning("Could not set adapter %s: %s", key, exc)


def bt_power_on():
    _set_adapter('Powered', dbus.Boolean(True))
    log.info("Bluetooth adapter powered on")


def bt_discoverable(on: bool = True):
    _set_adapter('Discoverable',        dbus.Boolean(on))
    _set_adapter('DiscoverableTimeout', dbus.UInt32(DISCOVERABLE_TIMEOUT))
    _set_adapter('Pairable',            dbus.Boolean(True))
    _set_adapter('PairableTimeout',     dbus.UInt32(0))
    log.info("Bluetooth discoverable=%s pairable=True", on)


def bt_trust_device(mac: str):
    """Mark device as trusted so it can reconnect without re-pairing."""
    try:
        bus = dbus.SystemBus()
        dev_path = BT_ADAPTER_PATH + '/dev_' + mac.replace(':', '_')
        dbus.Interface(
            bus.get_object(BT_SERVICE, dev_path),
            'org.freedesktop.DBus.Properties',
        ).Set('org.bluez.Device1', 'Trusted', dbus.Boolean(True))
        log.info("Device %s marked trusted", mac)
    except dbus.DBusException as exc:
        log.warning("Could not trust %s: %s", mac, exc)


# ── NoInputNoOutput pairing agent ─────────────────────────────────────────
# With this agent registered, phones will pair with zero interaction on the
# Pi — no PIN prompt, no passkey confirmation.

AGENT_IFACE = 'org.bluez.Agent1'


class AutoPairAgent(dbus.service.Object):
    """Accepts every pairing request automatically."""

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        log.info("Agent: Release")

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        log.info("Agent: AuthorizeService device=%s uuid=%s — approved", device, uuid)

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        log.info("Agent: RequestPinCode device=%s — returning 0000", device)
        return '0000'

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        log.info("Agent: RequestPasskey device=%s — returning 0", device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        log.info("Agent: DisplayPasskey device=%s passkey=%06d", device, passkey)

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pincode):
        log.info("Agent: DisplayPinCode device=%s pin=%s", device, pincode)

    @dbus.service.method(AGENT_IFACE, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        log.info("Agent: RequestConfirmation device=%s passkey=%06d — confirmed", device, passkey)
        # Do NOT raise — returning normally means "confirmed"

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='')
    def RequestAuthorization(self, device):
        log.info("Agent: RequestAuthorization device=%s — approved", device)

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Cancel(self):
        log.info("Agent: Cancel")


# ── nmcli / PAN helpers ────────────────────────────────────────────────────

def _nmcli(*args):
    cmd = ['nmcli'] + list(str(a) for a in args)
    log.debug("$ %s", ' '.join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.stdout.strip():
            log.debug("nmcli stdout: %s", r.stdout.strip())
        if r.returncode != 0 and r.stderr.strip():
            log.debug("nmcli stderr: %s", r.stderr.strip())
        return r
    except Exception as exc:
        log.warning("nmcli exception: %s", exc)
        return None


def _check_internet():
    try:
        subprocess.run(
            ['ping', '-c', '1', '-W', '3', '8.8.8.8'],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        return False


def connect_pan(mac: str):
    """Try to establish a Bluetooth PAN (NAP) connection via nmcli.

    Attempts (in order):
      1. nmcli device connect <mac>          – works if NM already knows type
      2. Create a 'panu' connection profile  – explicit Bluetooth PAN profile
      3. Create a 'dun'  connection profile  – Dial-Up Networking fallback
    """
    log.info("PAN: attempting connection to %s ...", mac)
    safe_mac = mac.replace(':', '')

    # ── Strategy 1: direct device connect ──────────────────────────────────
    r = _nmcli('device', 'connect', mac)
    if r and r.returncode == 0:
        log.info("✅ PAN connected to %s (strategy 1)", mac)
        _announce_internet(mac)
        return True

    # ── Strategy 2: explicit panu profile ──────────────────────────────────
    profile_panu = f'bt-panu-{safe_mac}'
    # Remove stale profile if it exists
    _nmcli('connection', 'delete', profile_panu)
    _nmcli('connection', 'add',
           'type',              'bluetooth',
           'con-name',          profile_panu,
           'bluetooth.bdaddr',  mac,
           'bluetooth.type',    'panu')
    r2 = _nmcli('connection', 'up', profile_panu)
    if r2 and r2.returncode == 0:
        log.info("✅ PAN connected to %s (strategy 2 / panu)", mac)
        _announce_internet(mac)
        return True

    # ── Strategy 3: dun profile (some iOS / older Android) ─────────────────
    profile_dun = f'bt-dun-{safe_mac}'
    _nmcli('connection', 'delete', profile_dun)
    _nmcli('connection', 'add',
           'type',              'bluetooth',
           'con-name',          profile_dun,
           'bluetooth.bdaddr',  mac,
           'bluetooth.type',    'dun')
    r3 = _nmcli('connection', 'up', profile_dun)
    if r3 and r3.returncode == 0:
        log.info("✅ PAN connected to %s (strategy 3 / dun)", mac)
        _announce_internet(mac)
        return True

    log.warning("⚠️ All PAN strategies failed for %s", mac)
    return False


def _announce_internet(mac: str):
    """Log whether internet is actually reachable after PAN connect."""
    # Give the interface a moment to get a DHCP lease
    time.sleep(3)
    if _check_internet():
        log.info("🌐 Internet reachable via Bluetooth tethering from %s", mac)
    else:
        log.warning("⚠️ PAN link up but internet not yet reachable (phone tethering enabled?)")


def _handle_paired(mac: str):
    """Called in a background thread after a device pairs."""
    bt_trust_device(mac)
    log.info("Waiting %.1fs before PAN connect (phone may need to enable tethering) ...",
             PAN_CONNECT_DELAY)
    time.sleep(PAN_CONNECT_DELAY)
    connect_pan(mac)


# ── DBus signal listener ───────────────────────────────────────────────────

def _on_properties_changed(interface, changed, invalidated, path=None):
    if interface != 'org.bluez.Device1':
        return

    # Extract MAC from DBus path: /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF
    try:
        dev_part = path.split('/')[-1]           # dev_AA_BB_CC_DD_EE_FF
        mac = dev_part[4:].replace('_', ':')     # AA:BB:CC:DD:EE:FF
    except Exception:
        return

    paired    = changed.get('Paired')
    connected = changed.get('Connected')
    name      = changed.get('Alias') or changed.get('Name') or ''

    if paired:
        log.info("📱 Device paired: %s  %s", mac, name)
        threading.Thread(target=_handle_paired, args=[mac], daemon=True).start()
    elif connected:
        log.info("🔗 Device connected: %s  %s", mac, name)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    # Power on + make discoverable
    bt_power_on()
    bt_discoverable(True)

    # Register the auto-accept pairing agent
    agent = AutoPairAgent(bus, BT_AGENT_PATH)
    agent_mgr = dbus.Interface(
        bus.get_object(BT_SERVICE, '/org/bluez'),
        'org.bluez.AgentManager1',
    )
    agent_mgr.RegisterAgent(BT_AGENT_PATH, 'NoInputNoOutput')
    agent_mgr.RequestDefaultAgent(BT_AGENT_PATH)
    log.info("Auto-pair agent registered (NoInputNoOutput)")

    # Subscribe to device property changes (Paired / Connected)
    bus.add_signal_receiver(
        _on_properties_changed,
        dbus_interface='org.freedesktop.DBus.Properties',
        signal_name='PropertiesChanged',
        path_keyword='path',
        bus_name=BT_SERVICE,
    )

    log.info("📲  Bluetooth ready — scan for this device from your phone and pair.")
    log.info("    On Android: enable Bluetooth Tethering in hotspot settings.")

    loop = GLib.MainLoop()

    # Periodically re-assert discoverability (BlueZ may reset it)
    def _keep_discoverable():
        bt_discoverable(True)
        return True   # returning True keeps the GLib timer repeating

    GLib.timeout_add_seconds(REDISCOVER_INTERVAL, _keep_discoverable)

    def _shutdown(signum, frame):
        log.info("Signal %s received — shutting down", signum)
        bt_discoverable(False)
        try:
            agent_mgr.UnregisterAgent(BT_AGENT_PATH)
        except Exception:
            pass
        loop.quit()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    loop.run()


if __name__ == '__main__':
    if os.geteuid() != 0:
        log.error("This script must be run as root: sudo python3 %s", __file__)
        sys.exit(1)
    main()
