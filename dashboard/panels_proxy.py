"""ClawBoard dashboard — proxy panel."""
import json
import subprocess
import tomlkit
import urllib.request
from nicegui import ui


from dashboard.config_io import deploy_clawproxy_config, load_clawproxy_config, save_clawproxy_config
def build_proxy_panel(T):
    """Build panel UI."""
    # ══ Upgrade ═══════════════════════════════════════════════════════════════
    # ══ ClawBerry Proxy Panel ════════════════════════════════════════════════
    proxy_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    proxy_content.set_visibility(False)
    with proxy_content:
        ui.label(T['proxy_title']).classes('text-h6 text-indigo-9 q-mb-xs')
        with ui.card().classes('w-full q-pa-md'):
            ui.label(T['proxy_card_title']).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T['proxy_hint']).classes('text-caption text-grey-6 q-mb-sm')

            proxy_status_lbl  = ui.label('').classes('text-body2 q-mb-sm')
            proxy_enabled_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')

            def _proxy_get_status():
                active  = subprocess.run(['systemctl', 'is-active',  'clawberry-proxy.service'],
                                         capture_output=True, text=True).stdout.strip()
                enabled = subprocess.run(['systemctl', 'is-enabled', 'clawberry-proxy.service'],
                                         capture_output=True, text=True).stdout.strip()
                return active, enabled

            def _proxy_refresh():
                active, enabled = _proxy_get_status()
                status_txt  = T['proxy_active']   if active  == 'active'  else T['proxy_inactive']
                enabled_txt = T['proxy_enabled']  if enabled == 'enabled' else T['proxy_disabled']
                proxy_status_lbl.set_text(f"{T['proxy_status_lbl']}: {status_txt}  |  {enabled_txt}")

            def _proxy_enable():
                subprocess.run(['sudo', '/usr/bin/systemctl', 'enable', '--now', 'clawberry-proxy.service'],
                               capture_output=True)
                _proxy_refresh()
                ui.notify('clawberry-proxy enabled & started', color='positive')

            def _proxy_disable():
                subprocess.run(['sudo', '/usr/bin/systemctl', 'disable', '--now', 'clawberry-proxy.service'],
                               capture_output=True)
                _proxy_refresh()
                ui.notify('clawberry-proxy disabled & stopped', color='warning')

            def _proxy_restart():
                subprocess.run(['sudo', '/usr/bin/systemctl', 'restart', 'clawberry-proxy.service'],
                               capture_output=True)
                _proxy_refresh()
                ui.notify('clawberry-proxy restarted', color='positive')

            _proxy_refresh()

            with ui.row().classes('gap-2 q-mt-sm'):
                ui.button(T['proxy_btn_enable'],  on_click=_proxy_enable) \
                    .props('elevated color=positive')
                ui.button(T['proxy_btn_disable'], on_click=_proxy_disable) \
                    .props('elevated color=warning')
                ui.button(T['proxy_btn_restart'], on_click=_proxy_restart) \
                    .props('elevated color=indigo-7')
                ui.button(T['proxy_btn_refresh'], on_click=_proxy_refresh) \
                    .props('flat color=grey-7')

        # ── Agent Status (from clawproxy /proxy/status) ────────────────────
        with ui.card().classes('w-full q-pa-md q-mt-sm'):
            ui.label(T.get('proxy_agents_title', '🔗 Agent Status')).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T.get('proxy_agents_hint', 'Live connection state from clawproxy health probe (refreshes every 30s).')).classes('text-caption text-grey-6 q-mb-sm')

            # ZC row
            zc_status_lbl = ui.label('').classes('text-body2 q-mr-sm')
            zc_reset_btn = ui.button(
                T.get('proxy_zc_reset_pairing', '🔄 Reset Pairing'),
                on_click=lambda: _proxy_reset_pairing('zc'),
            ).props('flat dense color=blue-8 size=sm')

            # PC row
            pc_status_lbl = ui.label('').classes('text-body2 q-mr-sm')
            pc_reset_btn = ui.button(
                T.get('proxy_pc_reset_pairing', '🔄 Reset Pairing'),
            ).props('flat dense color=grey-5 size=sm')
            pc_reset_btn.disable()
            pc_reset_btn.tooltip(T.get('proxy_pc_reset_hint', 'Not yet implemented for PicoClaw'))

            def _proxy_fetch_agent_status():
                """Fetch agent health from clawproxy /proxy/status."""
                zc_text = '⚪ Unknown'
                pc_text = '⚪ Unknown'
                try:
                    req = urllib.request.Request('http://127.0.0.1:18780/proxy/status')
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read().decode('utf-8'))
                    agents = data.get('agents', {})
                    # ── ZC ──
                    zc = agents.get('zc', {})
                    zc_configured = zc.get('configured', False)
                    zc_status = zc.get('status', 'unknown')
                    if not zc_configured:
                        zc_text = '⚫ Not Configured'
                    elif zc_status == 'online':
                        zc_text = '🟢 ZeroClaw Online'
                    elif zc_status == 'offline':
                        zc_text = '🔴 ZeroClaw Offline'
                    elif zc_status == 'auth_error':
                        zc_text = '🟡 ZeroClaw Auth Error'
                    else:
                        zc_text = f'⚪ ZeroClaw: {zc_status}'
                    # ── PC ──
                    pc = agents.get('pc', {})
                    pc_configured = pc.get('configured', False)
                    pc_status_val = pc.get('status', 'unknown')
                    if not pc_configured:
                        pc_text = '⚫ Not Configured'
                    elif pc_status_val == 'online':
                        pc_text = '🟢 PicoClaw Online'
                    elif pc_status_val == 'offline':
                        pc_text = '🔴 PicoClaw Offline'
                    elif pc_status_val == 'auth_error':
                        pc_text = '🟡 PicoClaw Auth Error'
                    else:
                        pc_text = f'⚪ PicoClaw: {pc_status_val}'
                except Exception:
                    zc_text = '⚪ Proxy unreachable'
                    pc_text = '⚪ Proxy unreachable'
                zc_status_lbl.set_text(zc_text)
                pc_status_lbl.set_text(pc_text)

            def _proxy_reset_pairing(agent: str):
                """Delete stored token and restart clawberry-proxy to force re-pair."""
                token_file = f'/opt/clawproxy/{agent}_token'
                try:
                    subprocess.run(
                        ['sudo', 'rm', '-f', token_file],
                        capture_output=True, text=True, timeout=10,
                    )
                except Exception as exc:
                    ui.notify(f'⚠️ Failed to remove token: {exc}', type='warning')
                    return
                try:
                    rr = subprocess.run(
                        ['sudo', '/usr/bin/systemctl', 'restart', 'clawberry-proxy.service'],
                        capture_output=True, text=True, timeout=15,
                    )
                    if rr.returncode == 0:
                        ui.notify(
                            T.get('proxy_reset_ok', '✅ Token cleared & proxy restarted — re-pairing…'),
                            type='positive',
                        )
                    else:
                        err = rr.stderr.strip() or f'exit {rr.returncode}'
                        ui.notify(
                            T.get('proxy_reset_fail', '❌ Reset failed: {err}').format(err=err),
                            type='negative',
                        )
                except Exception as exc:
                    ui.notify(
                        T.get('proxy_reset_fail', '❌ Reset failed: {err}').format(err=str(exc)),
                        type='negative',
                    )
                # Refresh after a short delay to let the proxy restart and re-pair
                ui.timer(3.0, _proxy_fetch_agent_status, once=True)

            with ui.row().classes('w-full items-center gap-2'):
                ui.label(T.get('proxy_zc_label', '🦾 ZeroClaw') + ':').classes('text-body2 text-bold')
                zc_status_lbl
                zc_reset_btn

            with ui.row().classes('w-full items-center gap-2'):
                ui.label(T.get('proxy_pc_label', '🐾 PicoClaw') + ':').classes('text-body2 text-bold')
                pc_status_lbl
                pc_reset_btn

            # Initial fetch + periodic refresh every 5 seconds
            _proxy_fetch_agent_status()
            ui.timer(5.0, _proxy_fetch_agent_status)

        # ── ClawProxy TTS Config ──────────────────────────────────────────────
        with ui.card().classes('w-full q-pa-md q-mt-sm'):
            ui.label(T.get('proxy_cp_cfg_title', '🎤 ClawProxy TTS Config')).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T.get('proxy_cp_cfg_hint', 'Edit clawproxy TTS provider settings (config.toml). Deploy to /opt/clawproxy/ when done.')).classes('text-caption text-grey-6 q-mb-sm')

            cpc_default_provider = ui.select(
                ['mimotts', 'f5tts', 'qwen3tts'],
                label=T.get('proxy_cp_default_provider', 'Default TTS Provider'),
                value='mimotts'
            ).classes('w-full q-mb-xs')
            cpc_default_voice = ui.input(
                T.get('proxy_cp_default_voice', 'Default Voice'),
                value='Chloe'
            ).classes('w-full q-mb-sm')

            # ── mimotts ──
            with ui.expansion('🔊 MimoTTS', value=True).classes('w-full q-mb-xs'):
                cpc_mimo_key = ui.input('API Key', value='').classes('w-full q-mb-xs')
                cpc_mimo_url = ui.input('Base URL', value='https://token-plan-cn.xiaomimimo.com/v1').classes('w-full q-mb-xs')
                cpc_mimo_model = ui.input('Model', value='mimo-v2.5-tts').classes('w-full')

            # ── f5tts ──
            with ui.expansion('🔊 F5-TTS', value=False).classes('w-full q-mb-xs'):
                cpc_f5_url = ui.input('Base URL', value='http://apicn.aiworm.cn:8010').classes('w-full q-mb-xs')
                cpc_f5_key = ui.input('API Key', value='token1').classes('w-full q-mb-xs')
                cpc_f5_speed = ui.number('Speed (0.5–2.0)', value=0.8, min=0.5, max=2.0, step=0.05).classes('w-full')

            # ── qwen3tts ──
            with ui.expansion('🔊 Qwen3-TTS', value=False).classes('w-full q-mb-xs'):
                cpc_qwen_url = ui.input('Base URL', value='http://apicn.aiworm.cn:8012').classes('w-full q-mb-xs')
                cpc_qwen_speed = ui.number('Speed (0.5–2.0)', value=0.9, min=0.5, max=2.0, step=0.05).classes('w-full')

            cpc_status = ui.label('').classes('text-caption q-mt-xs')

            def _cp_cfg_load():
                """Load clawproxy config.toml and populate UI fields."""
                try:
                    c = load_clawproxy_config()
                except Exception as e:
                    ui.notify(f'Failed to parse config.toml: {e}', type='negative')
                    return
                tts = c.get('tts', {}) if c else {}
                cpc_default_provider.set_value(tts.get('default_provider', 'mimotts'))
                cpc_default_voice.set_value(tts.get('default_voice', 'Chloe'))
                mimo = tts.get('mimotts', {})
                cpc_mimo_key.set_value(mimo.get('api_key', ''))
                cpc_mimo_url.set_value(mimo.get('base_url', 'https://token-plan-cn.xiaomimimo.com/v1'))
                cpc_mimo_model.set_value(mimo.get('model', 'mimo-v2.5-tts'))
                f5 = tts.get('f5tts', {})
                cpc_f5_url.set_value(f5.get('base_url', 'http://apicn.aiworm.cn:8010'))
                cpc_f5_key.set_value(f5.get('api_key', 'token1'))
                cpc_f5_speed.set_value(float(f5.get('speed', 0.8)))
                qwen = tts.get('qwen3tts', {})
                cpc_qwen_url.set_value(qwen.get('base_url', 'http://apicn.aiworm.cn:8012'))
                cpc_qwen_speed.set_value(float(qwen.get('speed', 0.9)))
                cpc_status.set_text(T.get('proxy_cp_loaded', '✅ Loaded from local config'))

            def _cp_cfg_save():
                """Save UI values to local clawproxy config.toml."""
                try:
                    c = tomlkit.document()
                    tts = tomlkit.table()
                    tts['default_provider'] = cpc_default_provider.value
                    tts['default_voice'] = cpc_default_voice.value
                    mimo = tomlkit.table()
                    mimo['api_key'] = cpc_mimo_key.value or ''
                    mimo['base_url'] = cpc_mimo_url.value or ''
                    mimo['model'] = cpc_mimo_model.value or ''
                    tts['mimotts'] = mimo
                    f5 = tomlkit.table()
                    f5['base_url'] = cpc_f5_url.value or ''
                    f5['api_key'] = cpc_f5_key.value or ''
                    f5['speed'] = cpc_f5_speed.value
                    tts['f5tts'] = f5
                    qwen = tomlkit.table()
                    qwen['base_url'] = cpc_qwen_url.value or ''
                    qwen['speed'] = cpc_qwen_speed.value
                    tts['qwen3tts'] = qwen
                    c['tts'] = tts
                    save_clawproxy_config(c)
                    cpc_status.set_text(T.get('proxy_cp_saved_ok', '✅ Config saved locally'))
                    ui.notify(T.get('proxy_cp_saved_ok', '✅ clawproxy config.toml saved'), type='positive')
                except Exception as e:
                    cpc_status.set_text(str(e))
                    ui.notify(T.get('proxy_cp_save_err', '❌ Save failed: {}').format(e), type='negative')

            def _cp_cfg_deploy():
                """Save local config, deploy to /opt/clawproxy/config.toml, then restart clawberry-proxy."""
                _cp_cfg_save()
                ok, msg = deploy_clawproxy_config()
                if not ok:
                    cpc_status.set_text(msg)
                    ui.notify(T.get('proxy_cp_deploy_err', '❌ Deploy failed: {}').format(msg), type='negative')
                    return
                # Restart clawberry-proxy so it picks up the new config
                rr = subprocess.run(
                    ['sudo', '/usr/bin/systemctl', 'restart', 'clawberry-proxy.service'],
                    capture_output=True, text=True
                )
                if rr.returncode != 0:
                    err = rr.stderr.strip() or f'systemctl restart failed (exit {rr.returncode})'
                    cpc_status.set_text(f'Config deployed but restart failed: {err}')
                    ui.notify(f'Config deployed but restart failed: {err}', type='warning')
                else:
                    cpc_status.set_text(T.get('proxy_cp_deploy_ok', '✅ Deployed & service restarted'))
                    ui.notify(T.get('proxy_cp_deploy_ok', '✅ clawproxy config deployed & service restarted'), type='positive')

            with ui.row().classes('gap-2 q-mt-sm'):
                ui.button(T.get('proxy_cp_btn_load', '📂 Load'), on_click=_cp_cfg_load).props('flat color=blue-8')
                ui.button(T.get('proxy_cp_btn_save', '💾 Save'), on_click=_cp_cfg_save).props('elevated color=indigo-7')
                ui.button(T.get('proxy_cp_btn_deploy', '🚀 Deploy'), on_click=_cp_cfg_deploy).props('elevated color=positive')

            # Auto-load on first view
            _cp_cfg_load()

    upgrade_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    upgrade_content.set_visibility(False)
    with upgrade_content:
        ui.label(T['upgrade_title']).classes('text-h6 text-orange-9 q-mb-xs')

    upgrade_content_inner = upgrade_content
    with upgrade_content_inner:
        with ui.card().classes('w-full q-pa-md'):
            ui.label(T['upgrade_card_title']).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T['upgrade_hint']).classes('text-caption text-grey-6 q-mb-sm')

            # ── Config-file sync options ──────────────────────────────────
            ui.label(T['upgrade_sync_hint']).classes('text-caption text-grey-7 q-mb-xs')
            with ui.row().classes('gap-4 q-mb-sm'):
                upg_chk_pc_cfg = ui.checkbox(
                    T['upgrade_chk_pc_cfg'], value=False)
                upg_chk_pc_sec = ui.checkbox(
                    T['upgrade_chk_pc_sec'], value=False)
                upg_chk_zc_cfg = ui.checkbox(
                    T['upgrade_chk_zc_cfg'], value=False)

            upg_status_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')
            upg_log_area   = ui.textarea().classes('w-full font-mono').props('outlined rows=16 readonly label="Output"')
            upg_log_area.set_visibility(False)

            _upg_proc = {'proc': None}

            def _start_upgrade():
                # Build a single -config flag with comma-separated targets
                _cmd = ['sudo', 'bash', '/usr/local/bin/clawberry-workspace-sync.sh']
                _checked = [
                    upg_chk_pc_cfg.value,
                    upg_chk_pc_sec.value,
                    upg_chk_zc_cfg.value,
                ]
                if all(_checked):
                    _cmd += ['-config', 'all']
                elif any(_checked):
                    _targets = []
                    if upg_chk_pc_cfg.value: _targets.append('config.json')
                    if upg_chk_pc_sec.value: _targets.append('security.yml')
                    if upg_chk_zc_cfg.value: _targets.append('config.toml')
                    _cmd += ['-config', ','.join(_targets)]

                upg_log_area.set_visibility(True)
                upg_log_area.set_value('')
                _flags = ' '.join(_cmd[4:]) or '(no config files)'
                upg_status_lbl.set_text(f'⏳ Running upgrade script… flags: {_flags}')
                btn_upg_run.props('disabled loading')
                import threading, subprocess as _sp

                def _run_upg(_cmd=_cmd):
                    try:
                        proc = _sp.Popen(
                            _cmd,
                            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                            bufsize=1,
                        )
                        _upg_proc['proc'] = proc
                        for line in proc.stdout:
                            upg_log_area.set_value(upg_log_area.value + line)
                        proc.wait()
                        rc = proc.returncode
                        upg_status_lbl.set_text(
                            f'✅ Upgrade complete (exit {rc})' if rc == 0
                            else f'⚠️ Script exited with code {rc}'
                        )
                    except Exception as exc:
                        upg_status_lbl.set_text(f'❌ Error: {exc}')
                    finally:
                        _upg_proc['proc'] = None
                        btn_upg_run.props(remove='disabled loading')

                threading.Thread(target=_run_upg, daemon=True).start()

            btn_upg_run = ui.button(
                T['upgrade_btn_run'], on_click=_start_upgrade
            ).props('elevated color=orange-9')
    return proxy_content, upgrade_content, upgrade_content_inner, _proxy_refresh
