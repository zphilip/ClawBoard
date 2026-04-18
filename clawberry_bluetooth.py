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

# Bluetooth PAN / NAP service UUID — when the phone offers this, it is
# sharing internet via Bluetooth tethering
PAN_NAP_UUID  = '00001116-0000-1000-8000-00805f9b34fb'
PANU_UUID     = '00001115-0000-1000-8000-00805f9b34fb'

# Track MACs currently being connected to avoid duplicate threads
_pan_connecting: set = set()
_pan_lock = threading.Lock()   # guards _pan_connecting against race conditions
_pan_success:  set = set()     # MACs that have successfully PAN-connected at least once
_pan_cooldown: dict = {}       # {mac: monotonic deadline} — ignore Connected signals after success
PAN_COOLDOWN_SECONDS = 90      # seconds to suppress re-connect after a successful PAN
_last_bt_event: float = 0.0   # monotonic time of last Paired/Connected/Disconnected signal

WATCHDOG_INTERVAL  = 120   # seconds between adapter health checks
WATCHDOG_IDLE_SECS = 180   # reset adapter if no BT signals for this long

# Pairing agent references — kept globally so _bt_adapter_reset can re-register
# after a bluetoothd restart (new process loses all agent registrations).
_agent_obj     = None   # AutoPairAgent instance
_agent_mgr_obj = None   # org.bluez.AgentManager1 proxy

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


def bt_remove_device(mac: str):
    """Remove (forget) a paired device so the phone can re-pair cleanly.

    Called automatically when PAN connect fails after all retries, so the
    stale bonding key is cleared.  Next time the phone tries to pair, BlueZ
    will treat it as a brand-new device — no "wrong passkey" error.
    """
    try:
        bus      = dbus.SystemBus()
        adapter  = dbus.Interface(
            bus.get_object(BT_SERVICE, BT_ADAPTER_PATH),
            'org.bluez.Adapter1',
        )
        dev_path = BT_ADAPTER_PATH + '/dev_' + mac.replace(':', '_')
        adapter.RemoveDevice(dbus.ObjectPath(dev_path))   # must be ObjectPath, not str
        log.info("🗑️  Removed stale bonding for %s — phone can pair fresh", mac)
    except dbus.DBusException as exc:
        err = str(exc)
        if 'DoesNotExist' in err or 'UnknownObject' in err:
            log.debug("Device %s already removed (DoesNotExist — harmless)", mac)
        else:
            log.warning("Could not remove device %s: %s", mac, exc)


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
        # Trust the device immediately so it can reconnect without re-pairing
        try:
            dev_part = str(device).split('/')[-1]
            mac = dev_part[4:].replace('_', ':')
            bt_trust_device(mac)
        except Exception as exc:
            log.warning("AuthorizeService trust error: %s", exc)
        uuid_lower = str(uuid).lower()
        if uuid_lower in (PAN_NAP_UUID, PANU_UUID):
            try:
                dev_part = str(device).split('/')[-1]
                mac = dev_part[4:].replace('_', ':')
                log.info("PAN service UUID detected for %s — scheduling PAN connect", mac)
                threading.Thread(target=_handle_connected, args=[mac], daemon=True).start()
            except Exception as exc:
                log.warning("AuthorizeService PAN dispatch error: %s", exc)

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
            log.info("nmcli stderr: %s", r.stderr.strip())
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


