"""ClawBoard dashboard — openclaw panel."""
import subprocess
from nicegui import ui


from dashboard.config_io import _read_openclaw_deploy_token, deploy_openclaw_config, enable_openclaw_user_service, load_openclaw_config, openclaw_service_is_enabled, restart_openclaw_service, save_openclaw_config
from urllib.parse import quote
from dashboard.provider_hints import _oc_model_ref_text, _oc_provider_models
def build_openclaw_panel(T, conf, lang, _ph_map, _ph_pid_base, _ph_pid_models, _oc_ph_hints, _oc_ph_map, _oc_ph_provs, _oc_ph_pid_base, _oc_ph_pid_models, _build_character_tab, _build_skills_tab, _get_lan_ip):
    """Build panel UI."""
    # ══ OpenClaw Dashboard ════════════════════════════════════════════════════
    oc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    oc_content.set_visibility(False)
    with oc_content:
        with ui.row().classes('w-full items-center q-mb-xs'):
            ui.label(T['oc_dashboard']).classes('text-h6 text-teal-8')
            ui.button(icon='refresh', on_click=lambda: ui.navigate.reload()) \
                .props('flat round dense color=teal-8').tooltip(T['tooltip_reload'])

        # ── Service disabled banner ──────────────────────────────────────
        _oc_svc_enabled = openclaw_service_is_enabled()
        oc_disabled_banner = ui.card().classes('w-full q-pa-md q-mb-sm bg-orange-1')
        oc_disabled_banner.set_visibility(not _oc_svc_enabled)
        with oc_disabled_banner:
            ui.label('⚠️ OpenClaw service is not enabled').classes('text-subtitle2 text-orange-9')
            ui.label(
                'The openclaw.service is currently disabled. '
                'Enable and start it to use the OpenClaw gateway and dashboard.'
            ).classes('text-caption text-grey-7 q-mb-sm')
            oc_enable_result = ui.label('').classes('text-caption q-mb-xs')

            def _oc_enable_service():
                ok, err = enable_openclaw_user_service()
                if ok:
                    oc_enable_result.set_text('✅ Service enabled and started — reloading…')
                    oc_enable_result.classes(remove='text-negative', add='text-positive')
                    ui.notify('✅ openclaw.service enabled & started', type='positive')
                    ui.timer(1.5, lambda: ui.navigate.reload(), once=True)
                else:
                    msg = f'❌ Enable failed: {err}'
                    oc_enable_result.set_text(msg)
                    oc_enable_result.classes(remove='text-positive', add='text-negative')
                    ui.notify(msg, type='negative')

            ui.button('▶ Enable & Start openclaw.service', on_click=_oc_enable_service) \
                .props('color=orange-9 elevated')

        # ── Main dashboard tabs (shown only when service is enabled) ─────
        with ui.tabs().classes('w-full bg-teal-1') as oc_sub_tabs:
            t_oc_wiz    = ui.tab(T['oc_tab_wizard'],        icon='auto_fix_high')
            t_oc_cfg    = ui.tab(T['oc_tab_configuration'], icon='settings')
            t_oc_pair   = ui.tab(T['oc_tab_pair_device'],   icon='devices')
            t_oc_doctor = ui.tab(T['oc_tab_doctor'],         icon='medical_services')
            t_oc_char   = ui.tab(T['tab_characters'],        icon='face')
            t_oc_skills = ui.tab(T['tab_skills'],            icon='extension')

        oc_sub_tabs.set_visibility(_oc_svc_enabled)

        with ui.tab_panels(oc_sub_tabs, value=t_oc_wiz).classes('w-full') as oc_tab_panels:
            pass  # populated below

        oc_tab_panels.set_visibility(_oc_svc_enabled)

        with oc_tab_panels:

            # ── OpenClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_oc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-teal-8 q-mb-xs')
                ui.label(
                    'Configure gateway and primary model in a few steps. '
                    'Click Apply — then restart OpenClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

                _oc_wiz_conf  = load_openclaw_config()
                _oc_wiz_gw    = _oc_wiz_conf.get('gateway', {})
                _oc_wiz_auth  = _oc_wiz_gw.get('auth', {})
                _oc_wiz_ad    = _oc_wiz_conf.get('agents', {}).get('defaults', {})
                _oc_wiz_model = _oc_model_ref_text(_oc_wiz_ad.get('model', ''))
                _oc_wiz_tools = _oc_wiz_conf.get('tools', {})
                _oc_wiz_provider_models = _oc_provider_models(_oc_wiz_conf)

                with ui.stepper(value='oc_wiz_gw').props('vertical animated').classes('w-full') as _oc_wiz:

                    # ── Step 1: Gateway ─────────────────────────────────────
                    with ui.step('oc_wiz_gw', title='1  Gateway', icon='router'):
                        ui.label('Set how OpenClaw listens and authenticates.').classes('text-caption text-grey-6 q-mb-sm')

                        _oc_wiz_bind_opts = ['loopback', 'lan', 'tailnet', 'auto']
                        _oc_wiz_cur_bind  = str(_oc_wiz_gw.get('bind', 'lan'))
                        oc_wiz_bind = ui.select(_oc_wiz_bind_opts, label='gateway.bind',
                            value=_oc_wiz_cur_bind if _oc_wiz_cur_bind in _oc_wiz_bind_opts else 'lan').classes('w-full q-mb-sm')

                        oc_wiz_port = ui.number('gateway.port',
                            value=int(_oc_wiz_gw.get('port', 18789) or 18789),
                            min=1024, max=65535, step=1).classes('w-full q-mb-sm')

                        _oc_wiz_authmode_opts = ['token', 'password', 'none']
                        _oc_wiz_cur_am = str(_oc_wiz_auth.get('mode', 'token'))
                        oc_wiz_auth_mode = ui.select(_oc_wiz_authmode_opts, label='gateway.auth.mode',
                            value=_oc_wiz_cur_am if _oc_wiz_cur_am in _oc_wiz_authmode_opts else 'token').classes('w-full q-mb-sm')

                        oc_wiz_token = ui.input('gateway.auth.token',
                            value=str(_oc_wiz_auth.get('token', '')),
                            password=True, password_toggle_button=True).classes('w-full q-mb-sm')

                        def _oc_wiz_read_live():
                            tok, err = _read_openclaw_deploy_token()
                            if tok:
                                oc_wiz_token.set_value(tok)
                                ui.notify('✅ Token read from deploy path', type='positive')
                            else:
                                ui.notify(f'⚠️ {err or "Token not found"}', type='warning')
                        ui.button(T['oc_token_live'], on_click=_oc_wiz_read_live).props('outline color=teal-8 size=sm').classes('q-mb-sm')

                        with ui.stepper_navigation():
                            ui.button('Next →', on_click=_oc_wiz.next).props('color=teal-8')

                    # ── Step 2: Model & Provider ────────────────────────────
                    with ui.step('oc_wiz_model', title='2  Provider & Model', icon='cloud'):
                        ui.label('Pick a provider and model for agents.').classes('text-caption text-grey-6 q-mb-sm')

                        # Derive initial values from config (OpenClaw schema)
                        _oc_wiz_init_primary = str(_oc_wiz_model)
                        # Provider is derived from the model reference: "provider/model-id"
                        _oc_wiz_init_prov_key = ''
                        if '/' in _oc_wiz_init_primary:
                            _oc_wiz_init_prov_key = _oc_wiz_init_primary.split('/', 1)[0]
                        _oc_wiz_init_model = ''
                        if '/' in _oc_wiz_init_primary:
                            _oc_wiz_init_model = _oc_wiz_init_primary.split('/', 1)[1]
                        else:
                            _oc_wiz_init_model = _oc_wiz_init_primary
                        # Read existing apiKey/baseUrl from models.providers.<name> (OpenClaw schema)
                        _oc_wiz_init_api_key = str(
                            _oc_wiz_provider_models.get(_oc_wiz_init_prov_key, {}).get('apiKey', ''))
                        _oc_wiz_init_base_url = str(
                            _oc_wiz_provider_models.get(_oc_wiz_init_prov_key, {}).get('baseUrl')
                            or _oc_wiz_provider_models.get(_oc_wiz_init_prov_key, {}).get('base_url')
                            or _oc_ph_pid_base.get(_oc_wiz_init_prov_key, ''))

                        # Build option lists — ensure current values appear
                        _oc_wiz_prov_opts = list(_oc_ph_provs)
                        if _oc_wiz_init_prov_key and _oc_wiz_init_prov_key not in _oc_wiz_prov_opts:
                            _oc_wiz_prov_opts = [_oc_wiz_init_prov_key] + _oc_wiz_prov_opts
                        if not _oc_wiz_prov_opts:
                            _oc_wiz_prov_opts = ['']
                        if _oc_wiz_init_prov_key not in _oc_wiz_prov_opts:
                            _oc_wiz_init_prov_key = _oc_wiz_prov_opts[0]

                        _oc_wiz_mname_opts = list(_oc_ph_map.keys())
                        if _oc_wiz_init_model and _oc_wiz_init_model not in _oc_wiz_mname_opts:
                            _oc_wiz_mname_opts = [_oc_wiz_init_model] + _oc_wiz_mname_opts
                        if not _oc_wiz_mname_opts:
                            _oc_wiz_mname_opts = ['']
                        if _oc_wiz_init_model not in _oc_wiz_mname_opts:
                            _oc_wiz_init_model = _oc_wiz_mname_opts[0]

                        ui.label('⚡ Quick pick').classes('text-caption text-teal-7')
                        oc_wiz_quick = ui.select(
                            options=list(_oc_ph_map.keys()),
                            label='Known model',
                            value=None,
                            clearable=True,
                            with_input=True,
                        ).classes('w-full q-mb-xs')
                        ui.separator().classes('q-my-xs')

                        oc_wiz_prov = ui.select(
                            _oc_wiz_prov_opts,
                            label='provider',
                            value=_oc_wiz_init_prov_key,
                            with_input=True,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_model_primary = ui.input(
                            'agents.defaults.model  (provider/model)',
                            value=_oc_wiz_init_primary,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_api_key = ui.input(
                            'Provider API Key',
                            value=_oc_wiz_init_api_key,
                            password=True, password_toggle_button=True,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_base_url = ui.input(
                            'models.providers.<name>.baseUrl',
                            value=_oc_wiz_init_base_url,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_workspace = ui.input('agents.defaults.workspace',
                            value=str(_oc_wiz_ad.get('workspace', '/var/lib/openclaw/.openclaw/workspace'))).classes('w-full q-mb-sm')

                        _oc_wiz_tp_opts = ['minimal', 'coding', 'messaging', 'full']
                        _oc_wiz_cur_tp  = str(_oc_wiz_tools.get('profile', 'coding'))
                        oc_wiz_tools_profile = ui.select(_oc_wiz_tp_opts, label='tools.profile',
                            value=_oc_wiz_cur_tp if _oc_wiz_cur_tp in _oc_wiz_tp_opts else 'coding').classes('w-full q-mb-sm')

                        # Quick-pick autofill
                        def _oc_wiz_fill_hint(e):
                            h = _oc_ph_map.get(e.value) if e.value else None
                            if not h: return
                            prov = h.get('provider', '')
                            if prov in _oc_wiz_prov_opts:
                                oc_wiz_prov.set_value(prov)
                            oc_wiz_model_primary.set_value(h.get('primary', h.get('provider', '') + '/' + h.get('model', '')))
                            if h.get('api_base') and not oc_wiz_base_url.value:
                                oc_wiz_base_url.set_value(h.get('api_base'))
                            if h.get('api_key_required', True) is False:
                                oc_wiz_api_key.set_value('')
                        oc_wiz_quick.on_value_change(_oc_wiz_fill_hint)

                        # Provider selection autofill
                        def _oc_wiz_fill_prov(e):
                            prov = e.value or ''
                            if not prov: return
                            models = _oc_ph_pid_models.get(prov, [])
                            if models:
                                first = _oc_ph_map.get(models[0], {})
                                oc_wiz_model_primary.set_value(first.get('primary', prov + '/' + first.get('model', '')))
                                if first.get('api_base') and not oc_wiz_base_url.value:
                                    oc_wiz_base_url.set_value(first.get('api_base'))
                        oc_wiz_prov.on_value_change(_oc_wiz_fill_prov)

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_oc_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →', on_click=_oc_wiz.next).props('color=teal-8')

                    # ── Step 3: Apply ───────────────────────────────────────
                    with ui.step('oc_wiz_apply', title='3  Apply', icon='check_circle'):
                        ui.label('Review and apply settings.').classes('text-caption text-grey-6 q-mb-sm')

                        def _oc_wiz_summary():
                            return (
                                f'gateway.bind:            {oc_wiz_bind.value}\n'
                                f'gateway.port:            {int(oc_wiz_port.value or 18789)}\n'
                                f'gateway.auth.mode:       {oc_wiz_auth_mode.value}\n'
                                f'gateway.auth.token:      {"(set)" if oc_wiz_token.value else "(empty)"}\n'
                                f'agents.defaults.model:   {oc_wiz_model_primary.value or "(unchanged)"}\n'
                                f'models.providers.{oc_wiz_prov.value or "?"}.baseUrl:  {oc_wiz_base_url.value or "(provider default)"}\n'
                                f'workspace:               {oc_wiz_workspace.value}\n'
                                f'tools.profile:           {oc_wiz_tools_profile.value}'
                            )
                        oc_wiz_summary_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')

                        def _oc_wiz_refresh_summary():
                            oc_wiz_summary_lbl.set_text(_oc_wiz_summary())
                        _oc_wiz.on('transition', lambda _: _oc_wiz_refresh_summary())

                        def _oc_wiz_apply():
                            data = load_openclaw_config()
                            data.setdefault('gateway', {}).setdefault('auth', {})
                            data['gateway']['bind']       = oc_wiz_bind.value or 'lan'
                            data['gateway']['port']       = int(oc_wiz_port.value or 18789)
                            data['gateway']['auth']['mode']  = oc_wiz_auth_mode.value or 'token'
                            if oc_wiz_token.value:
                                data['gateway']['auth']['token'] = oc_wiz_token.value
                            ad = data.setdefault('agents', {}).setdefault('defaults', {})
                            # OpenClaw does NOT have agents.defaults.provider —
                            # the provider is derived from the provider/model-id format.
                            if oc_wiz_model_primary.value:
                                ad['model'] = oc_wiz_model_primary.value
                            if oc_wiz_workspace.value:
                                ad['workspace'] = oc_wiz_workspace.value
                            data.setdefault('tools', {})['profile'] = oc_wiz_tools_profile.value or 'coding'
                            # Save provider config into models.providers.<name> (OpenClaw schema)
                            _prov = oc_wiz_prov.value or ''
                            if _prov and (oc_wiz_api_key.value or oc_wiz_base_url.value):
                                data.setdefault('models', {}).setdefault('providers', {}).setdefault(_prov, {})
                                if oc_wiz_base_url.value:
                                    data['models']['providers'][_prov]['baseUrl'] = oc_wiz_base_url.value
                                if oc_wiz_api_key.value:
                                    data['models']['providers'][_prov]['apiKey'] = oc_wiz_api_key.value
                            try:
                                save_openclaw_config(data)
                                ok_cfg, err_cfg = deploy_openclaw_config()
                                if not ok_cfg:
                                    ui.notify(f'⚠️ Saved locally but deploy failed: {err_cfg}', type='warning')
                                else:
                                    ui.notify('✅ OpenClaw config saved & deployed — restart OpenClaw to activate', type='positive')
                            except Exception as ex:
                                ui.notify(f'❌ {ex}', type='negative')

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_oc_wiz.previous).props('flat color=grey-7')
                            ui.button('✅ Apply & Deploy', on_click=_oc_wiz_apply).props('color=teal-8 elevated')

            # ── OpenClaw › Configuration ───────────────────────────────────
            with ui.tab_panel(t_oc_cfg):
                oc_conf    = load_openclaw_config()
                oc_gateway = oc_conf.get('gateway', {})
                oc_auth    = oc_gateway.get('auth', {})
                oc_agents  = oc_conf.get('agents', {}).get('defaults', {})
                oc_session = oc_conf.get('session', {})
                oc_tools   = oc_conf.get('tools', {})
                oc_ctrl_ui = oc_gateway.get('controlUi', {})
                oc_ts      = oc_gateway.get('tailscale', {})
                oc_provider_models = _oc_provider_models(oc_conf)

                # ── Gateway ──────────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_section_gateway']).classes('text-subtitle2 text-grey-7 q-mt-sm')

                    _oc_mode_opts = ['local', 'remote']
                    _oc_cur_mode  = str(oc_gateway.get('mode', 'local'))
                    oc_w_mode = ui.select(_oc_mode_opts, label='gateway.mode',
                        value=_oc_cur_mode if _oc_cur_mode in _oc_mode_opts else 'local',
                        with_input=True).classes('w-full')

                    oc_w_port = ui.number('gateway.port', value=int(oc_gateway.get('port', 18789) or 18789),
                        min=1024, max=65535, step=1).classes('w-full')

                    _oc_bind_opts = ['loopback', 'lan', 'tailnet', 'auto']
                    _oc_cur_bind  = str(oc_gateway.get('bind', 'lan'))
                    oc_w_bind = ui.select(_oc_bind_opts, label='gateway.bind',
                        value=_oc_cur_bind if _oc_cur_bind in _oc_bind_opts else 'lan',
                        with_input=True).classes('w-full')

                    ui.separator().classes('q-my-xs')
                    ui.label('Auth').classes('text-caption text-grey-6')

                    _oc_authmode_opts = ['token', 'password', 'none']
                    _oc_cur_authmode  = str(oc_auth.get('mode', 'token'))
                    oc_w_auth_mode = ui.select(_oc_authmode_opts, label='gateway.auth.mode',
                        value=_oc_cur_authmode if _oc_cur_authmode in _oc_authmode_opts else 'token').classes('w-full')

                    oc_w_auth_token = ui.input('gateway.auth.token',
                        value=str(oc_auth.get('token', '')),
                        password=True, password_toggle_button=True).classes('w-full')

                    def _oc_read_live_token():
                        tok, err = _read_openclaw_deploy_token()
                        if tok:
                            oc_w_auth_token.set_value(tok)
                            ui.notify(f'✅ Token read from deploy path', type='positive')
                        else:
                            ui.notify(f'⚠️ {err or "Token not found"}', type='warning')
                    ui.button(T['oc_token_live'], on_click=_oc_read_live_token).props('outline color=teal-8 size=sm').classes('q-mt-xs')

                    ui.separator().classes('q-my-xs')
                    ui.label('controlUi.allowedOrigins (one per line)').classes('text-caption text-grey-6')
                    _oc_origins = oc_ctrl_ui.get('allowedOrigins') or []
                    oc_w_allowed_origins = ui.textarea(value='\n'.join(_oc_origins)).classes('w-full').props('outlined rows=3')

                    ui.separator().classes('q-my-xs')
                    ui.label('Tailscale').classes('text-caption text-grey-6')
                    _oc_ts_opts = ['off', 'serve', 'funnel']
                    _oc_cur_ts  = str(oc_ts.get('mode', 'off'))
                    oc_w_ts_mode = ui.select(_oc_ts_opts, label='gateway.tailscale.mode',
                        value=_oc_cur_ts if _oc_cur_ts in _oc_ts_opts else 'off').classes('w-full')
                    oc_w_ts_reset = ui.checkbox('gateway.tailscale.resetOnExit',
                        value=bool(oc_ts.get('resetOnExit', False)))

                # ── Agents & Session ─────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_section_agents']).classes('text-subtitle2 text-grey-7')

                    oc_w_workspace = ui.input('agents.defaults.workspace',
                        value=str(oc_agents.get('workspace', '/var/lib/openclaw/.openclaw/workspace'))).classes('w-full')

                    ui.separator().classes('q-my-xs')
                    ui.label('Provider & Model').classes('text-caption text-grey-6')

                    # Derive current defaults from the OpenClaw model reference
                    _oc_model_primary = _oc_model_ref_text(oc_agents.get('model', ''))
                    _oc_cfg_init_prov = ''
                    if '/' in _oc_model_primary:
                        _oc_cfg_init_prov = _oc_model_primary.split('/', 1)[0]
                    _oc_cfg_prov_opts = list(_oc_ph_provs)
                    if _oc_cfg_init_prov and _oc_cfg_init_prov not in _oc_cfg_prov_opts:
                        _oc_cfg_prov_opts = [_oc_cfg_init_prov] + _oc_cfg_prov_opts
                    if not _oc_cfg_prov_opts:
                        _oc_cfg_prov_opts = ['']
                    if _oc_cfg_init_prov not in _oc_cfg_prov_opts:
                        _oc_cfg_init_prov = _oc_cfg_prov_opts[0]
                    # Read existing apiKey/baseUrl from models.providers.<name> (OpenClaw schema)
                    _oc_cfg_init_api_key = str(
                        oc_provider_models.get(_oc_cfg_init_prov, {}).get('apiKey', ''))
                    _oc_cfg_init_base_url = str(
                        oc_provider_models.get(_oc_cfg_init_prov, {}).get('baseUrl')
                        or oc_provider_models.get(_oc_cfg_init_prov, {}).get('base_url')
                        or _oc_ph_pid_base.get(_oc_cfg_init_prov, ''))

                    oc_w_cfg_quick = ui.select(
                        options=list(_oc_ph_map.keys()),
                        label='⚡ Quick pick model',
                        value=None,
                        clearable=True,
                        with_input=True,
                    ).classes('w-full q-mb-xs')

                    oc_w_provider = ui.select(
                        _oc_cfg_prov_opts,
                        label='provider',
                        value=_oc_cfg_init_prov,
                        with_input=True,
                    ).classes('w-full')

                    oc_w_model_primary = ui.input(
                        'agents.defaults.model  (provider/model)',
                        value=_oc_model_primary,
                    ).classes('w-full')

                    oc_w_api_key = ui.input(
                        'Provider API Key',
                        value=_oc_cfg_init_api_key,
                        password=True, password_toggle_button=True,
                    ).classes('w-full')

                    oc_w_base_url = ui.input(
                        'models.providers.<name>.baseUrl',
                        value=_oc_cfg_init_base_url,
                    ).classes('w-full')

                    # Quick-pick autofill
                    def _oc_cfg_fill_hint(e):
                        h = _oc_ph_map.get(e.value) if e.value else None
                        if not h: return
                        prov = h.get('provider', '')
                        if prov in _oc_cfg_prov_opts:
                            oc_w_provider.set_value(prov)
                        oc_w_model_primary.set_value(h.get('primary', prov + '/' + h.get('model', '')))
                        if h.get('api_base') and not oc_w_base_url.value:
                            oc_w_base_url.set_value(h.get('api_base'))
                        if h.get('api_key_required', True) is False:
                            oc_w_api_key.set_value('')
                    oc_w_cfg_quick.on_value_change(_oc_cfg_fill_hint)

                    # Provider selection autofill
                    def _oc_cfg_fill_prov(e):
                        prov = e.value or ''
                        if not prov: return
                        models = _oc_ph_pid_models.get(prov, [])
                        if models:
                            first = _oc_ph_map.get(models[0], {})
                            oc_w_model_primary.set_value(first.get('primary', prov + '/' + first.get('model', '')))
                            if first.get('api_base') and not oc_w_base_url.value:
                                oc_w_base_url.set_value(first.get('api_base'))
                        # Load existing apiKey for newly selected provider (OpenClaw schema)
                        existing_key = str(oc_provider_models.get(prov, {}).get('apiKey', ''))
                        if existing_key:
                            oc_w_api_key.set_value(existing_key)
                    oc_w_provider.on_value_change(_oc_cfg_fill_prov)

                    ui.separator().classes('q-my-xs')
                    _oc_dm_opts = ['per-channel-peer', 'global', 'per-channel']
                    _oc_cur_dm  = str(oc_session.get('dmScope', 'per-channel-peer'))
                    oc_w_dm_scope = ui.select(_oc_dm_opts, label='session.dmScope',
                        value=_oc_cur_dm if _oc_cur_dm in _oc_dm_opts else 'per-channel-peer').classes('w-full')

                # ── Tools ────────────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_section_tools']).classes('text-subtitle2 text-grey-7')

                    _oc_tp_opts = ['minimal', 'coding', 'messaging', 'full']
                    _oc_cur_tp  = str(oc_tools.get('profile', 'coding'))
                    oc_w_tools_profile = ui.select(_oc_tp_opts, label='tools.profile',
                        value=_oc_cur_tp if _oc_cur_tp in _oc_tp_opts else 'coding',
                        with_input=True).classes('w-full')

                # ── Action buttons ───────────────────────────────────────
                def oc_collect_and_save():
                    """Collect all widget values back into the openclaw config dict and save locally."""
                    data = load_openclaw_config()
                    data.setdefault('gateway', {})
                    data.setdefault('agents', {}).setdefault('defaults', {})
                    data.setdefault('session', {})
                    data.setdefault('tools', {})

                    data['gateway']['mode']  = oc_w_mode.value or 'local'
                    data['gateway']['port']  = int(oc_w_port.value or 18789)
                    data['gateway']['bind']  = oc_w_bind.value or 'lan'
                    data['gateway'].setdefault('auth', {})
                    data['gateway']['auth']['mode']  = oc_w_auth_mode.value or 'token'
                    data['gateway']['auth']['token'] = oc_w_auth_token.value or ''
                    data['gateway'].setdefault('controlUi', {})
                    origins_raw = [l.strip() for l in oc_w_allowed_origins.value.splitlines() if l.strip()]
                    data['gateway']['controlUi']['allowedOrigins'] = origins_raw
                    data['gateway'].setdefault('tailscale', {})
                    data['gateway']['tailscale']['mode']        = oc_w_ts_mode.value or 'off'
                    data['gateway']['tailscale']['resetOnExit'] = oc_w_ts_reset.value

                    data['agents']['defaults']['workspace'] = oc_w_workspace.value or ''
                    # OpenClaw does NOT have agents.defaults.provider
                    data['agents']['defaults']['model']    = oc_w_model_primary.value or ''

                    # Save provider config into models.providers.<name> (OpenClaw schema)
                    _cfg_prov = oc_w_provider.value or ''
                    if _cfg_prov and (oc_w_api_key.value or oc_w_base_url.value):
                        data.setdefault('models', {}).setdefault('providers', {}).setdefault(_cfg_prov, {})
                        if oc_w_base_url.value:
                            data['models']['providers'][_cfg_prov]['baseUrl'] = oc_w_base_url.value
                        if oc_w_api_key.value:
                            data['models']['providers'][_cfg_prov]['apiKey'] = oc_w_api_key.value

                    data['session']['dmScope'] = oc_w_dm_scope.value or 'per-channel-peer'
                    data['tools']['profile']   = oc_w_tools_profile.value or 'coding'

                    try:
                        save_openclaw_config(data)
                        ui.notify(T['oc_notify_saved'], type='positive')
                        return True
                    except Exception as e:
                        ui.notify(T['oc_notify_save_fail'].format(e), type='negative')
                        return False

                def oc_do_save_restart():
                    if not oc_collect_and_save():
                        return
                    ok_cfg, err_cfg = deploy_openclaw_config()
                    if not ok_cfg:
                        ui.notify(f'⚠️ Deploy failed: {err_cfg}', type='warning')
                        return
                    ok_svc, svc_err = restart_openclaw_service()
                    if ok_svc:
                        ui.notify('✅ OpenClaw deployed & restarted', type='positive')
                    else:
                        ui.notify(f'⚠️ Restart failed: {svc_err or T["notify_sudo_required"]}', type='warning')

                ui.separator()
                with ui.row().classes('w-full gap-2 q-pa-sm'):
                    ui.button(T['oc_btn_save'],         on_click=oc_collect_and_save).props('elevated').classes('flex-1 bg-teal-8 text-white')
                    ui.button(T['oc_btn_save_restart'],  on_click=oc_do_save_restart).props('elevated').classes('flex-1 bg-teal-10 text-white')

            # ── OpenClaw › Pair Device ─────────────────────────────────────
            with ui.tab_panel(t_oc_pair):
                oc_tok, oc_tok_err = _read_openclaw_deploy_token()
                oc_port = int(load_openclaw_config().get('gateway', {}).get('port', 18789) or 18789)
                oc_host   = _get_lan_ip() or request.url.hostname or 'localhost'
                oc_scheme = request.url.scheme or 'http'
                oc_url    = f'{oc_scheme}://{oc_host}:{oc_port}?token={oc_tok}' if oc_tok else ''
                oc_qr_url = f'https://quickchart.io/qr?size=260&margin=1&text={quote(oc_url, safe="")}' if oc_url else ''
                oc_tok_qr = f'https://quickchart.io/qr?size=260&margin=1&text={quote(oc_tok, safe="")}' if oc_tok else ''

                # ── QR / token card ──────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_pair_title']).classes('text-h6 text-teal-8')
                    if oc_tok_err:
                        ui.label(f'⚠️ {oc_tok_err}').classes('text-warning text-caption q-mt-xs')
                    if oc_tok:
                        ui.input('gateway.auth.token', value=oc_tok).props('readonly').classes('w-full q-mt-sm')
                        if oc_url:
                            ui.input('Pairing URL', value=oc_url).props('readonly').classes('w-full')

                        with ui.row().classes('w-full items-start gap-4 q-mt-sm'):
                            if oc_qr_url:
                                with ui.card().classes('q-pa-sm items-center bg-white'):
                                    ui.label(T['oc_pair_qr']).classes('text-caption text-grey-7 q-mb-xs')
                                    ui.image(oc_qr_url).classes('w-56 h-56')

                            with ui.card().classes('q-pa-sm items-center bg-white'):
                                ui.label(T['oc_gateway_token_qr']).classes('text-caption text-grey-7 q-mb-xs')
                                ui.image(oc_tok_qr).classes('w-56 h-56')

                            with ui.column().classes('gap-2 q-mt-md'):
                                def _copy_oc_url(url=oc_url):
                                    ui.clipboard.write(url)
                                    ui.notify('✅ URL copied', type='positive')
                                if oc_url:
                                    ui.button('📋 Copy URL', on_click=_copy_oc_url).props('outline color=teal-8')
                    else:
                        ui.label(T['oc_pair_missing_token']).classes('text-negative q-mt-sm')

                # ── Device lists ─────────────────────────────────────────
                def _oc_load_devices():
                    """Run `openclaw devices list --json` as the openclaw system user.
                    Passes --token and --url so the gateway auth token (operator.read scope)
                    is used instead of the CLI paired device (operator.pairing scope only)."""
                    gw_url = f'ws://localhost:{oc_port}'
                    cmd = ['sudo', '-u', 'openclaw', '-i',
                           'openclaw', 'devices', 'list', '--json']
                    if oc_tok:
                        cmd += ['--url', gw_url, '--token', oc_tok]
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode != 0:
                        return None, r.stderr.strip() or f'exit {r.returncode}'
                    # The login shell may print banners before AND after the JSON.
                    # Find the first '{' or '[', then use raw_decode to consume
                    # exactly one JSON value and ignore any trailing shell output.
                    out = r.stdout
                    start = -1
                    for ch in ('{', '['):
                        idx = out.find(ch)
                        if idx != -1 and (start == -1 or idx < start):
                            start = idx
                    if start == -1:
                        return None, f'No JSON found in output: {out[:200]}'
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(out, start)
                        return obj, ''
                    except Exception as e:
                        return None, f'Parse error: {e}  raw={out[start:start+200]}'

                async def _oc_approve_device(request_id: str, name_lbl, btn=None):
                    if btn:
                        btn.disable()
                    gw_url = f'ws://localhost:{oc_port}'
                    cmd = ['sudo', '-u', 'openclaw', '-i',
                           'openclaw', 'devices', 'approve', request_id]
                    if oc_tok:
                        cmd += ['--url', gw_url, '--token', oc_tok]
                    import asyncio as _asyncio
                    try:
                        r = await _asyncio.to_thread(
                            subprocess.run, cmd, capture_output=True, text=True, timeout=30
                        )
                    except subprocess.TimeoutExpired:
                        ui.notify('❌ Timed out waiting for approval response', type='negative')
                        if btn:
                            btn.enable()
                        return
                    except Exception as exc:
                        ui.notify(f'❌ Unexpected error: {exc}', type='negative')
                        if btn:
                            btn.enable()
                        return
                    if r.returncode == 0:
                        ui.notify(f'✅ Approved {request_id[:8]}…', type='positive')
                        name_lbl.set_text('✅ Approved')
                    else:
                        err = r.stderr.strip() or r.stdout.strip() or f'exit {r.returncode}'
                        ui.notify(f'❌ {err}', type='negative')
                        if btn:
                            btn.enable()

                def _oc_fmt_ts(ms):
                    if not ms:
                        return ''
                    try:
                        from datetime import datetime as _dt
                        return _dt.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M')
                    except Exception:
                        return str(ms)

                # ── Pending approvals ────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    with ui.row().classes('w-full items-center justify-between q-mb-sm'):
                        ui.label('⏳ Pending Approval').classes('text-subtitle1 text-bold text-orange-8')
                        def _oc_refresh_devices():
                            _pending_col.clear()
                            _paired_col.clear()
                            _oc_populate_devices()
                        ui.button(icon='refresh', on_click=_oc_refresh_devices) \
                            .props('flat round dense color=teal-8').tooltip('Refresh')

                    _pending_col = ui.column().classes('w-full gap-2')

                # ── Paired devices ───────────────────────────────────────
                with ui.card().classes('w-full q-pa-md'):
                    ui.label('📱 Paired Devices').classes('text-subtitle1 text-bold text-teal-8 q-mb-sm')
                    _paired_col = ui.column().classes('w-full gap-2')

                def _oc_populate_devices():
                    devs, err = _oc_load_devices()

                    # ── Pending ──────────────────────────────────────────
                    with _pending_col:
                        if err and devs is None:
                            ui.label(f'⚠️ {err}').classes('text-caption text-negative')
                        else:
                            pending = (devs or {}).get('pending', [])
                            if not pending:
                                ui.label('No pending requests').classes('text-caption text-grey-6')
                            for dev in pending:
                                rid   = dev.get('requestId', '')
                                dname = dev.get('displayName') or dev.get('deviceId', '?')[:16] + '…'
                                plat  = dev.get('platform', '')
                                fam   = dev.get('deviceFamily', '')
                                rip   = dev.get('remoteIp', '')
                                roles = ', '.join(dev.get('roles') or [])
                                ts    = _oc_fmt_ts(dev.get('ts'))
                                with ui.card().classes('w-full q-pa-sm bg-orange-1'):
                                    with ui.row().classes('w-full items-center justify-between'):
                                        with ui.column().classes('gap-0'):
                                            ui.label(f'📲 {dname}').classes('text-bold')
                                            ui.label(f'{fam or plat}  ·  {rip}  ·  {roles}').classes('text-caption text-grey-7')
                                            ui.label(f'Request ID: {rid}').classes('text-caption text-mono text-grey-6')
                                            ui.label(f'Requested: {ts}').classes('text-caption text-grey-6')
                                        _approve_lbl = ui.label('')
                                        _approve_btn = ui.button('✅ Approve').props('color=teal-8 size=sm')
                                        async def _on_approve(_rid=rid, _lbl=_approve_lbl, _btn=_approve_btn):
                                            await _oc_approve_device(_rid, _lbl, _btn)
                                        _approve_btn.on_click(_on_approve)

                    # ── Paired ───────────────────────────────────────────
                    with _paired_col:
                        if devs is not None:
                            paired = devs.get('paired', [])
                            if not paired:
                                ui.label('No paired devices').classes('text-caption text-grey-6')
                            for dev in paired:
                                dname   = dev.get('displayName') or dev.get('clientId', '?')
                                plat    = dev.get('platform', '')
                                fam     = dev.get('deviceFamily', '')
                                rip     = dev.get('remoteIp', '')
                                role    = dev.get('role', '')
                                roles   = ', '.join(dev.get('roles') or [])
                                created = _oc_fmt_ts(dev.get('createdAtMs'))
                                mode    = dev.get('clientMode', '')
                                with ui.card().classes('w-full q-pa-sm bg-teal-1'):
                                    ui.label(f'📱 {dname}').classes('text-bold')
                                    ui.label(f'{fam or plat}  ·  {rip}  ·  role: {role}  ·  mode: {mode}').classes('text-caption text-grey-7')
                                    ui.label(f'Roles: {roles}').classes('text-caption text-grey-6')
                                    if created:
                                        ui.label(f'Paired: {created}').classes('text-caption text-grey-6')

                _oc_populate_devices()

            # ── OpenClaw › Doctor ──────────────────────────────────────────
            with ui.tab_panel(t_oc_doctor):
                import asyncio as _asyncio_dr

                async def _oc_run_cmd_async(cmd):
                    try:
                        return await _asyncio_dr.to_thread(
                            subprocess.run, cmd,
                            capture_output=True, text=True, timeout=60
                        )
                    except subprocess.TimeoutExpired:
                        return None

                # ── Status card ──────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    with ui.row().classes('w-full items-center justify-between q-mb-sm'):
                        ui.label('📊 OpenClaw Status').classes('text-subtitle1 text-bold text-teal-8')
                        _status_btn = ui.button('▶ Run openclaw status', icon='play_arrow').props('color=teal-8 elevated size=sm')

                    _status_out = ui.column().classes('w-full gap-1')

                    async def _oc_run_status(_btn=_status_btn, _out=_status_out):
                        _btn.disable()
                        _out.clear()
                        with _out:
                            ui.spinner('dots', size='sm').classes('text-teal-8')
                        oc_tok_s, _ = _read_openclaw_deploy_token()
                        oc_port_s = int(load_openclaw_config().get('gateway', {}).get('port', 18789) or 18789)
                        cmd = ['sudo', '-u', 'openclaw', '-i', 'openclaw', 'status', '--json']
                        if oc_tok_s:
                            cmd += ['--url', f'ws://localhost:{oc_port_s}', '--token', oc_tok_s]
                        r = await _oc_run_cmd_async(cmd)
                        _out.clear()
                        with _out:
                            if r is None:
                                ui.label('❌ Timed out').classes('text-negative text-caption')
                                _btn.enable()
                                return
                            if r.returncode != 0:
                                err = r.stderr.strip() or r.stdout.strip() or f'exit {r.returncode}'
                                ui.label(f'❌ {err}').classes('text-negative text-caption')
                                _btn.enable()
                                return
                            # Parse JSON output
                            raw = r.stdout
                            start = -1
                            for ch in ('{', '['):
                                idx = raw.find(ch)
                                if idx != -1 and (start == -1 or idx < start):
                                    start = idx
                            if start == -1:
                                ui.label(raw or '(no output)').classes('text-caption text-mono')
                                _btn.enable()
                                return
                            try:
                                obj, _ = json.JSONDecoder().raw_decode(raw, start)
                            except Exception:
                                ui.label(raw).classes('text-caption text-mono')
                                _btn.enable()
                                return
                            # Render parsed status fields
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    color = 'text-positive' if str(v).lower() in ('true', 'active', 'ok', 'running', 'online') \
                                            else 'text-negative' if str(v).lower() in ('false', 'inactive', 'error', 'failed', 'offline') \
                                            else 'text-grey-8'
                                    with ui.row().classes('gap-2 items-center'):
                                        ui.label(f'{k}:').classes('text-caption text-grey-6 text-bold')
                                        ui.label(str(v)).classes(f'text-caption {color}')
                            else:
                                ui.label(str(obj)).classes('text-caption text-mono')
                            _btn.enable()

                    _status_btn.on_click(_oc_run_status)

                # ── Doctor card ───────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md'):
                    with ui.row().classes('w-full items-center justify-between q-mb-sm'):
                        ui.label('🩺 OpenClaw Doctor').classes('text-subtitle1 text-bold text-teal-8')
                        _doctor_btn = ui.button('▶ Run openclaw doctor --fix', icon='build').props('color=orange-8 elevated size=sm')

                    ui.label('Checks and auto-fixes missing dependencies, config issues, and service health.') \
                        .classes('text-caption text-grey-7 q-mb-sm')

                    _doctor_out = ui.log(max_lines=200).classes('w-full').style(
                        'height:320px; font-size:12px; background:#1e1e1e; color:#d4d4d4; border-radius:4px;'
                    )

                    async def _oc_run_doctor(_btn=_doctor_btn, _log=_doctor_out):
                        _btn.disable()
                        _log.clear()

                        async def _svc(action: str) -> bool:
                            cmd = ['sudo', '-u', 'openclaw', '-i',
                                   'systemctl', '--user', action, 'openclaw']
                            _log.push('$ ' + ' '.join(cmd))
                            r = await _oc_run_cmd_async(cmd)
                            if r is None:
                                _log.push(f'❌ systemctl {action} timed out')
                                return False
                            out = (r.stdout or '').strip() + (r.stderr or '').strip()
                            if out:
                                _log.push(out)
                            if r.returncode != 0:
                                _log.push(f'❌ systemctl {action} failed (exit {r.returncode})')
                                return False
                            _log.push(f'✅ openclaw service {action}ped')
                            return True

                        # 1. Stop service
                        if not await _svc('stop'):
                            ui.notify('❌ Could not stop openclaw service', type='negative')
                            _btn.enable()
                            return

                        # 2. Run doctor --fix
                        cmd = ['sudo', '-u', 'openclaw', '-i', 'openclaw', 'doctor', '--fix']
                        _log.push('$ ' + ' '.join(cmd))
                        r = await _oc_run_cmd_async(cmd)
                        if r is None:
                            _log.push('❌ Timed out after 60 s')
                            ui.notify('❌ Doctor timed out', type='negative')
                        else:
                            output = (r.stdout or '') + (r.stderr or '')
                            for line in output.splitlines():
                                _log.push(line)
                            if r.returncode == 0:
                                ui.notify('✅ Doctor finished', type='positive')
                            else:
                                ui.notify(f'⚠️ Doctor exited {r.returncode}', type='warning')

                        # 3. Restart service regardless of doctor outcome
                        await _svc('start')
                        _btn.enable()

                    _doctor_btn.on_click(_oc_run_doctor)

            # ── OpenClaw › Characters ──────────────────────────────────────
            with ui.tab_panel(t_oc_char):
                _build_character_tab('/var/lib/openclaw/.openclaw/workspace', 'openclaw', 'teal-8')

            # ── OpenClaw › Skills ─────────────────────────────────────────
            with ui.tab_panel(t_oc_skills):
                _build_skills_tab('/var/lib/openclaw/.openclaw/workspace', 'openclaw', 'teal-8')
    return oc_content
