"""ClawBoard dashboard — picoclaw panel."""
import os
import subprocess
from nicegui import ui


from dashboard.paths import PICOCLAW_CONFIG_PATH, SCRIPT_DIR
from dashboard.config_io import _read_security_yml_token, deploy_picoclaw_config, deploy_picoclaw_security, load_picoclaw_config, load_picoclaw_security, save_picoclaw_config, save_picoclaw_security
from dashboard.provider_hints import PROVIDER_IDS
from urllib.parse import quote
def build_picoclaw_panel(T, conf, lang, _ph_map, _ph_pid_base, _ph_pid_models, _pc_ph_hints, _pc_ph_map, _pc_ph_provs, _pc_ph_pid_base, _pc_ph_pid_models, _build_character_tab, _build_skills_tab, _get_lan_ip, restart_picoclaw_service, setup_pico_channel_token):
    """Build panel UI."""
    # ══ PicoClaw Dashboard ════════════════════════════════════════════════════
    pc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    pc_content.set_visibility(False)
    with pc_content:
        with ui.row().classes('w-full items-center q-mb-xs'):
            ui.label(T['pc_dashboard']).classes('text-h6 text-purple-8')
            ui.button(icon='refresh', on_click=lambda: ui.navigate.reload()) \
                .props('flat round dense color=purple-8').tooltip(T['tooltip_reload'])
        with ui.tabs().classes('w-full bg-purple-1') as pc_sub_tabs:
            t_pc_wiz  = ui.tab(T['pc_tab_wizard'],     icon='auto_fix_high')
            t_pc_cfg  = ui.tab(T['tab_configuration'], icon='settings')
            t_pc_pair = ui.tab(T['tab_pair_device'],   icon='devices')
            t_pc_char   = ui.tab(T['tab_characters'],    icon='face')
            t_pc_skills = ui.tab(T['tab_skills'],        icon='extension')

        with ui.tab_panels(pc_sub_tabs, value=t_pc_cfg).classes('w-full'):

            # ── PicoClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_pc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-purple-8 q-mb-xs')
                ui.label(
                    'Configure provider, tools and security in 3 steps. '
                    'Click Apply — then restart PicoClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

                # ── Pre-load current config values to populate wizard ─────
                _pc_wiz_cur_conf  = load_picoclaw_config()
                _pc_wiz_cur_ad    = _pc_wiz_cur_conf.get('agents', {}).get('defaults', {})
                _pc_wiz_cur_ml    = _pc_wiz_cur_conf.get('model_list', [])
                _pc_wiz_cur_sec   = load_picoclaw_security()
                _pc_wiz_init_prov  = _pc_wiz_cur_ad.get('provider', _pc_ph_provs[0] if _pc_ph_provs else '')
                _pc_wiz_init_mname = _pc_wiz_cur_ad.get('model_name', None)
                _pc_wiz_init_model = _pc_wiz_cur_ad.get('model', '')
                _pc_wiz_cur_mle    = next(
                    (e for e in _pc_wiz_cur_ml if e.get('model_name') == _pc_wiz_init_mname), {})
                _pc_wiz_init_api_base = (
                    _pc_wiz_cur_mle.get('api_base', '') or
                    _pc_ph_pid_base.get(_pc_wiz_init_prov, ''))
                _pc_wiz_init_auth  = _pc_wiz_cur_mle.get('auth_method', 'apikey')
                _pc_wiz_sec_ml     = _pc_wiz_cur_sec.get('model_list', {})
                _pc_wiz_init_keys  = (
                    _pc_wiz_sec_ml.get(_pc_wiz_init_mname or '', {}).get('api_keys') or
                    _pc_wiz_sec_ml.get(f'{_pc_wiz_init_mname}:0', {}).get('api_keys') or [])
                _pc_wiz_init_api_key = _pc_wiz_init_keys[0] if _pc_wiz_init_keys else ''
                # Ensure current provider/model_name appear in option lists
                _pc_wiz_prov_opts  = list(_pc_ph_provs)
                if _pc_wiz_init_prov and _pc_wiz_init_prov not in _pc_wiz_prov_opts:
                    _pc_wiz_prov_opts = [_pc_wiz_init_prov] + _pc_wiz_prov_opts
                if not _pc_wiz_prov_opts:
                    _pc_wiz_prov_opts = [PROVIDER_IDS[0]]
                if _pc_wiz_init_prov not in _pc_wiz_prov_opts:
                    _pc_wiz_init_prov = _pc_wiz_prov_opts[0]
                _pc_wiz_mname_opts = list(_pc_ph_map.keys())
                if _pc_wiz_init_mname and _pc_wiz_init_mname not in _pc_wiz_mname_opts:
                    _pc_wiz_mname_opts = [_pc_wiz_init_mname] + _pc_wiz_mname_opts
                if not _pc_wiz_mname_opts:
                    _pc_wiz_mname_opts = ['']
                if _pc_wiz_init_mname not in _pc_wiz_mname_opts:
                    _pc_wiz_init_mname = _pc_wiz_mname_opts[0]

                with ui.stepper(value='pc_wiz_prov').props('vertical animated').classes('w-full') as _pc_wiz:

                    # ── Step 1: Provider + Model ────────────────────────────
                    with ui.step('pc_wiz_prov', title='1  Provider & Model', icon='cloud'):
                        ui.label('Pick a known model — fields fill automatically.').classes('text-caption text-grey-6 q-mb-sm')

                        ui.label('⚡ Quick pick').classes('text-caption text-purple-7')
                        pc_wiz_quick = ui.select(
                            options=list(_pc_ph_map.keys()),
                            label='Known model',
                            value=None,
                            clearable=True,
                            with_input=True,
                        ).classes('w-full q-mb-xs')
                        ui.separator().classes('q-my-xs')

                        pc_wiz_prov      = ui.select(_pc_wiz_prov_opts, label='provider',
                            value=_pc_wiz_init_prov).classes('w-full q-mb-sm')
                        pc_wiz_model_name= ui.select(
                            options=_pc_wiz_mname_opts,
                            label='model_name',
                            value=_pc_wiz_init_mname,
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full q-mb-sm')
                        pc_wiz_model     = ui.input('model  (actual model id sent to provider)', value=_pc_wiz_init_model).classes('w-full q-mb-sm')
                        pc_wiz_api_base  = ui.input('api_base', value=_pc_wiz_init_api_base).classes('w-full q-mb-sm')
                        _pc_wiz_auth_opts  = ['apikey', 'oauth']
                        pc_wiz_auth_method = ui.select(_pc_wiz_auth_opts, label='auth_method',
                            value=_pc_wiz_init_auth if _pc_wiz_init_auth in _pc_wiz_auth_opts else 'apikey',
                        ).classes('w-full q-mb-sm')
                        pc_wiz_api_key   = ui.input('api_key', value=_pc_wiz_init_api_key,
                            password=True, password_toggle_button=True).classes('w-full')

                        def _pc_wiz_fill_hint(e):
                            h = _pc_ph_map.get(e.value) if e.value else None
                            if not h: return
                            prov = h.get('provider', '')
                            if prov in _pc_wiz_prov_opts: pc_wiz_prov.set_value(prov)
                            pc_wiz_model_name.set_value(h.get('model_name', ''))
                            pc_wiz_model.set_value(h.get('model', ''))
                            pc_wiz_api_base.set_value(h.get('api_base', ''))
                            _am = h.get('auth_method', 'apikey')
                            pc_wiz_auth_method.set_value(_am if _am in _pc_wiz_auth_opts else 'apikey')
                        pc_wiz_quick.on_value_change(_pc_wiz_fill_hint)

                        def _pc_wiz_fill_prov(e):
                            prov = e.value or ''
                            if not prov: return
                            pc_wiz_api_base.set_value(_pc_ph_pid_base.get(prov, ''))
                            models = _pc_ph_pid_models.get(prov, [])
                            if models:
                                pc_wiz_model_name.set_options(models, value=models[0])
                                first = _pc_ph_map.get(models[0], {})
                                pc_wiz_model.set_value(first.get('model', ''))
                        pc_wiz_prov.on_value_change(_pc_wiz_fill_prov)

                        def _pc_wiz_fill_mname(e):
                            h = _pc_ph_map.get(e.value) if e.value else None
                            if not h: return
                            pc_wiz_model.set_value(h.get('model', ''))
                            pc_wiz_api_base.set_value(h.get('api_base', ''))
                        pc_wiz_model_name.on_value_change(_pc_wiz_fill_mname)

                        with ui.stepper_navigation():
                            ui.button('Next →', on_click=_pc_wiz.next).props('color=purple-8')

                    # ── Step 2: Tools & Security ─────────────────────────────
                    with ui.step('pc_wiz_tools_sec', title='2  Tools & Security', icon='security'):
                        ui.label('Enable tools and set workspace security.').classes('text-caption text-grey-6 q-mb-sm')

                        ui.label('🔧 Tools').classes('text-subtitle2 text-purple-8 q-mb-xs')
                        pc_wiz_tool_web_search = ui.checkbox('Web Search', value=True)
                        pc_wiz_tool_exec       = ui.checkbox('exec  (shell command execution)', value=True)

                        ui.separator().classes('q-my-sm')
                        ui.label('🔒 Security').classes('text-subtitle2 text-purple-8 q-mb-xs')
                        pc_wiz_sec_restrict   = ui.checkbox('restrict_to_workspace', value=True)
                        pc_wiz_sec_allow_read = ui.checkbox('allow_read_outside_workspace', value=False)

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_pc_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →', on_click=_pc_wiz.next).props('color=purple-8')

                    # ── Step 3: Apply ────────────────────────────────────────
                    with ui.step('pc_wiz_apply', title='3  Apply', icon='check_circle'):
                        ui.label('Review and apply the provider settings.').classes('text-caption text-grey-6 q-mb-sm')

                        def _pc_wiz_summary():
                            return (
                                f'provider: {pc_wiz_prov.value}\n'
                                f'auth_method: {pc_wiz_auth_method.value}\n'
                                f'model_name: {pc_wiz_model_name.value or "(unchanged)"}\n'
                                f'model: {pc_wiz_model.value or "(unchanged)"}\n'
                                f'api_base: {pc_wiz_api_base.value or "(provider default)"}\n'
                                f'Web Search:                  {"on" if pc_wiz_tool_web_search.value else "off"}\n'
                                f'exec:                        {"on" if pc_wiz_tool_exec.value else "off"}\n'
                                f'restrict_to_workspace:        {pc_wiz_sec_restrict.value}\n'
                                f'allow_read_outside_workspace: {pc_wiz_sec_allow_read.value}'
                            )
                        pc_wiz_summary_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')

                        def _pc_wiz_refresh_summary():
                            pc_wiz_summary_lbl.set_text(_pc_wiz_summary())
                        _pc_wiz.on('transition', lambda _: _pc_wiz_refresh_summary())

                        def _pc_wiz_apply():
                            data = load_picoclaw_config()
                            sec  = load_picoclaw_security()
                            # Normalize :N-suffixed stale keys (e.g. "MiniMax-M2.5:0" → "MiniMax-M2.5")
                            _sec_ml_raw = sec.get('model_list', {})
                            _sec_ml_norm: dict = {}
                            for _k, _v in _sec_ml_raw.items():
                                _ck = _k.rsplit(':', 1)[0] if _k.rsplit(':', 1)[-1].isdigit() else _k
                                if _ck not in _sec_ml_norm or (
                                        _v.get('api_keys') and not _sec_ml_norm[_ck].get('api_keys')):
                                    _sec_ml_norm[_ck] = _v
                            sec['model_list'] = _sec_ml_norm
                            ad = data.setdefault('agents', {}).setdefault('defaults', {})
                            if pc_wiz_prov.value:       ad['provider']    = pc_wiz_prov.value
                            if pc_wiz_model_name.value: ad['model_name']  = pc_wiz_model_name.value
                            # security
                            ad['restrict_to_workspace']        = pc_wiz_sec_restrict.value
                            ad['allow_read_outside_workspace'] = pc_wiz_sec_allow_read.value
                            # tools
                            data.setdefault('tools', {}).setdefault('web', {})['enabled']  = pc_wiz_tool_web_search.value
                            data.setdefault('tools', {}).setdefault('exec', {})['enabled'] = pc_wiz_tool_exec.value
                            # Inject model into model_list if not already present
                            mname = pc_wiz_model_name.value
                            if mname:
                                ml = data.setdefault('model_list', [])
                                existing = [e for e in ml if e.get('model_name') == mname]
                                if not existing:
                                    entry = {'model_name': mname, 'model': pc_wiz_model.value,
                                             'api_base': pc_wiz_api_base.value,
                                             'auth_method': pc_wiz_auth_method.value}
                                    ml.append(entry)
                                else:
                                    existing[0]['api_base'] = pc_wiz_api_base.value
                                    existing[0]['auth_method'] = pc_wiz_auth_method.value
                                if pc_wiz_api_key.value:
                                    sec.setdefault('model_list', {}).setdefault(mname, {})['api_keys'] = [pc_wiz_api_key.value]
                            try:
                                save_picoclaw_config(data)
                                save_picoclaw_security(sec)
                                ok_cfg, err_cfg = deploy_picoclaw_config()
                                ok_sec, err_sec = deploy_picoclaw_security()
                                deploy_errs = [e for e in [err_cfg, err_sec] if e]
                                if deploy_errs:
                                    ui.notify(f'⚠️ Saved locally but deploy failed: {"; ".join(deploy_errs)}', type='warning')
                                else:
                                    ui.notify('✅ PicoClaw config saved & deployed — restart PicoClaw to activate', type='positive')
                            except Exception as ex:
                                ui.notify(f'❌ Save failed: {ex}', type='negative')

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_pc_wiz.previous).props('flat color=grey-7')
                            ui.button('✅ Apply & Save', on_click=_pc_wiz_apply).props('color=green-8')

            # ── PicoClaw › Configuration ───────────────────────────────────
            with ui.tab_panel(t_pc_cfg):
                pc_conf = load_picoclaw_config()
                pc_sec  = load_picoclaw_security()

                # shortcuts
                pc_session   = pc_conf.get('session',   {})
                pc_agents    = pc_conf.get('agents',    {}).get('defaults', {})
                pc_channels  = pc_conf.get('channel_list', {}) or pc_conf.get('channels', {})
                pc_model_list= pc_conf.get('model_list', [])
                pc_gateway   = pc_conf.get('gateway',   {})
                pc_tools     = pc_conf.get('tools',     {})
                pc_web_tools = pc_tools.get('web',      {})
                pc_heartbeat = pc_conf.get('heartbeat', {})
                pc_devices   = pc_conf.get('devices',   {})
                pc_voice     = pc_conf.get('voice',     {})
                pc_build     = pc_conf.get('build_info',{})

                # security.yml shortcuts
                pc_sec_models   = pc_sec.get('model_list', {})
                pc_sec_channels = pc_sec.get('channel_list', {}) or pc_sec.get('channels', {})
                pc_sec_web      = pc_sec.get('web',        {})
                pc_sec_skills   = pc_sec.get('skills',     {})

                pc_model_panels = {}  # idx → widget dict
                pc_model_cards  = {}  # idx → card element (for show/hide)
                _pc_sel_ref  = [None]  # forward ref → _pc_model_sel (set in Models tab)
                _pc_show_ref = [None]  # forward ref → _pc_show_model (set in Models tab)

                def _make_pc_sel_opt(i, mname):
                    return f'[{i}] {mname or "(new)"}'

                def _pc_idx_from_opt(opt_str):
                    try:
                        return int(str(opt_str).split(']')[0].lstrip('[').strip())
                    except (ValueError, AttributeError):
                        return None

                def pc_build_model_card(container, idx, entry):
                    _mname    = str(entry.get('model_name', ''))
                    _sec_keys = pc_sec_models.get(_mname, {}).get('api_keys', []) or []
                    _keys_txt = '\n'.join(str(k) for k in _sec_keys)
                    with container:
                        with ui.card().classes('w-full q-mb-sm') as card:
                            with ui.row().classes('w-full items-center justify-between'):
                                ui.label(f'model [{idx}]').classes('text-caption text-purple-7 text-bold')
                                def _rm(i=idx, c=card):
                                    pc_model_panels.pop(i, None)
                                    pc_model_cards.pop(i, None)
                                    c.delete()
                                    sel = _pc_sel_ref[0]
                                    if sel:
                                        new_opts = [o for o in sel.options if _pc_idx_from_opt(o) != i]
                                        new_val  = new_opts[0] if new_opts else None
                                        sel.set_options(new_opts, value=new_val)
                                        if new_val is not None and _pc_show_ref[0]:
                                            t = _pc_idx_from_opt(new_val)
                                            if t is not None: _pc_show_ref[0](t)
                                ui.button(icon='delete', on_click=_rm).props('flat round dense color=negative')
                            ui.label('⚡ Quick pick').classes('text-caption text-purple-7')
                            _card_quick = ui.select(
                                options=list(_pc_ph_map.keys()),
                                label='Known model (auto-fill)',
                                value=_mname if _mname in _pc_ph_map else None,
                                clearable=True, with_input=True,
                            ).classes('w-full q-mb-xs')
                            ui.separator().classes('q-my-xs')
                            widgets = {}
                            _card_mname_opts = list(_pc_ph_map.keys())
                            if _mname and _mname not in _card_mname_opts:
                                _card_mname_opts = [_mname] + _card_mname_opts
                            widgets['model_name'] = ui.select(
                                options=_card_mname_opts,
                                label='model_name',
                                value=_mname or None,
                                with_input=True, new_value_mode='add-unique',
                            ).classes('w-full')
                            widgets['model']     = ui.input('model',    value=str(entry.get('model',''))).classes('w-full')
                            widgets['api_base']  = ui.input('api_base', value=str(entry.get('api_base',''))).classes('w-full')
                            ui.label('api_keys  (one per line → security.yml)').classes('text-caption text-grey-6 q-mt-xs')
                            widgets['api_keys']  = ui.textarea(value=_keys_txt).classes('w-full').props('outlined rows=3 label=api_keys')
                            cur_auth  = str(entry.get('auth_method', 'apikey'))
                            auth_opts = ['apikey', 'oauth']
                            widgets['auth_method'] = ui.select(auth_opts, label='auth_method',
                                value=cur_auth if cur_auth in auth_opts else 'apikey').classes('w-full')
                            pc_model_panels[idx] = widgets
                            pc_model_cards[idx]  = card
                            card.set_visibility(False)

                            # Quick-pick fills all fields
                            def _card_fill(e, _w=widgets, _qk=_card_quick):
                                h = _pc_ph_map.get(e.value) if e.value else None
                                if not h: return
                                _w['model_name'].set_value(h.get('model_name', ''))
                                _w['model'].set_value(h.get('model', ''))
                                _w['api_base'].set_value(h.get('api_base', ''))
                                am = h.get('auth_method', 'apikey')
                                _w['auth_method'].set_value(am if am in auth_opts else 'apikey')
                            _card_quick.on_value_change(_card_fill)

                            # model_name select → fill model field + update selector label
                            def _card_mname(e, _w=widgets, _idx=idx):
                                h = _pc_ph_map.get(e.value) if e.value else None
                                if h and h.get('model'): _w['model'].set_value(h['model'])
                                sel = _pc_sel_ref[0]
                                if sel:
                                    new_opts = [
                                        _make_pc_sel_opt(_idx, e.value) if _pc_idx_from_opt(o) == _idx else o
                                        for o in sel.options
                                    ]
                                    sel.set_options(new_opts, value=sel.value)
                            widgets['model_name'].on_value_change(_card_mname)

                def pc_collect_and_save():
                    data = load_picoclaw_config()
                    sec  = load_picoclaw_security()

                    # ── Remove V3-invalid legacy keys that may be in the file ──
                    data.get('agents', {}).get('defaults', {}).pop('model', None)
                    data.get('session', {}).pop('dm_scope', None)

                    # session  (dm_scope is removed in V3; do not re-write it)
                    # (no session fields currently saved from the General tab)

                    # agents.defaults  (note: 'model' is V0-legacy — not written)
                    ad = data.setdefault('agents', {}).setdefault('defaults', {})
                    ad['workspace']                  = pc_w_workspace.value
                    ad['restrict_to_workspace']      = pc_w_restrict.value
                    ad['allow_read_outside_workspace']= pc_w_allow_read_outside.value
                    ad['provider']                   = pc_w_provider.value
                    ad['model_name']                 = pc_w_model_name.value
                    ad['max_tokens']                 = to_int(pc_w_max_tokens.value, 8192)
                    ad['max_tool_iterations']        = to_int(pc_w_max_iter.value, 50)
                    ad['summarize_message_threshold']= to_int(pc_w_sum_threshold.value, 20)
                    ad['summarize_token_percent']    = to_int(pc_w_sum_percent.value, 75)

                    # model_list → config.json (no api_keys); api_keys → security.yml
                    # Rebuild sec['model_list'] from scratch so stale names (e.g. MiniMax-M2.5:0)
                    # that are no longer in the UI are pruned. Existing keys in the file are
                    # preserved when the widget textarea is empty (user didn't change them).
                    # Normalize :N-suffixed stale keys before lookup
                    _old_sec_ml: dict = {}
                    for _k, _v in sec.get('model_list', {}).items():
                        _ck = _k.rsplit(':', 1)[0] if _k.rsplit(':', 1)[-1].isdigit() else _k
                        if _ck not in _old_sec_ml or (
                                _v.get('api_keys') and not _old_sec_ml[_ck].get('api_keys')):
                            _old_sec_ml[_ck] = _v
                    _new_sec_ml: dict = {}
                    data['model_list'] = []
                    _seen_mnames: set = set()
                    for w in pc_model_panels.values():
                        mname = w['model_name'].value
                        keys = [k.strip() for k in w['api_keys'].value.splitlines() if k.strip()]
                        if mname not in _seen_mnames:
                            _seen_mnames.add(mname)
                            data['model_list'].append({
                                'model_name':  mname,
                                'model':       w['model'].value,
                                'api_base':    w['api_base'].value,
                                'auth_method': w['auth_method'].value,
                            })
                        # Widget has real keys → use them; widget empty → preserve file value
                        if keys:
                            _new_sec_ml.setdefault(mname, {})['api_keys'] = keys
                        else:
                            old_keys = _old_sec_ml.get(mname, {}).get('api_keys') or []
                            _new_sec_ml.setdefault(mname, {})['api_keys'] = old_keys
                    # Replace entire section — removes any stale/duplicate names
                    sec['model_list'] = _new_sec_ml

                    # gateway
                    data.setdefault('gateway', {})['host'] = pc_w_gw_host.value
                    data['gateway']['port']                 = to_int(pc_w_gw_port.value, 18790)

                    # channels – enable flags + non-secret fields → config.json; secrets → security.yml
                    data.pop('channels', None)   # remove legacy key
                    sec.pop('channels', None)    # remove legacy key
                    ch     = data.setdefault('channel_list', {})
                    sec_ch = sec.setdefault('channel_list', {})
                    def _ch_cfg(name, **kw):
                        ch.setdefault(name, {}).update(kw)
                    def _ch_sec(name, **kw):
                        if kw: sec_ch.setdefault(name, {}).update(kw)

                    _ch_cfg('pico',     enabled=pc_w_ch_pico_en.value,
                            ping_interval=to_int(pc_w_ch_pico_ping.value, 30),
                            max_connections=to_int(pc_w_ch_pico_maxconn.value, 100))
                    _ch_sec('pico',     token=pc_w_ch_pico_token.value)

                    _ch_cfg('qq',       enabled=pc_w_ch_qq_en.value, app_id=pc_w_ch_qq_appid.value)
                    _ch_sec('qq',       app_secret=pc_w_ch_qq_secret.value)

                    _ch_cfg('telegram', enabled=pc_w_ch_tg_en.value,
                            base_url=pc_w_ch_tg_base.value, proxy=pc_w_ch_tg_proxy.value)
                    _ch_sec('telegram', token=pc_w_ch_tg_token.value)

                    _ch_cfg('discord',  enabled=pc_w_ch_dc_en.value, mention_only=pc_w_ch_dc_mention.value)
                    _ch_sec('discord',  token=pc_w_ch_dc_token.value)

                    _ch_cfg('whatsapp', enabled=pc_w_ch_wa_en.value,
                            bridge_url=pc_w_ch_wa_url.value, use_native=pc_w_ch_wa_native.value)

                    _ch_cfg('feishu',   enabled=pc_w_ch_fs_en.value, app_id=pc_w_ch_fs_appid.value)
                    _ch_sec('feishu',   app_secret=pc_w_ch_fs_secret.value,
                            encrypt_key=pc_w_ch_fs_encrypt.value,
                            verification_token=pc_w_ch_fs_verify.value)

                    _ch_cfg('slack',    enabled=pc_w_ch_sl_en.value)
                    _ch_sec('slack',    bot_token=pc_w_ch_sl_bot.value, app_token=pc_w_ch_sl_app.value)

                    _ch_cfg('matrix',   enabled=pc_w_ch_mx_en.value,
                            homeserver=pc_w_ch_mx_home.value, user_id=pc_w_ch_mx_user.value)
                    _ch_sec('matrix',   access_token=pc_w_ch_mx_token.value)

                    _ch_cfg('dingtalk',  enabled=pc_w_ch_dt_en.value, client_id=pc_w_ch_dt_id.value)
                    _ch_sec('dingtalk',  client_secret=pc_w_ch_dt_secret.value)

                    _ch_cfg('maixcam',  enabled=pc_w_ch_mc_en.value,
                            host=pc_w_ch_mc_host.value, port=to_int(pc_w_ch_mc_port.value, 18790))

                    _ch_cfg('irc',      enabled=pc_w_ch_irc_en.value,
                            server=pc_w_ch_irc_server.value, nick=pc_w_ch_irc_nick.value, tls=pc_w_ch_irc_tls.value)

                    _ch_cfg('onebot',   enabled=pc_w_ch_ob_en.value, ws_url=pc_w_ch_ob_ws.value)
                    _ch_sec('onebot',   access_token=pc_w_ch_ob_token.value)

                    _ch_cfg('line',     enabled=pc_w_ch_line_en.value)
                    _ch_sec('line',     channel_secret=pc_w_ch_line_secret.value,
                            channel_access_token=pc_w_ch_line_cat.value)

                    _ch_cfg('wecom',    enabled=pc_w_ch_wc_en.value)
                    _ch_sec('wecom',    token=pc_w_ch_wc_token.value,
                            encoding_aes_key=pc_w_ch_wc_aes.value)

                    # tools
                    t = data.setdefault('tools', {})
                    t['allow_read_paths']  = lines_to_list(pc_w_t_read_paths.value) or None
                    t['allow_write_paths'] = lines_to_list(pc_w_t_write_paths.value) or None

                    web = t.setdefault('web', {})
                    web['enabled']           = pc_w_t_web_en.value
                    web['fetch_limit_bytes'] = to_int(pc_w_t_fetch_limit.value, 10485760)
                    web.setdefault('duckduckgo', {})['enabled']   = pc_w_t_ddg.value
                    web.setdefault('brave',      {})['enabled']   = pc_w_t_brave.value
                    web.setdefault('tavily',     {})['enabled']   = pc_w_t_tavily.value
                    web.setdefault('perplexity', {})['enabled']   = pc_w_t_perp.value
                    web.setdefault('searxng',    {})['enabled']   = pc_w_t_searxng.value
                    web['searxng']['base_url']                     = pc_w_t_searxng_url.value

                    # web search api_keys → security.yml (plural array per spec)
                    sec_web = sec.setdefault('web', {})
                    def _web_sec(name, widget):
                        keys = [k.strip() for k in widget.value.splitlines() if k.strip()]
                        sec_web.setdefault(name, {})['api_keys'] = keys
                    _web_sec('brave',      pc_w_t_brave_key)
                    _web_sec('tavily',     pc_w_t_tavily_key)
                    _web_sec('perplexity', pc_w_t_perp_key)

                    # skills auth tokens → security.yml
                    sec_sk = sec.setdefault('skills', {})
                    sec_sk.setdefault('clawhub', {})['auth_token'] = pc_w_t_clawhub_auth.value
                    sec_sk.setdefault('github',  {})['token']       = pc_w_t_github_token.value

                    t.setdefault('exec',  {})['enabled']          = pc_w_t_exec.value
                    t['exec']['timeout_seconds']                   = to_int(pc_w_t_exec_timeout.value, 60)
                    t['exec']['allow_remote']                      = pc_w_t_exec_remote.value
                    t.setdefault('cron',  {})['enabled']          = pc_w_t_cron.value
                    t['cron']['allow_command']                     = pc_w_t_cron_cmd.value
                    t.setdefault('skills',{}).setdefault('registries',{}).setdefault('clawhub',{})['enabled'] = pc_w_t_skills.value
                    t.setdefault('mcp',   {})['enabled']          = pc_w_t_mcp.value
                    t.setdefault('spawn', {})['enabled']          = pc_w_t_spawn.value
                    t.setdefault('subagent',{})['enabled']        = pc_w_t_subagent.value
                    t.setdefault('web_fetch',{})['enabled']       = pc_w_t_web_fetch.value
                    t.setdefault('send_file',{})['enabled']       = pc_w_t_send_file.value
                    t.setdefault('read_file',{})['enabled']       = pc_w_t_read_file.value
                    t['read_file']['max_read_file_size']           = to_int(pc_w_t_read_max.value, 65536)
                    t.setdefault('write_file',{})['enabled']      = pc_w_t_write_file.value
                    t.setdefault('edit_file', {})['enabled']      = pc_w_t_edit_file.value
                    t.setdefault('append_file',{})['enabled']     = pc_w_t_append_file.value
                    t.setdefault('list_dir',  {})['enabled']      = pc_w_t_list_dir.value
                    t.setdefault('message',   {})['enabled']      = pc_w_t_message.value
                    t.setdefault('i2c',       {})['enabled']      = pc_w_t_i2c.value
                    t.setdefault('spi',       {})['enabled']      = pc_w_t_spi.value
                    mc_cfg = t.setdefault('media_cleanup', {})
                    mc_cfg['enabled']          = pc_w_t_mc_en.value
                    mc_cfg['max_age_minutes']  = to_int(pc_w_t_mc_age.value, 30)
                    mc_cfg['interval_minutes'] = to_int(pc_w_t_mc_interval.value, 5)

                    # heartbeat
                    data.setdefault('heartbeat', {})['enabled']  = pc_w_hb_en.value
                    data['heartbeat']['interval']                  = to_int(pc_w_hb_interval.value, 30)

                    # devices
                    data.setdefault('devices', {})['enabled']     = pc_w_dev_en.value
                    data['devices']['monitor_usb']                 = pc_w_dev_usb.value

                    # voice
                    data.setdefault('voice', {})['echo_transcription'] = pc_w_voice_echo.value

                    try:
                        save_picoclaw_config(data)
                        save_picoclaw_security(sec)
                        ui.notify(T['notify_saved'], type='positive')
                        return True
                    except Exception as e:
                        ui.notify(T['notify_save_fail'].format(e), type='negative')
                        return False

                def pc_do_save_restart():
                    """Save locally, deploy to /var/lib/picoclaw, then restart picoclaw.service."""
                    if not pc_collect_and_save():
                        return
                    ok_cfg, err_cfg = deploy_picoclaw_config()
                    ok_sec, err_sec = deploy_picoclaw_security()
                    deploy_errs = [e for e in [err_cfg, err_sec] if e]
                    if deploy_errs:
                        ui.notify(f'⚠️ Deploy failed: {"; ".join(deploy_errs)}', type='warning')
                        return
                    ok_svc, svc_err = restart_picoclaw_service()
                    if ok_svc:
                        ui.notify('✅ PicoClaw deployed & restarted', type='positive')
                    else:
                        ui.notify(f'⚠️ Restart failed: {svc_err or T["notify_sudo_required"]}', type='warning')

                # ── Config version badge ───────────────────────────────────
                _pc_cfg_ver = pc_conf.get('version', 0)
                _pc_bld_ver = pc_build.get('version', '')
                with ui.row().classes('items-center gap-2 q-px-sm q-pt-xs q-pb-none'):
                    _badge_color = 'green-7' if _pc_cfg_ver >= 3 else 'orange-7'
                    ui.badge(f'config v{_pc_cfg_ver}', color=_badge_color).props('outline')
                    if _pc_bld_ver:
                        ui.label(f'build {_pc_bld_ver}').classes('text-caption text-grey-5')

                # ── Upgrade banner (shown when config is below current version) ──
                if _pc_cfg_ver < 3:
                    _UPGRADE_SCRIPT = os.path.join(SCRIPT_DIR, 'scripts', 'upgrade_picoclaw_config.py')
                    with ui.card().classes('w-full q-pa-sm q-mb-sm bg-orange-1'):
                        with ui.row().classes('items-center gap-2'):
                            ui.icon('warning', color='orange-8').classes('text-h6')
                            ui.label(
                                f'Config is version {_pc_cfg_ver} — PicoClaw requires version 3. '
                                'Click Upgrade to convert the local config file in-place '
                                '(a timestamped .bak copy is kept automatically).'
                            ).classes('text-caption text-orange-9')
                        _upg_log = ui.textarea('').classes('w-full text-caption font-mono').props(
                            'outlined readonly rows=6 label="Upgrade output"')
                        _upg_log.set_visibility(False)

                        async def _run_pc_cfg_upgrade(_script=_UPGRADE_SCRIPT,
                                                      _path=PICOCLAW_CONFIG_PATH):
                            import asyncio as _aio
                            _upg_log.set_visibility(True)
                            _upg_log.set_value('⏳ Running upgrade script…\n')
                            _upg_btn.props('disabled loading')
                            try:
                                proc = await _aio.create_subprocess_exec(
                                    'python3', _script, _path, '--verbose',
                                    stdout=_aio.subprocess.PIPE,
                                    stderr=_aio.subprocess.STDOUT,
                                )
                                buf = '⏳ Running upgrade script…\n'
                                async for line in proc.stdout:
                                    buf += line.decode(errors='replace')
                                    _upg_log.set_value(buf)
                                await proc.wait()
                                if proc.returncode == 0:
                                    _upg_log.set_value(
                                        buf + f'\n✅ Done.\n'
                                              f'   Upgraded : {_path}\n'
                                              f'   Backup   : {_path}.bak.<timestamp>'
                                    )
                                    ui.notify('✅ Config upgraded — reloading page…',
                                              type='positive')
                                    ui.navigate.reload()
                                else:
                                    _upg_log.set_value(buf + '\n⚠️ Script exited with error.')
                                    ui.notify('⚠️ Upgrade script returned an error',
                                              type='warning')
                            except Exception as _ex:
                                _upg_log.set_value(f'❌ {_ex}')
                                ui.notify(f'❌ {_ex}', type='negative')
                            finally:
                                _upg_btn.props(remove='disabled loading')

                        _upg_btn = ui.button(
                            'Upgrade config to v3', icon='upgrade',
                            on_click=_run_pc_cfg_upgrade,
                        ).props('color=orange-8 size=sm')

                with ui.tabs().classes('w-full bg-purple-1') as pc_cfg_tabs:
                    t_pc_gen   = ui.tab(T['pc_tab_general'],  icon='tune')
                    t_pc_models= ui.tab(T['pc_tab_models'],   icon='cloud')
                    t_pc_ch    = ui.tab(T['pc_tab_channels'], icon='forum')
                    t_pc_tools = ui.tab(T['pc_tab_tools'],    icon='construction')
                    t_pc_sys   = ui.tab(T['pc_tab_system'],   icon='computer')

                with ui.tab_panels(pc_cfg_tabs, value=t_pc_gen).classes('w-full'):

                    # ── General ──────────────────────────────────────────────
                    with ui.tab_panel(t_pc_gen):
                        ui.label(T['pc_section_agent_def']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        pc_w_workspace          = ui.input('workspace', value=str(pc_agents.get('workspace','/var/lib/picoclaw/.picoclaw/workspace'))).classes('w-full')
                        pc_w_restrict           = ui.checkbox('restrict_to_workspace',       value=bool(pc_agents.get('restrict_to_workspace', False)))
                        pc_w_allow_read_outside = ui.checkbox('allow_read_outside_workspace', value=bool(pc_agents.get('allow_read_outside_workspace', False)))

                        _pc_cur_prov  = str(pc_agents.get('provider', 'qwen'))
                        _pc_prov_opts = list(_pc_ph_provs)
                        if _pc_cur_prov and _pc_cur_prov not in _pc_prov_opts:
                            _pc_prov_opts = [_pc_cur_prov] + _pc_prov_opts
                        pc_w_provider = ui.select(
                            options=_pc_prov_opts,
                            label='provider',
                            value=_pc_cur_prov or (_pc_prov_opts[0] if _pc_prov_opts else None),
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full')

                        _pc_cur_mname = str(pc_agents.get('model_name', 'qwen3.5-plus'))
                        _pc_init_mnames = _pc_ph_pid_models.get(_pc_cur_prov, list(_pc_ph_map.keys()))
                        _pc_mname_opts  = list(_pc_init_mnames) if _pc_cur_mname in _pc_init_mnames \
                            else ([_pc_cur_mname] + list(_pc_init_mnames))
                        pc_w_model_name = ui.select(
                            options=_pc_mname_opts,
                            label='model_name',
                            value=_pc_cur_mname,
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full')

                        # Auto-fill: provider change → update model_name list
                        def _pc_gen_prov_change(e):
                            prov = e.value or ''
                            mnames = _pc_ph_pid_models.get(prov, list(_pc_ph_map.keys()))
                            cur = pc_w_model_name.value
                            new_val = cur if cur in mnames else (mnames[0] if mnames else cur)
                            pc_w_model_name.set_options(list(mnames), value=new_val)
                        pc_w_provider.on_value_change(_pc_gen_prov_change)
                        pc_w_max_tokens         = ui.number('max_tokens',                 value=pc_agents.get('max_tokens', 8192),  min=256,  step=512).classes('w-full')
                        pc_w_max_iter           = ui.number('max_tool_iterations',         value=pc_agents.get('max_tool_iterations', 50), min=1, step=5).classes('w-full')
                        pc_w_sum_threshold      = ui.number('summarize_message_threshold', value=pc_agents.get('summarize_message_threshold', 20), min=1, step=1).classes('w-full')
                        pc_w_sum_percent        = ui.number('summarize_token_percent',     value=pc_agents.get('summarize_token_percent', 75), min=1, max=100, step=5).classes('w-full')

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_gateway']).classes('text-subtitle2 text-grey-7')
                        pc_w_gw_host = ui.input('host', value=str(pc_gateway.get('host','0.0.0.0'))).classes('w-full')
                        pc_w_gw_port = ui.number('port', value=pc_gateway.get('port', 18790), min=1024, max=65535, step=1).classes('w-full')

                    # ── Models ───────────────────────────────────────────────
                    with ui.tab_panel(t_pc_models):
                        ui.label(T['pc_section_models']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['pc_hint_models']).classes('text-caption text-grey-5')
                        # ── model selector ───────────────────────────────────
                        with ui.row().classes('w-full items-center gap-2 q-mb-sm'):
                            _pc_sel_opts = [_make_pc_sel_opt(i, e.get('model_name', ''))
                                            for i, e in enumerate(pc_model_list)]
                            _pc_model_sel = ui.select(
                                options=_pc_sel_opts,
                                label='Select model to edit',
                                value=_pc_sel_opts[0] if _pc_sel_opts else None,
                            ).classes('flex-1')
                            _pc_sel_ref[0] = _pc_model_sel
                        pc_model_container = ui.column().classes('w-full')
                        for i, entry in enumerate(pc_model_list):
                            pc_build_model_card(pc_model_container, i, entry)
                        def _pc_show_model(target_idx):
                            for _i, _c in pc_model_cards.items():
                                _c.set_visibility(_i == target_idx)
                        _pc_show_ref[0] = _pc_show_model
                        def _on_pc_sel(e):
                            t = _pc_idx_from_opt(e.value)
                            if t is not None: _pc_show_model(t)
                        _pc_model_sel.on_value_change(_on_pc_sel)
                        if pc_model_list:
                            _pc_show_model(0)
                        ui.separator().classes('q-my-sm')
                        def _pc_add_model():
                            new_idx = max(pc_model_panels.keys(), default=-1) + 1
                            pc_build_model_card(pc_model_container, new_idx, {})
                            new_opt = _make_pc_sel_opt(new_idx, '')
                            _pc_model_sel.set_options(list(_pc_model_sel.options) + [new_opt], value=new_opt)
                            _pc_show_model(new_idx)
                        ui.button(T['pc_btn_add_model'], on_click=_pc_add_model).props('outline color=purple')

                    # ── Channels ─────────────────────────────────────────────
                    with ui.tab_panel(t_pc_ch):
                        ui.label(T['pc_section_channels']).classes('text-subtitle2 text-grey-7 q-mt-sm')

                        def _pico_ch(name): return pc_channels.get(name, {})

                        with ui.expansion('🔵 Pico (native)', icon='wifi').classes('w-full'):
                            pc_w_ch_pico_en      = ui.checkbox('enabled',         value=bool(_pico_ch('pico').get('enabled', True)))
                            pc_w_ch_pico_token   = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('pico',{}).get('token',''))).classes('w-full')
                            pc_w_ch_pico_ping    = ui.number('ping_interval',     value=_pico_ch('pico').get('ping_interval', 30),   min=5,  step=5).classes('w-full')
                            pc_w_ch_pico_maxconn = ui.number('max_connections',   value=_pico_ch('pico').get('max_connections', 100), min=1, step=10).classes('w-full')

                        with ui.expansion('📱 QQ', icon='chat').classes('w-full'):
                            pc_w_ch_qq_en     = ui.checkbox('enabled',    value=bool(_pico_ch('qq').get('enabled', False)))
                            pc_w_ch_qq_appid  = ui.input('app_id',        value=str(_pico_ch('qq').get('app_id',''))).classes('w-full')
                            pc_w_ch_qq_secret = ui.input('app_secret (→ security.yml)', value=str(pc_sec_channels.get('qq',{}).get('app_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('✈️ Telegram', icon='send').classes('w-full'):
                            pc_w_ch_tg_en    = ui.checkbox('enabled', value=bool(_pico_ch('telegram').get('enabled', False)))
                            pc_w_ch_tg_token = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('telegram',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_tg_base  = ui.input('base_url',   value=str(_pico_ch('telegram').get('base_url',''))).classes('w-full')
                            pc_w_ch_tg_proxy = ui.input('proxy',      value=str(_pico_ch('telegram').get('proxy',''))).classes('w-full')

                        with ui.expansion('🎮 Discord', icon='discord').classes('w-full'):
                            pc_w_ch_dc_en      = ui.checkbox('enabled',      value=bool(_pico_ch('discord').get('enabled', False)))
                            pc_w_ch_dc_token   = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('discord',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_dc_mention = ui.checkbox('mention_only', value=bool(_pico_ch('discord').get('mention_only', False)))

                        with ui.expansion('💬 WhatsApp', icon='smartphone').classes('w-full'):
                            pc_w_ch_wa_en     = ui.checkbox('enabled',    value=bool(_pico_ch('whatsapp').get('enabled', False)))
                            pc_w_ch_wa_url    = ui.input('bridge_url',    value=str(_pico_ch('whatsapp').get('bridge_url','ws://localhost:3001'))).classes('w-full')
                            pc_w_ch_wa_native = ui.checkbox('use_native', value=bool(_pico_ch('whatsapp').get('use_native', False)))

                        with ui.expansion('🪶 Feishu / Lark', icon='language').classes('w-full'):
                            pc_w_ch_fs_en      = ui.checkbox('enabled',              value=bool(_pico_ch('feishu').get('enabled', False)))
                            pc_w_ch_fs_appid   = ui.input('app_id',                  value=str(_pico_ch('feishu').get('app_id',''))).classes('w-full')
                            pc_w_ch_fs_secret  = ui.input('app_secret (→ security.yml)', value=str(pc_sec_channels.get('feishu',{}).get('app_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_fs_encrypt = ui.input('encrypt_key (→ security.yml)', value=str(pc_sec_channels.get('feishu',{}).get('encrypt_key','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_fs_verify  = ui.input('verification_token (→ security.yml)', value=str(pc_sec_channels.get('feishu',{}).get('verification_token',''))).classes('w-full')

                        with ui.expansion('💼 Slack', icon='workspaces').classes('w-full'):
                            pc_w_ch_sl_en  = ui.checkbox('enabled',    value=bool(_pico_ch('slack').get('enabled', False)))
                            pc_w_ch_sl_bot = ui.input('bot_token (→ security.yml)', value=str(pc_sec_channels.get('slack',{}).get('bot_token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_sl_app = ui.input('app_token (→ security.yml)', value=str(pc_sec_channels.get('slack',{}).get('app_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🔷 Matrix', icon='grid_on').classes('w-full'):
                            pc_w_ch_mx_en    = ui.checkbox('enabled',      value=bool(_pico_ch('matrix').get('enabled', False)))
                            pc_w_ch_mx_home  = ui.input('homeserver',      value=str(_pico_ch('matrix').get('homeserver','https://matrix.org'))).classes('w-full')
                            pc_w_ch_mx_user  = ui.input('user_id',         value=str(_pico_ch('matrix').get('user_id',''))).classes('w-full')
                            pc_w_ch_mx_token = ui.input('access_token (→ security.yml)', value=str(pc_sec_channels.get('matrix',{}).get('access_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🔔 DingTalk', icon='notifications').classes('w-full'):
                            pc_w_ch_dt_en     = ui.checkbox('enabled',       value=bool(_pico_ch('dingtalk').get('enabled', False)))
                            pc_w_ch_dt_id     = ui.input('client_id',        value=str(_pico_ch('dingtalk').get('client_id',''))).classes('w-full')
                            pc_w_ch_dt_secret = ui.input('client_secret (→ security.yml)', value=str(pc_sec_channels.get('dingtalk',{}).get('client_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('📷 MaixCam', icon='videocam').classes('w-full'):
                            pc_w_ch_mc_en   = ui.checkbox('enabled', value=bool(_pico_ch('maixcam').get('enabled', False)))
                            pc_w_ch_mc_host = ui.input('host',        value=str(_pico_ch('maixcam').get('host','0.0.0.0'))).classes('w-full')
                            pc_w_ch_mc_port = ui.number('port',       value=_pico_ch('maixcam').get('port', 18790), min=1024, max=65535, step=1).classes('w-full')

                        with ui.expansion('💬 IRC', icon='terminal').classes('w-full'):
                            pc_w_ch_irc_en     = ui.checkbox('enabled', value=bool(_pico_ch('irc').get('enabled', False)))
                            pc_w_ch_irc_server = ui.input('server',     value=str(_pico_ch('irc').get('server',''))).classes('w-full')
                            pc_w_ch_irc_nick   = ui.input('nick',       value=str(_pico_ch('irc').get('nick',''))).classes('w-full')
                            pc_w_ch_irc_tls    = ui.checkbox('tls',     value=bool(_pico_ch('irc').get('tls', False)))

                        with ui.expansion('🤖 OneBot', icon='smart_toy').classes('w-full'):
                            pc_w_ch_ob_en    = ui.checkbox('enabled',      value=bool(_pico_ch('onebot').get('enabled', False)))
                            pc_w_ch_ob_ws    = ui.input('ws_url',          value=str(_pico_ch('onebot').get('ws_url','ws://127.0.0.1:3001'))).classes('w-full')
                            pc_w_ch_ob_token = ui.input('access_token (→ security.yml)', value=str(pc_sec_channels.get('onebot',{}).get('access_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🟢 LINE', icon='message').classes('w-full'):
                            pc_w_ch_line_en     = ui.checkbox('enabled',               value=bool(_pico_ch('line').get('enabled', False)))
                            pc_w_ch_line_secret = ui.input('channel_secret (→ security.yml)', value=str(pc_sec_channels.get('line',{}).get('channel_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_line_cat    = ui.input('channel_access_token (→ security.yml)', value=str(pc_sec_channels.get('line',{}).get('channel_access_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🏢 WeCom', icon='business').classes('w-full'):
                            pc_w_ch_wc_en    = ui.checkbox('enabled',          value=bool(_pico_ch('wecom').get('enabled', False)))
                            pc_w_ch_wc_token = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('wecom',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_wc_aes   = ui.input('encoding_aes_key (→ security.yml)', value=str(pc_sec_channels.get('wecom',{}).get('encoding_aes_key','')),
                                password=True, password_toggle_button=True).classes('w-full')

                    # ── Tools ────────────────────────────────────────────────
                    with ui.tab_panel(t_pc_tools):
                        ui.label(T['pc_section_tools']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['pc_lbl_read_paths']).classes('text-caption text-grey-6')
                        _rp = pc_tools.get('allow_read_paths') or []
                        pc_w_t_read_paths  = ui.textarea(value='\n'.join(_rp)).classes('w-full').props('outlined rows=3')
                        ui.label(T['pc_lbl_write_paths']).classes('text-caption text-grey-6')
                        _wp = pc_tools.get('allow_write_paths') or []
                        pc_w_t_write_paths = ui.textarea(value='\n'.join(_wp)).classes('w-full').props('outlined rows=3')

                        ui.separator().classes('q-my-sm')
                        with ui.expansion(T['pc_exp_web_search'], icon='search').classes('w-full'):
                            pc_w_t_web_en      = ui.checkbox('web.enabled',        value=bool(pc_web_tools.get('enabled', True)))
                            pc_w_t_fetch_limit = ui.number('fetch_limit_bytes',    value=pc_web_tools.get('fetch_limit_bytes', 10485760), min=1024, step=1048576).classes('w-full')
                            pc_w_t_ddg         = ui.checkbox('duckduckgo.enabled', value=bool(pc_web_tools.get('duckduckgo',{}).get('enabled', True)))
                            pc_w_t_brave       = ui.checkbox('brave.enabled',      value=bool(pc_web_tools.get('brave',{}).get('enabled', False)))
                            pc_w_t_brave_key   = ui.input('brave api_keys[0] (-> security.yml)',
                                value=str((pc_sec_web.get('brave',{}).get('api_keys',[]) or [''])[0]),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_t_tavily      = ui.checkbox('tavily.enabled',     value=bool(pc_web_tools.get('tavily',{}).get('enabled', False)))
                            pc_w_t_tavily_key  = ui.input('tavily api_keys[0] (-> security.yml)',
                                value=str((pc_sec_web.get('tavily',{}).get('api_keys',[]) or [''])[0]),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_t_perp        = ui.checkbox('perplexity.enabled', value=bool(pc_web_tools.get('perplexity',{}).get('enabled', False)))
                            pc_w_t_perp_key    = ui.input('perplexity api_keys[0] (-> security.yml)',
                                value=str((pc_sec_web.get('perplexity',{}).get('api_keys',[]) or [''])[0]),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_t_searxng     = ui.checkbox('searxng.enabled',    value=bool(pc_web_tools.get('searxng',{}).get('enabled', False)))
                            pc_w_t_searxng_url = ui.input('searxng.base_url',      value=str(pc_web_tools.get('searxng',{}).get('base_url',''))).classes('w-full')

                        with ui.expansion(T['pc_exp_exec'], icon='terminal').classes('w-full'):
                            pc_t_exec = pc_tools.get('exec', {})
                            pc_w_t_exec         = ui.checkbox('exec.enabled',       value=bool(pc_t_exec.get('enabled', True)))
                            pc_w_t_exec_remote  = ui.checkbox('allow_remote',       value=bool(pc_t_exec.get('allow_remote', True)))
                            pc_w_t_exec_timeout = ui.number('timeout_seconds',      value=pc_t_exec.get('timeout_seconds', 60), min=5, step=5).classes('w-full')

                        with ui.expansion(T['pc_exp_cron'], icon='schedule').classes('w-full'):
                            pc_t_cron = pc_tools.get('cron', {})
                            pc_w_t_cron     = ui.checkbox('cron.enabled',     value=bool(pc_t_cron.get('enabled', True)))
                            pc_w_t_cron_cmd = ui.checkbox('allow_command',    value=bool(pc_t_cron.get('allow_command', True)))

                        with ui.expansion(T['pc_exp_skills_mcp'], icon='hub').classes('w-full'):
                            pc_w_t_skills  = ui.checkbox('skills.clawhub.enabled',  value=bool(pc_tools.get('skills',{}).get('registries',{}).get('clawhub',{}).get('enabled', True)))
                            pc_w_t_mcp     = ui.checkbox('mcp.enabled',             value=bool(pc_tools.get('mcp',{}).get('enabled', False)))

                        with ui.expansion('🔑 Skills Auth Tokens (-> security.yml)', icon='key').classes('w-full'):
                            ui.label('clawhub.auth_token').classes('text-caption text-grey-6')
                            pc_w_t_clawhub_auth = ui.input('clawhub auth_token',
                                value=str(pc_sec_skills.get('clawhub',{}).get('auth_token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            ui.label('github.token').classes('text-caption text-grey-6')
                            pc_w_t_github_token = ui.input('github token',
                                value=str(pc_sec_skills.get('github',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion(T['pc_exp_file_tools'], icon='folder').classes('w-full'):
                            pc_w_t_read_file   = ui.checkbox('read_file.enabled',   value=bool(pc_tools.get('read_file',{}).get('enabled', True)))
                            pc_w_t_read_max    = ui.number('read_file.max_read_file_size', value=pc_tools.get('read_file',{}).get('max_read_file_size', 65536), min=1024, step=4096).classes('w-full')
                            pc_w_t_write_file  = ui.checkbox('write_file.enabled',  value=bool(pc_tools.get('write_file',{}).get('enabled', True)))
                            pc_w_t_edit_file   = ui.checkbox('edit_file.enabled',   value=bool(pc_tools.get('edit_file',{}).get('enabled', True)))
                            pc_w_t_append_file = ui.checkbox('append_file.enabled', value=bool(pc_tools.get('append_file',{}).get('enabled', True)))
                            pc_w_t_list_dir    = ui.checkbox('list_dir.enabled',    value=bool(pc_tools.get('list_dir',{}).get('enabled', True)))
                            pc_w_t_send_file   = ui.checkbox('send_file.enabled',   value=bool(pc_tools.get('send_file',{}).get('enabled', True)))
                            pc_w_t_message     = ui.checkbox('message.enabled',     value=bool(pc_tools.get('message',{}).get('enabled', True)))
                            pc_w_t_web_fetch   = ui.checkbox('web_fetch.enabled',   value=bool(pc_tools.get('web_fetch',{}).get('enabled', True)))
                            pc_w_t_spawn       = ui.checkbox('spawn.enabled',       value=bool(pc_tools.get('spawn',{}).get('enabled', True)))
                            pc_w_t_subagent    = ui.checkbox('subagent.enabled',    value=bool(pc_tools.get('subagent',{}).get('enabled', True)))
                            pc_w_t_i2c         = ui.checkbox('i2c.enabled',         value=bool(pc_tools.get('i2c',{}).get('enabled', False)))
                            pc_w_t_spi         = ui.checkbox('spi.enabled',         value=bool(pc_tools.get('spi',{}).get('enabled', False)))

                        with ui.expansion(T['pc_exp_media_cleanup'], icon='cleaning_services').classes('w-full'):
                            pc_t_mc = pc_tools.get('media_cleanup', {})
                            pc_w_t_mc_en       = ui.checkbox('enabled',          value=bool(pc_t_mc.get('enabled', True)))
                            pc_w_t_mc_age      = ui.number('max_age_minutes',    value=pc_t_mc.get('max_age_minutes', 30),   min=1, step=5).classes('w-full')
                            pc_w_t_mc_interval = ui.number('interval_minutes',   value=pc_t_mc.get('interval_minutes', 5),   min=1, step=1).classes('w-full')

                    # ── System ───────────────────────────────────────────────
                    with ui.tab_panel(t_pc_sys):
                        ui.label(T['pc_section_heartbeat']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        pc_w_hb_en       = ui.checkbox('heartbeat.enabled',  value=bool(pc_heartbeat.get('enabled', True)))
                        pc_w_hb_interval = ui.number('interval (secs)',       value=pc_heartbeat.get('interval', 30), min=5, step=5).classes('w-full')

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_devices']).classes('text-subtitle2 text-grey-7')
                        pc_w_dev_en  = ui.checkbox('devices.enabled',     value=bool(pc_devices.get('enabled', False)))
                        pc_w_dev_usb = ui.checkbox('monitor_usb',         value=bool(pc_devices.get('monitor_usb', True)))

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_voice']).classes('text-subtitle2 text-grey-7')
                        pc_w_voice_echo = ui.checkbox('echo_transcription', value=bool(pc_voice.get('echo_transcription', False)))

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_build']).classes('text-subtitle2 text-grey-7')
                        with ui.card().classes('w-full bg-grey-2 q-pa-sm'):
                            for k, v in pc_build.items():
                                ui.label(f'{k}: {v}').classes('text-caption text-mono')

                ui.separator()
                with ui.row().classes('w-full gap-2 q-pa-sm'):
                    ui.button(T['pc_btn_save'],         on_click=pc_collect_and_save).props('elevated').classes('flex-1 bg-purple-8 text-white')
                    ui.button(T['pc_btn_save_restart'],  on_click=pc_do_save_restart).props('elevated').classes('flex-1 bg-deep-purple-8 text-white')

            # ── PicoClaw › Pair Device ─────────────────────────────────────
            with ui.tab_panel(t_pc_pair):
                with ui.card().classes('w-full q-pa-md'):
                    ui.label(T['pc_pair_title']).classes('text-h6 text-purple-8')
                    ui.label(T['pc_pair_hint']).classes('text-caption text-grey-6 q-mt-xs')

                    def _run_pico_setup_for_pair():
                        ok, msg = setup_pico_channel_token()
                        if ok:
                            ui.notify(T['pc_pair_setup_ok'], type='positive')
                            ui.timer(0.8, lambda: ui.navigate.reload(), once=True)
                        else:
                            ui.notify(f"{T['pc_pair_setup_fail']}: {msg}", type='warning')

                    # Pico auth token comes directly from .security.yml.
                    _sec_tok  = ''
                    _sec_err  = ''
                    _sec_tok, _sec_err = _read_security_yml_token('pico')
                    pico_token = _sec_tok
                    pico_port = int(pc_gateway.get('port', 18790) or 18790)
                    pico_host = _get_lan_ip() or request.url.hostname or 'localhost'
                    pico_scheme = request.url.scheme or 'http'
                    pico_url = f'{pico_scheme}://{pico_host}:{pico_port}?token={pico_token}' if pico_token else ''
                    pico_qr_url = f'https://quickchart.io/qr?size=260&margin=1&text={quote(pico_url, safe="")}' if pico_url else ''

                    if _sec_err:
                        ui.label(f'⚠️ {_sec_err}').classes('text-warning text-caption q-mt-xs')
                        ui.label('Run pico setup to generate token, then reload this page.').classes('text-warning text-caption')
                    if pico_token:
                        ui.input('Pico token  (.security.yml: channel_list.pico.settings.token)', value=pico_token).props('readonly').classes('w-full q-mt-sm')
                        ui.input(T['pc_pair_url'], value=pico_url).props('readonly').classes('w-full')

                        with ui.row().classes('w-full items-start gap-4 q-mt-sm'):
                            with ui.card().classes('q-pa-sm items-center bg-white'):
                                ui.label(T['pc_pair_qr']).classes('text-caption text-grey-7 q-mb-xs')
                                ui.image(pico_qr_url).classes('w-56 h-56')

                            with ui.column().classes('gap-2 q-mt-md'):
                                def _copy_pico_url(url=pico_url):
                                    ui.clipboard.write(url)
                                    ui.notify(T['pc_pair_copy_ok'], type='positive')

                                def _show_pico_qr(url=pico_url, token=pico_token):
                                    try:
                                        import importlib
                                        import clawberry_paircode as _cp
                                        importlib.reload(_cp)
                                        _cp.request_picoclaw_qr_display(url, token)
                                        ui.notify(T['pc_pair_queue_ok'], type='positive')
                                    except Exception as exc:
                                        ui.notify(f'❌ {exc}', type='negative')

                                ui.button(T['pc_pair_copy_url'], on_click=_copy_pico_url).props('outline color=purple-8')
                                ui.button(T['pc_pair_show_display'], on_click=_show_pico_qr).props('elevated color=purple-8')
                    else:
                        ui.label(T['pc_pair_missing_token']).classes('text-negative q-mt-sm')
                        ui.button(T['pc_pair_btn_setup'], on_click=_run_pico_setup_for_pair).props('elevated color=purple-8').classes('q-mt-sm')

            # ── PicoClaw › Characters ──────────────────────────────────────
            with ui.tab_panel(t_pc_char):
                _build_character_tab('/var/lib/picoclaw/.picoclaw/workspace', 'picoclaw', 'purple-8')

            # ── PicoClaw › Skills ─────────────────────────────────────────
            with ui.tab_panel(t_pc_skills):
                _build_skills_tab('/var/lib/picoclaw/.picoclaw/workspace', 'picoclaw', 'purple-8')
    return pc_content