def _request_dhcp(iface: str):
    """Request a DHCP lease on *iface*.

    BlueZ Network1.Connect() creates the bnep0 interface but does not
    request a DHCP lease.  We must do it explicitly.
    Tries dhclient first (Debian/Ubuntu), then dhcpcd (Raspberry Pi OS).
    """
    for cmd in (['dhclient', '-v', iface], ['dhcpcd', iface]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                log.info('DHCP lease obtained on %s via %s', iface, cmd[0])
                return
            log.debug('%s %s: %s', cmd[0], iface,
                      r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'failed')
        except FileNotFoundError:
            continue   # binary not installed, try next
        except Exception as exc:
            log.debug('_request_dhcp %s error: %s', iface, exc)
    # Last resort: ask NetworkManager to manage the bare interface —
    # but only if it still exists (dhcpcd removes it on carrier loss).
    if os.path.exists(f'/sys/class/net/{iface}'):
        try:
            subprocess.run(
                ['nmcli', 'device', 'connect', iface],
                capture_output=True, text=True, timeout=20
            )
            log.info('Asked NM to connect interface %s', iface)
        except Exception as exc:
            log.debug('nmcli device connect %s error: %s', iface, exc)
    else:
        log.debug('Interface %s already gone — skipping NM fallback', iface)


def _cleanup_bnep():
    """Force-delete any stale bnep* kernel interfaces.

    After rapid BNEP connect/disconnect cycles the kernel module can leave
    bnep0 in a zombie state that causes BlueZ Network1.Connect() to return
    'Input/output error' indefinitely.  Nuking it at the kernel level before
    each connect attempt gives BlueZ a clean slate.
    """
    try:
        for entry in os.scandir('/sys/class/net'):
            if entry.name.startswith('bnep'):
                subprocess.run(['ip', 'link', 'delete', entry.name],
                               capture_output=True, timeout=5)
                log.info('Cleaned up stale %s interface', entry.name)
                time.sleep(0.1)
    except FileNotFoundError:
        pass   # /sys/class/net vanished — harmless
    except Exception as exc:
        log.debug('_cleanup_bnep: %s', exc)


def _bt_adapter_reset():
    """Power-cycle the BT adapter to clear stuck BNEP/HCI state.

    After many rapid BNEP cycles the kernel HCI layer can stop surfacing
    Paired/Connected signals entirely.  A D-Bus power-off→on cycle resets
    the internal state in ~3 s without restarting bluetoothd.
    Falls back to 'systemctl restart bluetooth' if D-Bus is unresponsive.
    """
    global _last_bt_event
    log.warning('🔄 BT adapter stuck — power-cycling to recover ...')
    _pan_cooldown.clear()
    _cleanup_bnep()
    try:
        for _attempt in range(6):
            try:
                _adapter_props().Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(False))
                log.debug('Adapter powered off for reset (attempt %d)', _attempt + 1)
                break
            except dbus.DBusException as _exc:
                log.debug('Reset power-off attempt %d: %s', _attempt + 1, _exc)
                time.sleep(1)
        time.sleep(2)
        _set_adapter('Powered', dbus.Boolean(True))
        time.sleep(1)
        bt_discoverable(True)
        _last_bt_event = time.monotonic()   # prevent immediate watchdog re-trigger
        log.info('🔄 BT adapter reset complete — discoverable again')
    except Exception as exc:
        log.warning('D-Bus adapter reset failed (%s) — restarting bluetoothd', exc)
        try:
            subprocess.run(['systemctl', 'restart', 'bluetooth'],
                           capture_output=True, timeout=15)
            time.sleep(5)
            bt_power_on()
            bt_discoverable(True)
            _reregister_agent()   # critical: new bluetoothd has no agent registered
            _last_bt_event = time.monotonic()
            log.info('🔄 bluetoothd restarted — discoverable again')
        except Exception as exc2:
            log.error('Could not restart bluetooth: %s', exc2)


