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
    _, data = _api("POST", "/api/control", {"did": did, "action": action}, timeout=15)
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
        auto_refresh_switch = ui.switch('Auto-refresh', value=True).props('dense color=teal-8').classes('q-ml-auto')
        auto_refresh_timer_holder = [None]

        def _on_auto_refresh_change(e):
            if e.value:
                auto_refresh_timer_holder[0] = ui.timer(10.0, _halite_refresh)
            else:
                if auto_refresh_timer_holder[0] is not None:
                    auto_refresh_timer_holder[0].deactivate()
                    auto_refresh_timer_holder[0] = None

        auto_refresh_switch.on('change', _on_auto_refresh_change)

        # ── Server offline banner ─────────────────────────────────────────────
        offline_banner = ui.card().classes('w-full q-pa-md q-mb-sm bg-red-1')
        offline_banner.set_visibility(False)
        with offline_banner:
            ui.label('⚠️ ha-lite server is not reachable').classes('text-subtitle2 text-red-9')
            ui.label(f'Expected at {HALITE_BASE} — start it: sudo systemctl start halite').classes('text-caption text-grey-7')

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
                    try:
                        result = _halite_qr_start()
                    except Exception as e:
                        qr_msg.set_text(f'Cannot reach ha-lite server at {HALITE_BASE}: {e}')
                        qr_status_badge.set_text('Server offline')
                        qr_status_badge.props('color=red icon=error')
                        ui.notify(f'ha-lite server unreachable: {e}', type='negative')
                        return

                    if result.get("status") == "waiting":
                        qr_status_badge.set_text('Waiting for scan…')
                        qr_status_badge.props('color=blue icon=qr_code')

                        # Build HTML with embedded base64 QR image + direct link.
                        data_uri = result.get("qr_image_b64_data_uri", "") or result.get("qr_image_b64", "")
                        if data_uri and not data_uri.startswith("data:"):
                            data_uri = "data:image/png;base64," + data_uri
                        direct_url = result.get("login_url", "")
                        img_html = ""
                        if data_uri:
                            img_html = (
                                f'<img src="{data_uri}" '
                                f'style="max-width:280px;border-radius:12px;display:block;margin:12px auto;" '
                                f'alt="Xiaomi QR Code">'
                            )
                        else:
                            # Fallback: use image URL endpoint with ?format=raw for PNG.
                            img_url = result.get("qr_image_url", "") or f"{HALITE_BASE}/api/login/qr/image"
                            if img_url and "?" not in img_url:
                                img_url += "?format=raw"
                            if img_url:
                                img_html = (
                                    f'<img src="{img_url}" '
                                    f'style="max-width:280px;border-radius:12px;display:block;margin:12px auto;" '
                                    f'alt="Xiaomi QR Code">'
                                )

                        link_html = ""
                        if direct_url:
                            link_html = (
                                f'<p style="color:#999;font-size:0.7rem;word-break:break-all;margin-top:8px;">'
                                f'Or: <a href="{direct_url}" target="_blank" style="font-size:0.65rem;color:#1976d2;">'
                                f'open direct login link</a></p>'
                            )

                        qr_iframe.set_content(
                            f'<div style="text-align:center;margin-top:12px;">'
                            f'{img_html}'
                            f'<p style="color:#666;font-size:0.8rem;">📱 Open Mi Home app → Profile → top-right → Scan</p>'
                            f'<p style="color:#999;font-size:0.75rem;">After scan, click Refresh Devices</p>'
                            f'{link_html}'
                            f'</div>'
                        )
                        qr_msg.set_text('QR code ready. Scan with Mi Home app on your phone.')
                        _start_polling_qr()
                    else:
                        err_msg = result.get("message", "") or result.get("error", "") or str(result)
                        qr_msg.set_text(f'QR start failed: {err_msg}')
                        qr_iframe.set_content(
                            f'<div style="padding:12px;color:#c00;font-size:0.8rem;">'
                            f'QR login could not start.<br>'
                            f'Error: {err_msg}<br>'
                            f'Check ha-lite logs: <code>journalctl -u halite -f</code>'
                            f'</div>'
                        )
                        ui.notify(f'QR login failed: {err_msg}', type='negative')

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
                        qr_msg.set_text('Login successful! Syncing devices from cloud...')
                        # Call collect to sync devices, then refresh UI.
                        _api("POST", "/api/login/qr/collect")
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
            offline_banner.set_visibility(False)

            if health.get("error") or health.get("status") != "ok":
                offline_banner.set_visibility(True)
                health_label.set_text('🔴 Server: unreachable')
                device_count_label.set_text('')
                cloud_label.set_text('')
                qr_card.set_visibility(False)
                device_container.clear()
                return

            is_authed = health.get("cloud_authed", False)
            dev_count = health.get("device_count", 0)
            health_label.set_text(f'🟢 Server: {health.get("version", "?")}')
            device_count_label.set_text(f'Devices: {dev_count}')
            cloud_label.set_text(f'Cloud: {"✅" if is_authed else "❌"}')

            # Hide QR card after successful login.
            if is_authed:
                qr_card.set_visibility(False)
            else:
                qr_card.set_visibility(True)

            devices = _halite_devices()
            device_container.clear()

            if not devices:
                with device_container:
                    ui.card().classes('w-full q-pa-md q-mb-sm bg-grey-2')
                    with ui.row().classes('w-full items-center'):
                        if is_authed:
                            ui.icon('cloud_done', color='green').classes('q-mr-sm')
                            ui.label('Cloud connected but no devices found. Add devices in Mi Home app, then re-login.').classes('text-grey-7')
                        else:
                            ui.icon('info', color='grey').classes('q-mr-sm')
                            ui.label('No devices yet. Click "Start QR Login" to sync devices from Xiaomi Cloud.').classes('text-grey-7')
                return

            online_devices = [d for d in devices if d.get("online", False)]
            offline_devices = [d for d in devices if not d.get("online", False)]

            def _render_device(d):
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

                        # ── Brightness & color temperature sliders for lights ──
                        if online and _device_is_light(model):
                            with ui.row().classes('w-full items-center q-mt-xs'):
                                # Brightness slider with debounce (600ms)
                                ui.icon('light_mode', size='xs').classes('text-amber-5')
                                bright_lbl = ui.label('50%').classes('text-caption')
                                bright_lbl.props('style="min-width:45px;text-align:right;"')
                                bright_slider = ui.slider(min=1, max=100, value=50, step=1).classes('q-mx-sm')
                                bright_timer_holder = [None]  # mutable container to avoid nonlocal issues

                                def _mk_bright_handler(d, n, lbl, slider, holder):
                                    def _handler():
                                        val = int(slider.value)
                                        lbl.set_text(f'{val}%')
                                        return _send_brightness(d, n, val, lbl)
                                    def _on_slide():
                                        lbl.set_text(f'{int(slider.value)}%')
                                        if holder[0] is not None:
                                            holder[0].deactivate()
                                        holder[0] = ui.timer(0.6, _handler, once=True)
                                    return _on_slide

                                def _send_brightness(did, name, val, lbl):
                                    lbl.set_text(f'{val}% ✅')
                                    r = _halite_control(did, f"brightness:{val}")
                                    if r.get("status") != "success":
                                        ui.notify(f'{name}: {r.get("error", "?")}', type='warning')

                                bright_slider.on('change', _mk_bright_handler(did, name, bright_lbl, bright_slider, bright_timer_holder))

                            with ui.row().classes('w-full items-center q-mt-xs'):
                                # Color temperature slider with debounce (600ms)
                                ui.icon('thermostat', size='xs').classes('text-blue-5')
                                cct_lbl = ui.label('4000K').classes('text-caption')
                                cct_lbl.props('style="min-width:45px;text-align:right;"')
                                cct_slider = ui.slider(min=2700, max=6500, value=4000, step=100).classes('q-mx-sm')
                                cct_timer_holder = [None]

                                def _mk_cct_handler(d, n, lbl, slider, holder):
                                    def _handler():
                                        val = int(slider.value)
                                        lbl.set_text(f'{val}K')
                                        return _send_cct(d, n, val, lbl)
                                    def _on_slide():
                                        lbl.set_text(f'{int(slider.value)}K')
                                        if holder[0] is not None:
                                            holder[0].deactivate()
                                        holder[0] = ui.timer(0.6, _handler, once=True)
                                    return _on_slide

                                def _send_cct(did, name, val, lbl):
                                    lbl.set_text(f'{val}K ✅')
                                    r = _halite_control(did, f"color_temp:{val}")
                                    if r.get("status") != "success":
                                        ui.notify(f'{name}: {r.get("error", "?")}', type='warning')

                                cct_slider.on('change', _mk_cct_handler(did, name, cct_lbl, cct_slider, cct_timer_holder))

            with device_container:
                # ── Online devices section (collapsible) ──
                if online_devices:
                    with ui.expansion(
                        f'Online ({len(online_devices)})',
                        icon='wifi', value=True
                    ).classes('w-full q-pa-xs q-mb-xs bg-green-1').props('header-class=text-green-9 text-subtitle2'):
                        online_by_cat = _group_by_category(online_devices)
                        for cat_key in _CATEGORY_ORDER:
                            if cat_key not in online_by_cat:
                                continue
                            cat_devices = online_by_cat[cat_key]
                            meta = _CATEGORY_META.get(cat_key, _CATEGORY_META['other'])
                            if len(online_by_cat) > 1:
                                with ui.row().classes('w-full items-center q-mt-sm q-mb-xs'):
                                    ui.label(f'{meta["icon"]} {meta["label"]} ({len(cat_devices)})').classes('text-caption text-grey-6')
                            for d in cat_devices:
                                _render_device(d)

                # ── Offline devices section (collapsible) ──
                if offline_devices:
                    with ui.expansion(
                        f'Offline ({len(offline_devices)})',
                        icon='wifi_off', value=True
                    ).classes('w-full q-pa-xs q-mb-xs bg-grey-3').props('header-class=text-grey-7 text-subtitle2'):
                        offline_by_cat = _group_by_category(offline_devices)
                        for cat_key in _CATEGORY_ORDER:
                            if cat_key not in offline_by_cat:
                                continue
                            cat_devices = offline_by_cat[cat_key]
                            meta = _CATEGORY_META.get(cat_key, _CATEGORY_META['other'])
                            if len(offline_by_cat) > 1:
                                with ui.row().classes('w-full items-center q-mt-sm q-mb-xs'):
                                    ui.label(f'{meta["icon"]} {meta["label"]} ({len(cat_devices)})').classes('text-caption text-grey-6')
                            for d in cat_devices:
                                _render_device(d)

        # Initial load.
        _halite_refresh()
        auto_refresh_timer_holder[0] = ui.timer(10.0, _halite_refresh)

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


