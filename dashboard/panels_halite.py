"""ClawBoard dashboard — ha-lite panel (Xiaomi smart home device control)."""
import json
import urllib.request
import urllib.error
from nicegui import ui

HALITE_BASE = "http://127.0.0.1:8090"

def _api(method: str, path: str, body: dict | None = None, timeout: int = 5) -> tuple[int, dict]:
    """Call ha-lite REST API."""
    url = f"{HALITE_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
    except Exception as e:
        return 0, {"error": str(e)}


def _halite_health() -> dict:
    _, data = _api("GET", "/api/health")
    return data


def _halite_devices() -> list[dict]:
    _, data = _api("GET", "/api/devices")
    return data.get("devices", [])


def _halite_control(did: str, action: str) -> dict:
    _, data = _api("POST", "/api/control", {"did": did, "action": action})
    return data


def _halite_schema() -> dict:
    _, data = _api("GET", "/openclaw/schema")
    return data


def _halite_qr_status() -> dict:
    _, data = _api("GET", "/api/login/qr/status")
    return data


def _halite_qr_start() -> dict:
    _, data = _api("POST", "/api/login/qr/start")
    return data


def _halite_sync() -> dict:
    _, data = _api("POST", "/api/sync")
    return data


def build_halite_panel(T: dict, conf: dict, lang: str):
    """Build the ha-lite device control panel."""

    halite_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    halite_content.set_visibility(False)

    with halite_content:
        # ── Header ────────────────────────────────────────────────────────────
        with ui.row().classes('w-full items-center q-mb-xs'):
            ui.label('🏠 HA Lite — Smart Home').classes('text-h6 text-teal-8')
            ui.button(icon='refresh', on_click=lambda: _halite_refresh()).props('flat round dense color=teal-8').tooltip('Refresh devices')

        # ── Status bar ────────────────────────────────────────────────────────
        status_bar = ui.row().classes('w-full items-center q-mb-sm gap-x-4')
        health_label = ui.label('').classes('text-caption')
        device_count_label = ui.label('').classes('text-caption')
        cloud_label = ui.label('').classes('text-caption')

        # ── QR Login section ──────────────────────────────────────────────────
        qr_card = ui.card().classes('w-full q-pa-md q-mb-sm bg-blue-1')
        with qr_card:
            with ui.row().classes('w-full items-center'):
                ui.label('🔑 Login to Xiaomi Cloud').classes('text-subtitle2 text-blue-9')
                qr_status_badge = ui.chip('Not logged in', color='orange', icon='lock')
            qr_msg = ui.label('Scan QR code with Mi Home app to sync devices.').classes('text-caption text-grey-7 q-mt-xs')
            qr_iframe = ui.html('').classes('w-full')
            with ui.row().classes('q-mt-sm gap-x-2'):
                def _start_qr():
                    result = _halite_qr_start()
                    if result.get("status") == "waiting":
                        qr_msg.set_text('✅ QR code ready. Open the link below or scan the QR in server terminal.')
                        qr_status_badge.set_text('Waiting for scan…')
                        qr_status_badge.props('color=blue icon=qr_code')
                        img_url = result.get("qr_image_url", "")
                        if img_url:
                            qr_iframe.set_content(
                                f'<div style="text-align:center;margin-top:12px;">'
                                f'<img src="{img_url}" style="max-width:280px;border-radius:12px;" '
                                f'onerror="this.parentElement.innerHTML=\'<p style=color:red>QR image not loaded. '
                                f'Check ha-lite server logs.</p>\'">'
                                f'<p style="color:#666;font-size:0.8rem;">Scan with Mi Home app → Profile → top-right → Scan</p>'
                                f'<p style="color:#999;font-size:0.75rem;">After scan, click Refresh Devices</p>'
                                f'</div>'
                            )
                        _start_polling_qr()
                    else:
                        qr_msg.set_text(f'❌ QR start failed: {result.get("message", "unknown error")}')
                        ui.notify(f'QR login failed: {result.get("message", "")}', type='negative')

                ui.button('📱 Start QR Login', on_click=_start_qr).props('color=blue-8')
                ui.button('🔄 Refresh Devices', on_click=lambda: _halite_refresh()).props('color=teal-8')

                def _start_polling_qr():
                    timer = ui.timer(3.0, lambda: _check_qr(timer), active=True)

                def _check_qr(timer):
                    status = _halite_qr_status()
                    if status.get("has_service_token"):
                        timer.deactivate()
                        qr_status_badge.set_text('Logged in ✅')
                        qr_status_badge.props('color=green icon=check_circle')
                        qr_msg.set_text('Login successful! Syncing devices...')
                        # Auto-collect and refresh.
                        _halite_sync()
                        _halite_refresh()
                        ui.notify('✅ Xiaomi login complete', type='positive')
                    elif status.get("status") == "scanned":
                        qr_status_badge.set_text('Scanned — finishing…')
                        qr_status_badge.props('color=orange icon=photo_camera')
                    elif status.get("status") == "timeout":
                        timer.deactivate()
                        qr_status_badge.set_text('Expired')
                        qr_status_badge.props('color=red icon=timer_off')
                        qr_msg.set_text('QR code expired. Click "Start QR Login" to get a new one.')

        # ── Device list ───────────────────────────────────────────────────────
        device_container = ui.column().classes('w-full')

        def _halite_refresh():
            """Refresh device list and status."""
            health = _halite_health()
            health_label.set_text(f'🟢 Server: {health.get("version", "?")}')
            device_count_label.set_text(f'Devices: {health.get("device_count", 0)}')
            cloud_label.set_text(f'Cloud: {"✅" if health.get("cloud_authed") else "❌"}')

            devices = _halite_devices()
            device_container.clear()

            if not devices:
                with device_container:
                    ui.card().classes('w-full q-pa-md q-mb-sm bg-grey-2')
                    with ui.row().classes('w-full items-center'):
                        ui.icon('info', color='grey').classes('q-mr-sm')
                        ui.label('No devices found. Login to Xiaomi Cloud first or add devices via Mi Home app.').classes('text-grey-7')
                return

            # Hide QR card if already authenticated.
            if health.get("cloud_authed"):
                qr_card.set_visibility(False)

            with device_container:
                for d in devices:
                    did = d.get("did", "")
                    name = d.get("name", "?")
                    model = d.get("model", "?")
                    ip = d.get("ip", "?")
                    online = d.get("online", False)

                    with ui.card().classes('w-full q-pa-sm q-mb-xs'):
                        with ui.row().classes('w-full items-center'):
                            status_icon = '🟢' if online else '🔴'
                            ui.label(f'{status_icon} {name}').classes('text-subtitle2')
                            ui.space()
                            ui.label(model).classes('text-caption text-grey-6')
                            ui.label(f'IP: {ip}').classes('text-caption text-grey-6 q-ml-sm')

                            # On/Off buttons for controllable devices.
                            if online and _device_supports_control(model):
                                dev_state = ui.label('').classes('text-caption q-mx-sm')

                                def _on(did=did, name=name, lbl=dev_state):
                                    lbl.set_text('⏳')
                                    r = _halite_control(did, "on")
                                    if r.get("status") == "success":
                                        lbl.set_text('✅ ON')
                                        ui.notify(f'{name}: turned ON', type='positive')
                                    else:
                                        lbl.set_text('❌')
                                        ui.notify(f'{name}: failed — {r.get("error", "?")}', type='negative')

                                def _off(did=did, name=name, lbl=dev_state):
                                    lbl.set_text('⏳')
                                    r = _halite_control(did, "off")
                                    if r.get("status") == "success":
                                        lbl.set_text('✅ OFF')
                                        ui.notify(f'{name}: turned OFF', type='positive')
                                    else:
                                        lbl.set_text('❌')
                                        ui.notify(f'{name}: failed — {r.get("error", "?")}', type='negative')

                                ui.button('ON', on_click=_on).props('size=sm color=green-7 dense')
                                ui.button('OFF', on_click=_off).props('size=sm color=red-7 dense')

        # Initial load.
        _halite_refresh()

    return halite_content


def _device_supports_control(model: str) -> bool:
    """Check if a device model supports on/off control."""
    model_lower = model.lower()
    # Sensors and cameras are read-only.
    read_only = ("sensor", "camera", "motion", "contact", "flood", "temperature",
                 "humidity", "gateway", "watch", "speaker", "toothbrush",
                 "doorbell", "lock", "monitor", "storyteller", "repeater",
                 "box", "purifier")  # purifier supports control but has different siid
    for kw in read_only:
        if kw in model_lower:
            # But purifiers and some others DO support basic on/off.
            if kw == "purifier" and "air" not in model_lower:
                continue
            if kw in ("speaker", "box", "purifier"):
                continue
            return False
    return True