def _bt_watchdog():
    """GLib timer callback: health-check the BT adapter and reset if stuck.

    Two failure modes are detected:
      1. BlueZ D-Bus stops responding entirely (adapter truly dead).
      2. D-Bus responds but no Paired/Connected/Disconnected signals have
         been received for WATCHDOG_IDLE_SECS — HCI layer zombie state.

    Returns True so GLib keeps calling this at WATCHDOG_INTERVAL.
    """
    # Liveness probe — a frozen bluetoothd will not respond to this
    try:
        powered = _adapter_props().Get('org.bluez.Adapter1', 'Powered')
        if not bool(powered):
            log.warning('⚠️ BT adapter is powered off — powering on')
            bt_power_on()
            bt_discoverable(True)
    except Exception as exc:
        log.warning('⚠️ BlueZ not responding (%s) — resetting adapter', exc)
        threading.Thread(target=_bt_adapter_reset, daemon=True).start()
        return True

    # Idle probe — D-Bus works but signals have dried up (HCI zombie)
    if _last_bt_event > 0:
        idle = time.monotonic() - _last_bt_event
        if idle > WATCHDOG_IDLE_SECS:
            log.warning('⚠️ No BT events for %.0f s — resetting adapter', idle)
            threading.Thread(target=_bt_adapter_reset, daemon=True).start()

    return True   # keep GLib timer repeating