def _device_is_light(model: str) -> bool:
    """Check if a device supports brightness / color temperature control."""
    model_lower = model.lower()
    light_keywords = ("light", "lamp", "bulb", "candle", "downlight", "ceiling")
    return any(kw in model_lower for kw in light_keywords)


# ── Device auto-categorization ────────────────────────────────────────────────

_CATEGORY_META = {
    'lights':    {'icon': '💡', 'label': 'Lights'},
    'vacuum':    {'icon': '🧹', 'label': 'Vacuums'},
    'fan':       {'icon': '🌀', 'label': 'Fans'},
    'sensor':    {'icon': '📡', 'label': 'Sensors'},
    'air':       {'icon': '🌬️', 'label': 'Air Purifiers'},
    'switch':    {'icon': '🔌', 'label': 'Switches & Plugs'},
    'camera':    {'icon': '📷', 'label': 'Cameras'},
    'curtain':   {'icon': '🪟', 'label': 'Curtains'},
    'lock':      {'icon': '🔐', 'label': 'Locks'},
    'gateway':   {'icon': '🌐', 'label': 'Gateways'},
    'speaker':   {'icon': '🔊', 'label': 'Speakers'},
    'appliance': {'icon': '🏠', 'label': 'Appliances'},
    'other':     {'icon': '📦', 'label': 'Other'},
}

