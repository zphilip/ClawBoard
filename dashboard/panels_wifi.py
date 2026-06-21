"""ClawBoard dashboard — wifi panel."""
import subprocess
from nicegui import ui


def build_wifi_panel(T):
    """Build panel UI."""
    # ══ WiFi Setup ════════════════════════════════════════════════════════════
    wifi_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    wifi_content.set_visibility(False)
    with wifi_content:
        ui.label(T['wifi_title']).classes('text-h6 text-teal-8 q-mb-xs')

        # ── Current WiFi status card ─────────────────────────────────────────
        with ui.card().classes('w-full q-pa-md q-mb-sm'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label(T['wifi_cur_status']).classes('text-subtitle1 text-bold')
                def _refresh_wifi_status():
                    import subprocess as _sp
                    lines = []

                    # ── Primary: nmcli (works with USB WiFi adapters) ────────
                    ssid = iface = ip4 = gw4 = signal = ''
                    try:
                        # nmcli -t -f DEVICE,TYPE,STATE,CONNECTION,SIGNAL dev
                        nm = _sp.run(
                            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION,SIGNAL', 'dev'],
                            capture_output=True, text=True
                        )
                        for row in nm.stdout.splitlines():
                            parts = row.split(':')
                            if len(parts) >= 3 and parts[1] == 'wifi' and parts[2] == 'connected':
                                iface  = parts[0]
                                ssid   = parts[3] if len(parts) > 3 else ''
                                signal = parts[4].strip() + ' %' if len(parts) > 4 and parts[4].strip() else ''
                                break
                    except Exception:
                        pass

                    # ── Fallback: iwgetid (classic wireless-tools) ───────────
                    if not ssid:
                        try:
                            iface_r = _sp.run(['iwgetid', '-r'], capture_output=True, text=True)
                            ssid    = iface_r.stdout.strip()
                            iface_n = _sp.run(['iwgetid'],       capture_output=True, text=True)
                            iface   = iface_n.stdout.split()[0] if iface_n.stdout.strip() else iface or 'wlan0'
                        except Exception:
                            pass

                    # ── Detect any active wifi interface if still unknown ─────
                    if not iface:
                        try:
                            ip_lnk = _sp.run(['ip', '-o', 'link', 'show'],
                                             capture_output=True, text=True)
                            for ln in ip_lnk.stdout.splitlines():
                                if 'wlan' in ln or 'wlp' in ln or 'wlx' in ln:
                                    iface = ln.split(':')[1].strip().split('@')[0]
                                    break
                        except Exception:
                            iface = 'wlan0'

                    # ── Connected / disconnected banner ──────────────────────
                    if ssid:
                        lines.append(f'🟢 Connected  |  SSID: {ssid}  |  Interface: {iface}')
                    else:
                        # Double-check: is there an IP on the wifi iface anyway?
                        try:
                            chk = _sp.run(['ip', '-4', 'addr', 'show', iface or 'wlan0'],
                                          capture_output=True, text=True)
                            has_ip = any(l.strip().startswith('inet ') for l in chk.stdout.splitlines())
                        except Exception:
                            has_ip = False
                        if has_ip:
                            lines.append(f'🟡 Connected (SSID unknown)  |  Interface: {iface or "wlan0"}')
                        else:
                            lines.append('🔴 Not connected to any WiFi network')

                    # ── IP address ───────────────────────────────────────────
                    try:
                        ip_r = _sp.run(['ip', '-4', 'addr', 'show', iface or 'wlan0'],
                                       capture_output=True, text=True)
                        for ln in ip_r.stdout.splitlines():
                            ln = ln.strip()
                            if ln.startswith('inet '):
                                ip4 = ln.split()[1]
                                lines.append(f'   IP Address : {ip4}')
                                break
                        else:
                            lines.append('   IP Address : (none)')
                    except Exception as exc:
                        lines.append(f'   IP Address : error ({exc})')

                    # ── Default gateway ──────────────────────────────────────
                    try:
                        gw_r = _sp.run(['ip', 'route', 'show', 'default'],
                                       capture_output=True, text=True)
                        for ln in gw_r.stdout.strip().splitlines():
                            parts = ln.split()
                            if len(parts) > 2:
                                lines.append(f'   Gateway    : {parts[2]}')
                                break
                    except Exception:
                        pass

                    # ── Signal (from nmcli or iwconfig fallback) ─────────────
                    if signal:
                        lines.append(f'   Signal     : {signal}')
                    else:
                        try:
                            iwc = _sp.run(['iwconfig', iface or 'wlan0'],
                                          capture_output=True, text=True)
                            for ln in iwc.stdout.splitlines():
                                if 'Signal level' in ln:
                                    sig = ln.strip().split('Signal level=')[-1].split()[0]
                                    lines.append(f'   Signal     : {sig}')
                                    break
                        except Exception:
                            pass

                    wifi_cur_lbl.set_text('\n'.join(lines))

                ui.button(icon='refresh', on_click=_refresh_wifi_status).props('flat round dense color=teal-8').tooltip(T['wifi_refresh_tip'])
            wifi_cur_lbl = ui.label('…').classes('text-caption text-mono q-mt-xs').style('white-space: pre')
            # Populate immediately when the card is built
            _refresh_wifi_status()

        # ── Captive portal card ───────────────────────────────────────────────
        with ui.card().classes('w-full q-pa-md'):
            ui.label(T['wifi_config_title']).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T['wifi_config_hint']).classes('text-caption text-grey-6 q-mb-md')

            wifi_status_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')
            wifi_log_area   = ui.textarea().classes('w-full').props('outlined rows=8 readonly label="Output"')
            wifi_log_area.set_visibility(False)

            _wifi_proc = {'proc': None}

            def _start_wifi_setup():
                wifi_log_area.set_visibility(True)
                wifi_log_area.set_value('')
                wifi_status_lbl.set_text('⏳ Starting wifi-connect…')
                btn_wifi_start.props('disabled loading')
                btn_wifi_stop.props(remove='disabled')
                import threading, subprocess as _sp

                def _run():
                    # Stop dnsmasq if it's running so wifi-connect can bind port 53
                    _dnsmasq_was_active = _sp.run(
                        ['systemctl', 'is-active', '--quiet', 'dnsmasq'],
                        capture_output=True
                    ).returncode == 0
                    if _dnsmasq_was_active:
                        wifi_log_area.set_value(wifi_log_area.value + '[setup] Stopping dnsmasq…\n')
                        _sp.run(['sudo', '/usr/bin/systemctl', 'stop', 'dnsmasq'],
                                capture_output=True)
                    try:
                        proc = _sp.Popen(
                            ['sudo', '/opt/wifi-connect/wifi-connect',
                             '-u', '/opt/wifi-connect/web',
                             '-s', 'ClawBerry WiFi Setup'],
                            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True
                        )
                        _wifi_proc['proc'] = proc
                        wifi_status_lbl.set_text('🟢 Running — connect to "ClawBerry WiFi Setup" AP')
                        for line in proc.stdout:
                            wifi_log_area.set_value(wifi_log_area.value + line)
                        proc.wait()
                        rc = proc.returncode
                        wifi_status_lbl.set_text(
                            f'✅ Finished (exit {rc})' if rc == 0 else f'⚠️ Exited with code {rc}'
                        )
                        _refresh_wifi_status()  # update current-status card
                    except Exception as exc:
                        wifi_status_lbl.set_text(f'❌ Error: {exc}')
                    finally:
                        _wifi_proc['proc'] = None
                        btn_wifi_start.props(remove='disabled loading')
                        btn_wifi_stop.props('disabled')
                        # Restore dnsmasq if it was running before
                        if _dnsmasq_was_active:
                            wifi_log_area.set_value(wifi_log_area.value + '[setup] Restoring dnsmasq…\n')
                            _sp.run(['sudo', '/usr/bin/systemctl', 'start', 'dnsmasq'],
                                    capture_output=True)

                threading.Thread(target=_run, daemon=True).start()

            def _stop_wifi_setup():
                proc = _wifi_proc.get('proc')
                if proc:
                    try:
                        proc.terminate()
                        wifi_status_lbl.set_text('🛑 Stopped')
                    except Exception as exc:
                        wifi_status_lbl.set_text(f'Stop failed: {exc}')
                btn_wifi_stop.props('disabled')

            with ui.row().classes('gap-2 q-mt-sm'):
                btn_wifi_start = ui.button(
                    T['wifi_btn_start'], on_click=_start_wifi_setup
                ).props('elevated color=teal-8')
                btn_wifi_stop = ui.button(
                    T['wifi_btn_stop'], on_click=_stop_wifi_setup
                ).props('outline color=negative disabled')
    return wifi_content