def _nm_purge_bt_profiles(mac: str = None):
    """Delete all NetworkManager Bluetooth connection profiles.

    Called on startup (mac=None) to clear any profiles left from previous runs,
    and on every disconnect (mac=<addr>) to remove profiles for that device.
    This prevents NM from auto-reconnecting via a saved profile.
    """
    try:
        r = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show'],
            capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            parts = line.split(':')
            if len(parts) < 2:
                continue
            name, conn_type = parts[0].strip(), parts[1].strip()
            device = parts[2].strip() if len(parts) > 2 else ''
            if conn_type != 'bluetooth':
                continue
            if mac and device.lower() != mac.lower() and mac.lower() not in name.lower():
                continue
            result = subprocess.run(
                ['nmcli', 'connection', 'delete', name],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                log.info('Deleted NM BT profile: %s', name)
            else:
                log.debug('Could not delete NM profile %s: %s', name,
                          result.stderr.strip().splitlines()[0] if result.stderr else '')
    except Exception as exc:
        log.debug('_nm_purge_bt_profiles error: %s', exc)


def _nm_device_disconnect(mac: str):
    """Tell NetworkManager to disconnect *mac* so it does not auto-reconnect.

    Called whenever BlueZ reports a disconnect.  Without this, NM sees the
    BT device go away and immediately tries to re-establish its saved profile.
    """
    try:
        subprocess.run(
            ['nmcli', 'device', 'disconnect', mac],
            capture_output=True, text=True, timeout=10
        )
        log.debug('NM device %s disconnected', mac)
    except Exception as exc:
        log.debug('nmcli device disconnect %s: %s', mac, exc)


def _reregister_agent():
    """Re-register the pairing agent with bluetoothd.

    Must be called after 'systemctl restart bluetooth' because the new
    bluetoothd process has no knowledge of our AutoPairAgent — every
    pair request would silently receive no response until re-registered.
    """
    if _agent_obj is None:
        return
    try:
        bus = dbus.SystemBus()
        mgr = dbus.Interface(
            bus.get_object(BT_SERVICE, '/org/bluez'),
            'org.bluez.AgentManager1',
        )
        mgr.RegisterAgent(BT_AGENT_PATH, 'NoInputNoOutput')
        mgr.RequestDefaultAgent(BT_AGENT_PATH)
        log.info('Pairing agent re-registered with bluetoothd')
    except Exception as exc:
        log.warning('Could not re-register pairing agent: %s', exc)


def _connect_pan_bttool(mac: str):
    """Connect PAN via bt-network (bluez-tools) subprocess.

    'bt-network -c <MAC> nap' is a battle-tested C implementation that handles
    BNEP at the socket level, avoiding the Python D-Bus fragility of Network1.
    Install with: sudo apt install bluez-tools

    Returns the bnep interface name on success, None if bt-network is not
    installed, or False on connection failure.
    """
    try:
        _cleanup_bnep()
        # bt-network -c is a foreground process that blocks for the lifetime of
        # the connection.  We use Popen and poll for the bnep interface to appear.
        proc = subprocess.Popen(
            ['bt-network', '-c', mac, 'nap'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait up to 10 s for kernel to expose the bnep* interface
        for _ in range(20):
            time.sleep(0.5)
            if proc.poll() is not None:
                log.debug('bt-network exited early (returncode=%d)', proc.returncode)
                return False
            try:
                for entry in os.scandir('/sys/class/net'):
                    if entry.name.startswith('bnep'):
                        log.info('bt-network created interface %s for %s', entry.name, mac)
                        # Store proc so we can terminate it on explicit disconnect
                        _bt_network_procs[mac] = proc
                        return entry.name
            except OSError:
                pass
        # Timeout — interface never appeared
        proc.terminate()
        return False
    except FileNotFoundError:
        return None   # bt-network not installed — fall back to D-Bus
    except Exception as exc:
        log.debug('_connect_pan_bttool error: %s', exc)
        return False


# Running bt-network processes keyed by MAC — terminated on disconnect
_bt_network_procs: dict = {}


def _connect_pan_network1(mac: str):
    """Strategy 0: connect via org.bluez.Network1 D-Bus interface directly.

    Returns:
      True   — connected successfully
      False  — NAP/PANU not available yet (phone not tethering), worth retrying
      None   — device object itself no longer exists, stop retrying
    """
    bus      = dbus.SystemBus()
    dev_path = BT_ADAPTER_PATH + '/dev_' + mac.replace(':', '_')

    # First probe: is the device object still present?
    try:
        dbus.Interface(
            bus.get_object(BT_SERVICE, dev_path),
            'org.freedesktop.DBus.Properties',
        ).Get('org.bluez.Device1', 'Connected')
    except dbus.DBusException as exc:
        err = str(exc)
        if 'UnknownObject' in err or 'DoesNotExist' in err:
            log.info('Network1: device %s no longer exists — aborting', mac)
            return None   # device was removed; stop all retries
        # Other probe error — device still exists, continue

    # Nuke any zombie bnep* interfaces in the kernel before attempting connect.
    # Leftover interfaces from previous rapid cycles are the primary cause of
    # 'Input/output error' on Network1.Connect().
    _cleanup_bnep()

    for profile in ('nap', 'panu'):
        try:
            net   = dbus.Interface(
                bus.get_object(BT_SERVICE, dev_path),
                'org.bluez.Network1',
            )
            # Disconnect any stale Network1 state from a previous bnep0 link.
            # Without this, BlueZ returns "Input/output error" on Connect() when
            # the phone's BT tethering stack cycles disconnect/reconnect rapidly.
            try:
                net.Disconnect()
                time.sleep(0.3)
            except dbus.DBusException:
                pass  # Not connected — that is fine
            iface = net.Connect(profile)
            log.info('✅ PAN connected via bluez Network1/%s (%s) for %s', profile, iface, mac)
            # BlueZ created the interface but did not request a DHCP lease — do it now
            threading.Thread(target=_request_dhcp, args=[str(iface)], daemon=True).start()
            return True
        except dbus.DBusException as exc:
            err = str(exc)
            if 'UnknownObject' in err or 'DoesNotExist' in err:
                # Device was removed between the probe and the Connect call
                log.info('Network1 %s: device gone mid-attempt for %s', profile, mac)
                return None
            # InProgress / Failed / etc. — phone not tethering yet, worth retrying
            log.info('Network1 %s failed for %s: %s', profile, mac, exc)
    return False


def connect_pan(mac: str):
    """Try to establish a Bluetooth PAN (NAP) connection.

    Strategy 0: bt-network (bluez-tools) — C implementation, most robust.
                Install with: sudo apt install bluez-tools
    Strategy 1: org.bluez.Network1 D-Bus — pure Python fallback.

    Returns True on success, False on failure, None if device is gone.
    """
    log.info("PAN: attempting connection to %s ...", mac)

    # ── Strategy 0: bt-network subprocess (bluez-tools) ────────────────────
    result = _connect_pan_bttool(mac)
    if isinstance(result, str):   # returns interface name on success
        threading.Thread(target=_request_dhcp, args=[result], daemon=True).start()
        _announce_internet(mac)
        return True
    if result is False:
        log.debug('bt-network failed for %s — trying D-Bus fallback', mac)
    # result is None — bt-network not installed, proceed to D-Bus

    # ── Strategy 1: native BlueZ Network1 D-Bus ────────────────────────────
    dbus_result = _connect_pan_network1(mac)
    if dbus_result is True:
        _announce_internet(mac)
        return True
    if dbus_result is None:
        return None   # device gone — abort retry loop

    log.warning("\u26a0\ufe0f PAN not ready for %s (phone tethering enabled?)", mac)
    return False


def _announce_internet(mac: str):
    """Log whether internet is actually reachable after PAN connect."""
    # Give the interface a moment to get a DHCP lease
    time.sleep(3)
    _pan_success.add(mac)   # mark as ever-successfully-connected
    # Suppress auto-reconnect attempts for a while
    _pan_cooldown[mac] = time.monotonic() + PAN_COOLDOWN_SECONDS
    # Remove any NM BT profiles so NM can't auto-reconnect on next disconnect
    _nm_purge_bt_profiles(mac)
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
    _handle_connected(mac)


def _handle_connected(mac: str):
    """Attempt PAN connect, deduplicating concurrent calls for the same MAC."""
    with _pan_lock:
        if time.monotonic() < _pan_cooldown.get(mac, 0):
            log.debug('PAN cooldown active for %s — ignoring reconnect signal', mac)
            return
        if mac in _pan_connecting:
            log.debug("PAN connect already in progress for %s — skipping", mac)
            return
        _pan_connecting.add(mac)
    try:
        # Retry up to 3 times with 5 s gap — phone tethering may need a moment
        for attempt in range(1, 4):
            result = connect_pan(mac)
            if result is True:
                return
            if result is None:
                log.info("Device %s no longer present — aborting PAN retries", mac)
                return
            if attempt < 3:
                log.info("PAN attempt %d failed for %s — retrying in 5 s ...", attempt, mac)
                time.sleep(5)
        log.warning("⚠️ PAN connect gave up after 3 attempts for %s", mac)
        log.warning("    → Make sure Bluetooth Tethering is ON in your phone's hotspot settings.")
        # Always remove bonding so the phone can re-pair cleanly.
        # With the NoInputNoOutput agent, re-pairing is automatic — the phone
        # will re-pair and re-establish tethering without user interaction.
        # NOT removing bonding leaves previously-connected devices permanently
        # stuck when the phone's BNEP stack enters an I/O error state.
        bt_remove_device(mac)
    finally:
        with _pan_lock:
            _pan_connecting.discard(mac)


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

    # Any real BT signal proves the adapter is alive — reset the idle watchdog
    if paired or connected is not None:
        global _last_bt_event
        _last_bt_event = time.monotonic()

    if paired:
        log.info("📱 Device paired: %s  %s", mac, name)
        threading.Thread(target=_handle_paired, args=[mac], daemon=True).start()
    elif connected:
        log.info("🔗 Device connected: %s  %s — attempting PAN", mac, name)
        threading.Thread(target=_handle_connected, args=[mac], daemon=True).start()
    elif connected == dbus.Boolean(False):
        log.info("🔌 Device disconnected: %s  %s", mac, name)
        with _pan_lock:
            still_connecting = mac in _pan_connecting
        # Clear PAN cooldown so the next Connected signal immediately re-establishes PAN.
        # The phone's BT tethering stack cycles disconnect/reconnect every ~50–60 s,
        # which is shorter than the 90 s cooldown window — without this the Pi would
        # sit with no internet until the cooldown expires.
        _pan_cooldown.pop(mac, None)
        # Purge NM BT profiles for this MAC so NM can't auto-reconnect
        threading.Thread(target=_nm_purge_bt_profiles, args=[mac], daemon=True).start()
        # If no PAN attempt is in flight and device never connected successfully,
        # the bonding key may be stale — remove it so the phone can re-pair cleanly.
        if mac not in _pan_success and not still_connecting:
            log.info("Device %s disconnected without successful PAN — removing stale bonding", mac)
            threading.Thread(target=bt_remove_device, args=[mac], daemon=True).start()


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    # Clean up any stale kernel state left by the previous run before touching
    # the adapter.  Zombie bnep interfaces and stuck HCI state cause every
    # subsequent Network1.Connect() to fail with Input/output error.
    _cleanup_bnep()
    log.info("Cleaned up stale bnep interfaces")

    # Hard-reset the adapter so the HCI layer starts clean regardless of what
    # the previous service run left behind. Power-off flushes internal BNEP/ACL
    # state that a Python process restart alone cannot clear.
    # BlueZ briefly rejects Set('Powered', False) with AuthenticationFailed while
    # still cleaning up the previous session — retry until it sticks.
    log.info("Power-cycling BT adapter for clean startup ...")
    for _attempt in range(6):
        try:
            _adapter_props().Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(False))
            log.debug('Adapter powered off (attempt %d)', _attempt + 1)
            break
        except dbus.DBusException as _exc:
            log.debug('Power-off attempt %d: %s — retrying in 1 s', _attempt + 1, _exc)
            time.sleep(1)
    time.sleep(1)  # let the HCI layer settle after power-off

    # Power on + make discoverable
    bt_power_on()
    bt_discoverable(True)

    # Register the auto-accept pairing agent
    global _agent_obj, _agent_mgr_obj
    _agent_obj = AutoPairAgent(bus, BT_AGENT_PATH)
    _agent_mgr_obj = dbus.Interface(
        bus.get_object(BT_SERVICE, '/org/bluez'),
        'org.bluez.AgentManager1',
    )
    _agent_mgr_obj.RegisterAgent(BT_AGENT_PATH, 'NoInputNoOutput')
    _agent_mgr_obj.RequestDefaultAgent(BT_AGENT_PATH)
    log.info("Auto-pair agent registered (NoInputNoOutput)")

    # Purge any BT profiles left from previous runs — prevents NM auto-reconnect
    _nm_purge_bt_profiles()
    log.info("Cleared stale NM Bluetooth profiles")

    # Subscribe to device property changes (Paired / Connected).
    # Do NOT filter by bus_name=BT_SERVICE: if bluetoothd restarts (watchdog
    # fallback), it acquires a new unique D-Bus name and the old bus_name filter
    # would silently drop all Paired/Connected signals forever.
    # The handler already ignores anything that isn't org.bluez.Device1.
    bus.add_signal_receiver(
        _on_properties_changed,
        dbus_interface='org.freedesktop.DBus.Properties',
        signal_name='PropertiesChanged',
        path_keyword='path',
    )

    log.info("📲  Bluetooth ready — scan for this device from your phone and pair.")
    log.info("    On Android: enable Bluetooth Tethering in hotspot settings.")

    # Seed idle watchdog with current time so it fires even if no device
    # has ever paired in this session (catches stuck adapter from startup)
    global _last_bt_event
    _last_bt_event = time.monotonic()

    loop = GLib.MainLoop()

    # Periodically re-assert discoverability (BlueZ may reset it)
    def _keep_discoverable():
        bt_discoverable(True)
        return True   # returning True keeps the GLib timer repeating

    GLib.timeout_add_seconds(REDISCOVER_INTERVAL, _keep_discoverable)
    GLib.timeout_add_seconds(WATCHDOG_INTERVAL,   _bt_watchdog)

    def _shutdown(signum, frame):
        log.info("Signal %s received — shutting down", signum)
        bt_discoverable(False)
        try:
            _agent_mgr_obj.UnregisterAgent(BT_AGENT_PATH)
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