_CATEGORY_ORDER = ['lights', 'switch', 'fan', 'air', 'vacuum', 'curtain',
                   'camera', 'sensor', 'lock', 'gateway', 'speaker',
                   'appliance', 'other']


def _device_category(model: str) -> str:
    """Auto-categorize a Xiaomi device by model name."""
    m = model.lower()
    if any(kw in m for kw in ('light', 'lamp', 'bulb', 'candle', 'downlight', 'ceiling', 'led')):
        return 'lights'
    if any(kw in m for kw in ('vacuum', 'clean', 'sweep', 'dust')):
        return 'vacuum'
    if any(kw in m for kw in ('fan', 'airer', 'dryer')):
        return 'fan'
    if any(kw in m for kw in ('sensor', 'motion', 'contact', 'flood', 'temp', 'humid', 'weather', 'smoke', 'gas', 'magnet')):
        return 'sensor'
    if any(kw in m for kw in ('purifier', 'filter')):
        return 'air'
    if any(kw in m for kw in ('plug', 'outlet', 'switch', 'relay', 'strip', 'power', 'socket')):
        return 'switch'
    if any(kw in m for kw in ('camera', 'doorbell', 'monitor', 'cam', 'isp')):
        return 'camera'
    if any(kw in m for kw in ('curtain', 'blind', 'window', 'shade', 'roller')):
        return 'curtain'
    if any(kw in m for kw in ('lock', 'door', 'deadbolt')):
        return 'lock'
    if any(kw in m for kw in ('gateway', 'hub', 'bridge', 'repeater')):
        return 'gateway'
    if any(kw in m for kw in ('speaker', 'box', 'audio', 'sound', 'alarm', 'story')):
        return 'speaker'
    if any(kw in m for kw in ('kettle', 'cooker', 'rice', 'oven', 'microwave', 'fridge', 'washer', 'heater', 'water', 'toothbrush', 'scale', 'watch', 'band')):
        return 'appliance'
    return 'other'


def _group_by_category(devices):
    """Group devices by category, ordered by _CATEGORY_ORDER."""
    groups = {}
    for d in devices:
        cat = _device_category(d.get("model", ""))
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(d)
    return groups