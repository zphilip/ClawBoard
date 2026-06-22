"""ClawBoard dashboard — zeroclaw panel."""
import datetime
import os
import secrets as _secrets
import subprocess

from nicegui import ui


from dashboard.paths import CONFIG_PATH, DEPLOY_CONFIG_PATH, SCRIPT_DIR
from dashboard.config_io import deploy_config, save_config
from dashboard.auth import _hash_pw, _load_auth, _verify_pw
from dashboard.provider_hints import CHANNEL_LABELS, CHANNEL_SCHEMAS, PROVIDER_IDS
from urllib.parse import quote
import time as _time

CHANNEL_KEYS = list(CHANNEL_SCHEMAS.keys())
_invite_tokens = {}  # one-time invite tokens
def build_zeroclaw_panel(T, conf, zc_source, lang, other_lang, _ph_hints, _ph_map, _ph_models, _ph_pid_base, _ph_pid_models, provider_panels, channel_panels, do_status, _build_character_tab, _build_skills_tab, _loaded_from, to_int, lines_to_list, build_provider_card, build_channel_card, do_save, do_save_deploy, request):
    """Build panel UI."""
    _diag = ''  # populated by _wiz_apply; read by _wiz_refresh_summary

    # ── shortcuts (section aliases from conf) ───────────────────────────────
    top          = conf
    autonomy     = conf.get('autonomy',    {})
    agent_c      = conf.get('agent',       {})
    obs          = conf.get('observability',{})
    skills       = conf.get('skills',      {})
    memory       = conf.get('memory',      {})
    gateway      = conf.get('gateway',     {})
    ch_conf_top  = conf.get('channels', {})
    sec          = conf.get('security',    {})
    sec_res      = sec.get('resources',   {})
    sec_sandbox  = sec.get('sandbox',     {})
    sec_audit    = sec.get('audit',       {})
    sec_otp      = sec.get('otp',         {})
    sec_estop    = sec.get('estop',       {})
    reliability  = conf.get('reliability', {})
    scheduler    = conf.get('scheduler',   {})
    web_fetch    = conf.get('web_fetch',   {})
    web_search   = conf.get('web_search',  {})
    http_request = conf.get('http_request',{})
    browser      = conf.get('browser',     {})
    multimodal   = conf.get('multimodal',  {})
    cost         = conf.get('cost',        {})
    composio_c   = conf.get('composio',    {})
    tunnel       = conf.get('tunnel',      {})
    transcription= conf.get('transcription',{})
    heartbeat    = conf.get('heartbeat',   {})
    cron         = conf.get('cron',        {})
    hooks        = conf.get('hooks',       {})
    hardware     = conf.get('hardware',    {})
    identity     = conf.get('identity',    {})
    secrets_c    = conf.get('secrets',     {})
    _prov_models = conf.get('providers', {}).get('models', {})
    _default_prov = next(iter(_prov_models.values()), {}) if _prov_models else {}

    # ══ ZeroClaw Dashboard ════════════════════════════════════════════════════
    zc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    with zc_content:
        with ui.row().classes('w-full items-center q-mb-xs'):
            ui.label(T['zc_dashboard']).classes('text-h6 text-blue-9')
            ui.button(icon='refresh', on_click=lambda: ui.navigate.reload()) \
                .props('flat round dense color=blue-9').tooltip(T['tooltip_reload'])
        with ui.tabs().classes('w-full bg-blue-1') as zc_sub_tabs:
            t_zc_wiz  = ui.tab(T['tab_wizard'],        icon='auto_fix_high')
            t_zc_cfg  = ui.tab(T['tab_configuration'], icon='settings')
            t_zc_pair = ui.tab(T['tab_pair_device'],   icon='devices')
            t_zc_char   = ui.tab(T['tab_characters'],    icon='face')
            t_zc_skills = ui.tab(T['tab_skills'],        icon='extension')

        with ui.tab_panels(zc_sub_tabs, value=t_zc_cfg).classes('w-full'):

            # ── ZeroClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_zc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-blue-9 q-mb-xs')
                ui.label(
                    'Configure an AI provider and a messaging channel in 3 steps. '
                    'Click Apply at the end — then restart ZeroClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

                # ── Config source selector (same as Configuration tab) ─────
                with ui.row().classes('items-center gap-2 q-mb-sm'):
                    ui.label('Config source:').classes('text-caption text-grey-6')
                    _wiz_src_sel = ui.select(
                        {'runtime': 'Runtime  (/var/lib/zeroclaw/…)',
                         'local':   'Template (config/config.toml)'},
                        value=zc_source,
                    ).props('dense outlined').classes('text-caption').style('min-width: 240px')
                    def _wiz_reload_source():
                        new_src = _wiz_src_sel.value or 'local'
                        ui.navigate.to(f'/?lang={lang}&zc_source={new_src}')
                        _wiz_src_btn.props('loading')
                    _wiz_src_btn = ui.button('Load', icon='refresh', on_click=_wiz_reload_source
                        ).props('dense flat size=sm color=blue-7')

                with ui.stepper(value='wiz_provider').props('vertical animated').classes('w-full') as _wiz:

                    # ── Step 1: Provider ────────────────────────────────────
                    with ui.step('wiz_provider', title='1  Provider', icon='cloud'):
                        ui.label('Choose an AI provider and supply its API key.').classes('text-caption text-grey-6 q-mb-sm')

                        # ── Quick pick from provider_hints.json ────────────
                        _ph_map   = {h['model_name']: h for h in _ph_hints if h.get('model_name')}
                        _pid_base = _ph_pid_base  # reuse page-level dict

                        ui.label('⚡ Quick pick — fills fields automatically').classes('text-caption text-blue-7')
                        # Use a list (not dict) so e.value is the plain string key
                        wiz_quick = ui.select(
                            options=list(_ph_map.keys()),
                            label='Known model / provider',
                            value=None,
                            clearable=True,
                        ).classes('w-full q-mb-xs')
                        ui.separator().classes('q-my-xs')

                        wiz_prov_id = ui.select(
                            PROVIDER_IDS, label='Provider ID', value='openrouter',
                        ).classes('w-full q-mb-sm')
                        wiz_prov_alias = ui.input(
                            'Alias  (used as config key, e.g. "openrouter")',
                            value='openrouter').classes('w-full q-mb-sm')
                        wiz_prov_key = ui.input(
                            'API Key',
                            password=True, password_toggle_button=True).classes('w-full q-mb-sm')
                        wiz_prov_base = ui.input(
                            'base_url  (leave blank to use provider default)',
                            value='').classes('w-full q-mb-sm')
                        wiz_def_model = ui.input(
                            'default_model  (e.g. anthropic/claude-sonnet-4-6)',
                            value=str(top.get('default_model', ''))).classes('w-full')
                        wiz_is_fallback = ui.checkbox(
                            'Set as fallback provider  — used when the primary provider fails',
                            value=False)

                        # ── Callbacks defined AFTER all widgets so closures resolve ──
                        def _fill_from_hint(e):
                            """Fill provider fields from a quick-pick hint selection."""
                            picked = e.value
                            h = _ph_map.get(picked) if picked else None
                            if not h:
                                return
                            pid = h.get('provider_id', '') or h.get('model', '').split('/')[0]
                            if pid in PROVIDER_IDS:
                                # set provider_id — may cascade into _fill_from_pid,
                                # which writes prov_base; we overwrite it again below.
                                wiz_prov_id.set_value(pid)
                            suggested = h.get('suggested_alias', '')
                            if suggested:
                                wiz_prov_alias.set_value(suggested)
                            else:
                                slug = (h.get('model_name') or '').lower() \
                                    .replace(' ', '_').replace('-', '_').replace('.', '_') \
                                    .split('(')[0].rstrip('_')
                                if slug:
                                    wiz_prov_alias.set_value(slug)
                            if h.get('model'):
                                wiz_def_model.set_value(h['model'])
                            # Set base_url LAST so hint value wins over any cascade
                            wiz_prov_base.set_value(h.get('api_base', ''))

                        # Track last provider_id so _fill_from_pid can detect
                        # when the alias hasn't been customized (matches old pid)
                        _last_pid = ['openrouter']

                        def _fill_from_pid(e):
                            """Fill base_url from provider-ID selection.
                            Only auto-set alias when the current alias still
                            matches the previous provider-id — i.e. the user
                            hasn't customized it. This preserves regional
                            variants like minimax-cn set by the quick-pick."""
                            pid = e.value
                            if not pid:
                                return
                            wiz_prov_base.set_value(_pid_base.get(pid, ''))
                            cur_alias = (wiz_prov_alias.value or '').strip()
                            if not cur_alias or cur_alias == _last_pid[0]:
                                wiz_prov_alias.set_value(pid.split(':')[0])
                            _last_pid[0] = pid

                        # Register via .on_value_change() — callbacks are fully defined above
                        wiz_quick.on_value_change(_fill_from_hint)
                        wiz_prov_id.on_value_change(_fill_from_pid)

                        with ui.stepper_navigation():
                            ui.button('Next →', on_click=_wiz.next).props('color=blue-8')

                    # ── Step 2: Channel ─────────────────────────────────────
                    with ui.step('wiz_channel', title='2  Channel', icon='forum'):
                        ui.label('Pick a messaging channel to enable.').classes('text-caption text-grey-6 q-mb-sm')
                        wiz_ch_sel = ui.select(
                            {k: v['label'] for k, v in CHANNEL_SCHEMAS.items()},
                            label='Channel type',
                        ).classes('w-full q-mb-sm')
                        wiz_ch_container = ui.column().classes('w-full')
                        _wiz_ch_widgets: dict = {}

                        def _wiz_build_ch(e=None):
                            wiz_ch_container.clear()
                            _wiz_ch_widgets.clear()
                            key = wiz_ch_sel.value
                            schema = CHANNEL_SCHEMAS.get(key)
                            if not schema:
                                return
                            with wiz_ch_container:
                                for (fkey, flabel, ftype, fdefault) in schema['fields']:
                                    if ftype == 'text':
                                        _wiz_ch_widgets[fkey] = ui.input(flabel, value=str(fdefault)).classes('w-full')
                                    elif ftype == 'password':
                                        _wiz_ch_widgets[fkey] = ui.input(
                                            flabel, value='',
                                            password=True, password_toggle_button=True,
                                        ).classes('w-full')
                                    elif ftype == 'bool':
                                        _wiz_ch_widgets[fkey] = ui.checkbox(flabel, value=bool(fdefault))
                                    elif ftype == 'int':
                                        _wiz_ch_widgets[fkey] = ui.number(flabel, value=to_int(fdefault, 0), step=1).classes('w-full')
                                    elif ftype == 'textarea':
                                        ui.label(flabel).classes('text-caption text-grey-6')
                                        _wiz_ch_widgets[fkey] = ui.textarea(
                                            value='\n'.join(fdefault) if isinstance(fdefault, list) else str(fdefault)
                                        ).classes('w-full').props('outlined rows=3')
                                    elif ftype.startswith('select:'):
                                        opts = ftype.split(':', 1)[1].split(',')
                                        _wiz_ch_widgets[fkey] = ui.select(opts, label=flabel, value=opts[0]).classes('w-full')

                        wiz_ch_sel.on('update:model-value', _wiz_build_ch)

                        def _wiz_refresh_summary():
                            key = str(wiz_prov_key.value)
                            masked = ('*' * 6 + key[-4:]) if len(key) >= 6 else ('*' * len(key))
                            ch_label = CHANNEL_SCHEMAS.get(wiz_ch_sel.value or '', {}).get('label', '(none selected)')
                            lines = [
                                _diag,
                                '',
                                f'Provider:      {wiz_prov_id.value}',
                                f'Alias:         {wiz_prov_alias.value or "default"}',
                                f'API Key:       {masked if key else "(not set)"}',
                                f'base_url:      {wiz_prov_base.value or "(provider default)"}',
                                f'default_model: {wiz_def_model.value or "(unchanged)"}',
                                f'Fallback:      {"✓ YES" if wiz_is_fallback.value else "—"}',
                                f'Channel:       {ch_label}',
                                f'Web Search:    {"on" if wiz_tool_web_search.value else "off"}',
                                f'Web Fetch:     {"on" if wiz_tool_web_fetch.value else "off"}',
                                f'HTTP Request:  {"on" if wiz_tool_http_req.value else "off"}',
                                f'Browser:       {"on" if wiz_tool_browser.value else "off"}',
                                f'workspace_only:                  {wiz_sec_workspace_only.value}',
                                f'req_approval_medium_risk:        {wiz_sec_req_approval.value}',
                                f'block_high_risk_commands:        {wiz_sec_block_high.value}',
                            ]
                            wiz_summary.set_text('\n'.join(lines))

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →', on_click=_wiz.next).props('color=blue-8')

                    # ── Step 3: Tools & Security ─────────────────────────────
                    with ui.step('wiz_tools_sec', title='3  Tools & Security', icon='security'):
                        ui.label('Enable tools and configure security permissions.').classes('text-caption text-grey-6 q-mb-sm')

                        ui.label('🔧 Tools').classes('text-subtitle2 text-blue-8 q-mb-xs')
                        wiz_tool_web_search = ui.checkbox('Web Search', value=True)
                        wiz_tool_web_fetch  = ui.checkbox('Web Fetch', value=True)
                        wiz_tool_http_req   = ui.checkbox('HTTP Request', value=False)
                        wiz_tool_browser    = ui.checkbox('Browser', value=False)

                        ui.separator().classes('q-my-sm')
                        ui.label('🔒 Security').classes('text-subtitle2 text-blue-8 q-mb-xs')
                        wiz_sec_workspace_only = ui.checkbox('workspace_only  — restrict agent to workspace directory', value=True)
                        wiz_sec_req_approval   = ui.checkbox('require_approval_for_medium_risk', value=True)
                        wiz_sec_block_high     = ui.checkbox('block_high_risk_commands', value=True)

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →',
                                on_click=lambda: (_wiz_refresh_summary(), _wiz.next())
                            ).props('color=blue-8')

                    # ── Step 4: Apply ───────────────────────────────────────
                    with ui.step('wiz_apply', title='4  Apply', icon='check_circle'):
                        ui.label(
                            'Review the summary below, then click Apply & Save.'
                        ).classes('text-caption text-grey-6 q-mb-sm')
                        wiz_summary = ui.label('…').classes(
                            'text-caption text-mono bg-grey-2 q-pa-sm w-full q-mb-sm'
                        ).style('white-space: pre; border-radius: 4px;')
                        wiz_result_lbl = ui.label('').classes('text-caption w-full q-mb-xs')

                        def _wiz_apply():
                            nonlocal _diag
                            alias = (wiz_prov_alias.value or 'default').strip()
                            # ── provider
                            prov_entry: dict = {
                                'name': wiz_prov_id.value,
                                'requires_openai_auth': False,
                                'model': (wiz_def_model.value.strip() or ''),
                                'temperature': 0.7,
                                'timeout_secs': 120,
                            }
                            if wiz_prov_key.value:
                                prov_entry['api_key'] = wiz_prov_key.value
                            if wiz_prov_base.value: prov_entry['uri'] = wiz_prov_base.value
                            conf.setdefault('providers', {}).setdefault('models', {})
                            # Diagnostic: capture state before cleanup
                            _before_keys = list(conf['providers']['models'].keys())
                            # Remove any stale entries that share the same provider name
                            # (e.g. leftover "minimax" or "custom:https://..." aliases from
                            # previous buggy wizard runs) so only the current alias stays.
                            # tomlkit tables are MutableMapping, NOT dict — use hasattr, not isinstance.
                            pname = wiz_prov_id.value
                            for _k in list(conf['providers']['models'].keys()):
                                _v = conf['providers']['models'].get(_k)
                                if hasattr(_v, 'get') and _v.get('name') == pname and _k != alias:
                                    del conf['providers']['models'][_k]
                            conf['providers']['models'][alias] = prov_entry
                            # Diagnostic: capture state after cleanup
                            _after_keys = list(conf['providers']['models'].keys())
                            _loaded_src = _loaded_from or 'unknown'
                            _diag = (
                                f'📋 Loaded from: {_loaded_src}\n'
                                f'   Before cleanup: {sorted(_before_keys)}\n'
                                f'   After cleanup:  {sorted(_after_keys)}\n'
                                f'   Will save to:   {CONFIG_PATH}\n'
                                f'   Will deploy to: {DEPLOY_CONFIG_PATH}'
                            )
                            _wiz_refresh_summary()
                            # ── fallback
                            if wiz_is_fallback.value:
                                conf.setdefault('providers', {})['fallback'] = alias
                            # ── channel
                            ch_key = wiz_ch_sel.value
                            if ch_key and ch_key in CHANNEL_SCHEMAS:
                                ch_entry: dict = {'enabled': True}
                                for (fkey, _fl, ftype, _fd) in CHANNEL_SCHEMAS[ch_key]['fields']:
                                    w = _wiz_ch_widgets.get(fkey)
                                    if w is None: continue
                                    if ftype == 'textarea': ch_entry[fkey] = lines_to_list(w.value)
                                    elif ftype == 'bool':   ch_entry[fkey] = w.value
                                    elif ftype == 'int':    ch_entry[fkey] = to_int(w.value)
                                    else:                   ch_entry[fkey] = w.value
                                conf.setdefault('channels', {})[ch_key] = ch_entry
                            # ── tools
                            conf.setdefault('web_search', {})['enabled'] = wiz_tool_web_search.value
                            conf.setdefault('web_fetch', {})['enabled'] = wiz_tool_web_fetch.value
                            conf.setdefault('http_request', {})['enabled'] = wiz_tool_http_req.value
                            conf.setdefault('browser', {})['enabled'] = wiz_tool_browser.value
                            # ── security (autonomy)
                            conf.setdefault('autonomy', {})['workspace_only'] = wiz_sec_workspace_only.value
                            conf.setdefault('autonomy', {})['require_approval_for_medium_risk'] = wiz_sec_req_approval.value
                            conf.setdefault('autonomy', {})['block_high_risk_commands'] = wiz_sec_block_high.value
                            try:
                                save_config(conf)
                            except Exception as _e:
                                msg = f'❌ Save failed: {_e}'
                                ui.notify(msg, type='negative')
                                wiz_result_lbl.set_text(msg)
                                wiz_result_lbl.classes(remove='text-positive text-warning', add='text-negative')
                                return
                            ok_deploy, deploy_err = deploy_config(conf)
                            if not ok_deploy:
                                msg = f'⚠️ Saved locally but deploy failed: {deploy_err}\n{_diag}'
                                ui.notify(msg, type='warning')
                                wiz_result_lbl.set_text(msg)
                                wiz_result_lbl.classes(remove='text-positive text-negative', add='text-warning')
                                return
                            msg = f'✅ Wizard applied & deployed successfully\n{_diag}'
                            ui.notify(msg, type='positive')
                            wiz_result_lbl.set_text(msg)
                            wiz_result_lbl.classes(remove='text-warning text-negative', add='text-positive')
                            # Reload the page after a short delay to reflect the
                            # deployed config from disk (not the old in-memory conf)
                            ui.timer(2.0, lambda: ui.navigate.to(
                                f'/?lang={lang}&zc_source={zc_source}'))

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('✅ Apply & Deploy', on_click=_wiz_apply).props('color=green-8')

            # ── ZeroClaw › Configuration ───────────────────────────────────
            with ui.tab_panel(t_zc_cfg):

                # ── Header row: schema badge + config source selector ──────
                _zc_schema_ver = int(conf.get('schema_version', 0))
                with ui.row().classes('items-center gap-3 q-px-sm q-pt-xs q-pb-none'):
                    _zc_badge_color = 'green-7' if _zc_schema_ver >= 2 else 'orange-7'
                    ui.badge(f'schema v{_zc_schema_ver}', color=_zc_badge_color).props('outline')
                    ui.space()
                    ui.label('Config source:').classes('text-caption text-grey-6')
                    _zc_src_sel = ui.select(
                        {'runtime': 'Runtime  (/var/lib/zeroclaw/…)',
                         'local':   'Template (config/config.toml)'},
                        value=zc_source,
                    ).props('dense outlined').classes('text-caption').style('min-width: 240px')
                    def _zc_reload_source():
                        new_src = _zc_src_sel.value or 'local'
                        ui.navigate.to(f'/?lang={lang}&zc_source={new_src}')
                    ui.button('Load', icon='refresh', on_click=_zc_reload_source
                        ).props('dense flat size=sm color=blue-7')

                # ── Upgrade banner (shown when schema is below current version) ──
                if _zc_schema_ver < 2:
                    with ui.card().classes('w-full q-pa-sm q-mb-sm bg-orange-1'):
                        with ui.row().classes('items-center gap-2'):
                            ui.icon('warning', color='orange-8').classes('text-h6')
                            ui.label(
                                f'Config schema is version {_zc_schema_ver} — ZeroClaw requires version 2. '
                                'Click Migrate to run the built-in migration as the zeroclaw account.'
                            ).classes('text-caption text-orange-9')
                        _zc_upg_log = ui.textarea('').classes('w-full text-caption font-mono').props(
                            'outlined readonly rows=6 label="Migration output"')
                        _zc_upg_log.set_visibility(False)

                        async def _run_zc_cfg_migrate():
                            import asyncio as _aio
                            _zc_upg_log.set_visibility(True)
                            _zc_upg_log.set_value('⏳ Running zeroclaw config migrate…\n')
                            _zc_upg_btn.props('disabled loading')
                            try:
                                proc = await _aio.create_subprocess_exec(
                                    'sudo', '-u', 'zeroclaw', '/opt/zeroclaw/zeroclaw', 'config', 'migrate',
                                    stdout=_aio.subprocess.PIPE,
                                    stderr=_aio.subprocess.STDOUT,
                                )
                                buf = '⏳ Running zeroclaw config migrate…\n'
                                async for line in proc.stdout:
                                    buf += line.decode(errors='replace')
                                    _zc_upg_log.set_value(buf)
                                await proc.wait()
                                if proc.returncode == 0:
                                    _zc_upg_log.set_value(buf + '\n✅ Migration complete.')
                                    ui.notify('✅ Schema migrated — reloading page…', type='positive')
                                    ui.navigate.reload()
                                else:
                                    _zc_upg_log.set_value(buf + '\n⚠️ Migration exited with error.')
                                    ui.notify('⚠️ Migration returned an error', type='warning')
                            except Exception as _ex:
                                _zc_upg_log.set_value(f'❌ {_ex}')
                                ui.notify(f'❌ {_ex}', type='negative')
                            finally:
                                _zc_upg_btn.props(remove='disabled loading')

                        _zc_upg_btn = ui.button(
                            'Migrate schema to v2', icon='upgrade',
                            on_click=_run_zc_cfg_migrate,
                        ).props('color=orange-8 size=sm')

                with ui.tabs().classes('w-full bg-blue-1') as tabs:
                    t_gen   = ui.tab(T['tab_general'],   icon='tune')
                    t_prov  = ui.tab(T['tab_providers'],  icon='cloud')
                    t_auto  = ui.tab(T['tab_autonomy'],   icon='psychology')
                    t_agent = ui.tab(T['tab_agent'],      icon='smart_toy')
                    t_mem   = ui.tab(T['tab_memory'],     icon='memory')
                    t_comm  = ui.tab(T['tab_comms'],      icon='hub')
                    t_ch    = ui.tab(T['tab_channels'],   icon='forum')
                    t_sec   = ui.tab(T['tab_security'],   icon='security')
                    t_feat  = ui.tab(T['tab_features'],   icon='extension')
                    t_sys   = ui.tab(T['tab_system'],     icon='computer')

                with ui.tab_panels(tabs, value=t_gen).classes('w-full'):

                    # ══ General ══════════════════════════════════════════════
                    with ui.tab_panel(t_gen):
                        ui.label(T['section_api']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_api_key = ui.input(T['lbl_api_key'], value=str(_default_prov.get('api_key', '') or top.get('api_key', '')),
                            password=True, password_toggle_button=True).classes('w-full')
                        cur_prov = str(_default_prov.get('name', '') or top.get('default_provider', 'dashscope'))
                        _eff_prov = cur_prov if cur_prov in PROVIDER_IDS else PROVIDER_IDS[0]
                        w_default_provider = ui.select(PROVIDER_IDS, label='default_provider',
                            value=_eff_prov).classes('w-full')
                        _cur_def_model = str(_default_prov.get('model', '') or top.get('default_model', 'anthropic/claude-sonnet-4-6'))
                        # Seed model list from the currently selected provider
                        _init_models = _ph_pid_models.get(_eff_prov, _ph_models)
                        _dm_opts = list(_init_models) if _cur_def_model in _init_models \
                            else ([_cur_def_model] + list(_init_models))
                        w_default_model = ui.select(
                            options=_dm_opts,
                            label='default_model',
                            value=_cur_def_model,
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full')

                        # Re-populate model list when provider changes
                        def _on_prov_change(e):
                            pid    = e.value or ''
                            models = _ph_pid_models.get(pid, _ph_models)
                            cur    = w_default_model.value
                            new_val = cur if cur in models else (models[0] if models else cur)
                            w_default_model.set_options(list(models), value=new_val)
                        w_default_provider.on_value_change(_on_prov_change)
                        w_temperature = ui.number('default_temperature',
                            value=_default_prov.get('temperature', top.get('default_temperature', 0.7)), min=0.0, max=2.0, step=0.1).classes('w-full')
                        w_prov_timeout = ui.number('provider_timeout_secs',
                            value=_default_prov.get('timeout_secs', top.get('provider_timeout_secs', 120)), min=5, step=5).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_secrets']).classes('text-subtitle2 text-grey-7')
                        w_secrets_encrypt = ui.checkbox('secrets.encrypt', value=bool(secrets_c.get('encrypt', True)))
                        cur_id = str(identity.get('format', 'openclaw'))
                        w_identity_format = ui.select(['openclaw', 'aieos'], label='identity.format',
                            value=cur_id if cur_id in ['openclaw','aieos'] else 'openclaw').classes('w-full')

                    # ══ Providers ════════════════════════════════════════════
                    with ui.tab_panel(t_prov):
                        ui.label(T['section_providers']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['hint_providers']).classes('text-caption text-grey-5')
                        provider_container = ui.column().classes('w-full')
                        for alias, mp_data in conf.get('providers', {}).get('models', {}).items():
                            build_provider_card(provider_container, alias, mp_data)
                        ui.separator().classes('q-my-sm')
                        ui.label(T['lbl_add_provider']).classes('text-caption text-blue-7')
                        # unique provider ids from hints, preserving first-seen order
                        _seen_pids: list[str] = []
                        for _hh in _ph_hints:
                            _pid = _hh.get('provider_id', '')
                            if _pid and _pid not in _seen_pids:
                                _seen_pids.append(_pid)
                        new_prov_sel = ui.select(
                            options=_seen_pids,
                            label='Provider  (pick to auto-fill)',
                            value=None,
                            clearable=True,
                            with_input=True,
                        ).classes('w-full q-mb-xs')
                        with ui.row().classes('w-full gap-2 items-center'):
                            new_alias_input = ui.input(
                                'Alias  [providers.models.<alias>]  — auto-filled, edit for duplicates',
                                value='',
                            ).classes('flex-1')
                            ui.button(T['btn_add_provider'], on_click=lambda: _add_provider()).props('outline color=blue')

                        def _on_prov_sel(e):
                            pid = e.value or ''
                            # Only auto-fill alias if user hasn't manually changed it away from a known pid
                            if pid:
                                new_alias_input.set_value(pid)
                        new_prov_sel.on_value_change(_on_prov_sel)

                        def _add_provider():
                            alias = new_alias_input.value.strip()
                            if not alias:
                                ui.notify(T['warn_alias_empty'], type='warning'); return
                            if alias in provider_panels:
                                ui.notify(T['warn_alias_exists'].format(alias), type='warning'); return
                            pid = new_prov_sel.value or alias
                            pre = {
                                'name': pid,
                                'uri':  _ph_pid_base.get(pid, ''),
                            }
                            build_provider_card(provider_container, alias, pre)
                            new_alias_input.set_value('')
                            new_prov_sel.set_value(None)

                    # ══ Autonomy ═════════════════════════════════════════════
                    with ui.tab_panel(t_auto):
                        ui.label(T['section_autonomy']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        cur_lvl = autonomy.get('level', 'supervised')
                        w_auto_level = ui.select(['read_only', 'supervised', 'full'], label='autonomy.level',
                            value=cur_lvl if cur_lvl in ['read_only','supervised','full'] else 'supervised').classes('w-full')
                        w_auto_workspace        = ui.checkbox('workspace_only',                   value=autonomy.get('workspace_only', True))
                        w_auto_require_approval = ui.checkbox('require_approval_for_medium_risk', value=autonomy.get('require_approval_for_medium_risk', True))
                        w_auto_block_high       = ui.checkbox('block_high_risk_commands',          value=autonomy.get('block_high_risk_commands', True))
                        ui.separator().classes('q-my-sm')
                        w_auto_max_actions = ui.number('max_actions_per_hour',  value=autonomy.get('max_actions_per_hour', 20),   min=1,  step=1).classes('w-full')
                        w_auto_max_cost    = ui.number('max_cost_per_day_cents', value=autonomy.get('max_cost_per_day_cents', 500), min=0,  step=10).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['lbl_allowed_commands']).classes('text-caption text-grey-6')
                        w_auto_cmds = ui.textarea(value='\n'.join(autonomy.get('allowed_commands', []))).classes('w-full').props('outlined rows=5')
                        ui.label(T['lbl_auto_approve']).classes('text-caption text-grey-6')
                        w_auto_approve = ui.textarea(value='\n'.join(autonomy.get('auto_approve', []))).classes('w-full').props('outlined rows=3')
                        ui.label(T['lbl_always_ask']).classes('text-caption text-grey-6')
                        w_auto_always_ask = ui.textarea(value='\n'.join(autonomy.get('always_ask', []))).classes('w-full').props('outlined rows=3')
                        ui.label(T['lbl_forbidden_paths']).classes('text-caption text-grey-6')
                        w_auto_forbidden = ui.textarea(value='\n'.join(autonomy.get('forbidden_paths', []))).classes('w-full').props('outlined rows=5')
                        ui.label(T['lbl_allowed_roots']).classes('text-caption text-grey-6')
                        w_auto_allowed_roots = ui.textarea(value='\n'.join(autonomy.get('allowed_roots', []))).classes('w-full').props('outlined rows=3')
                        ui.label(T['lbl_shell_env']).classes('text-caption text-grey-6')
                        w_auto_shell_env = ui.textarea(value='\n'.join(autonomy.get('shell_env_passthrough', []))).classes('w-full').props('outlined rows=3')

                    # ══ Agent ════════════════════════════════════════════════
                    with ui.tab_panel(t_agent):
                        ui.label(T['section_agent']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_agent_compact  = ui.checkbox('compact_context', value=agent_c.get('compact_context', False))
                        w_agent_parallel = ui.checkbox('parallel_tools',  value=agent_c.get('parallel_tools', False))
                        w_agent_max_iter = ui.number('max_tool_iterations',  value=agent_c.get('max_tool_iterations', 10),  min=1, step=1).classes('w-full')
                        w_agent_max_hist = ui.number('max_history_messages', value=agent_c.get('max_history_messages', 50), min=1, step=5).classes('w-full')
                        cur_disp = agent_c.get('tool_dispatcher', 'auto')
                        w_agent_tool_dispatcher = ui.select(['auto', 'sequential', 'parallel'], label='tool_dispatcher',
                            value=cur_disp if cur_disp in ['auto','sequential','parallel'] else 'auto').classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_obs']).classes('text-subtitle2 text-grey-7')
                        cur_obs = obs.get('backend', 'none')
                        w_obs_backend = ui.select(['none', 'noop', 'log', 'prometheus', 'otel'], label='backend',
                            value=cur_obs if cur_obs in ['none','noop','log','prometheus','otel'] else 'none').classes('w-full')
                        cur_tm = obs.get('runtime_trace_mode', obs.get('log_persistence', 'none'))
                        w_obs_trace_mode = ui.select(['none', 'rolling', 'full'], label='runtime_trace_mode',
                            value=cur_tm if cur_tm in ['none','rolling','full'] else 'none').classes('w-full')
                        w_obs_otel_endpoint = ui.input('otel_endpoint', value=str(obs.get('otel_endpoint', 'http://localhost:4318'))).classes('w-full')
                        w_obs_otel_service  = ui.input('otel_service_name', value=str(obs.get('otel_service_name', 'zeroclaw'))).classes('w-full')
                        w_obs_trace_path    = ui.input('runtime_trace_path', value=str(obs.get('runtime_trace_path', obs.get('log_persistence_path', 'state/runtime-trace.jsonl')))).classes('w-full')
                        w_obs_trace_max     = ui.number('runtime_trace_max_entries', value=obs.get('runtime_trace_max_entries', obs.get('log_persistence_max_entries', 200)), min=10, step=50).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_skills']).classes('text-subtitle2 text-grey-7')
                        w_skills_open = ui.checkbox('open_skills_enabled', value=skills.get('open_skills_enabled', False))
                        w_skills_allow_scripts = ui.checkbox('allow_scripts', value=skills.get('allow_scripts', True))
                        cur_pm = skills.get('prompt_injection_mode', 'full')
                        w_skills_mode = ui.select(['full', 'compact'], label='prompt_injection_mode',
                            value=cur_pm if cur_pm in ['full','compact'] else 'full').classes('w-full')
                        ui.separator().classes('q-my-xs')
                        ui.label('skill_creation').classes('text-caption text-blue-7 text-bold')
                        _sk_cr = skills.get('skill_creation', {})
                        w_sk_cr_enabled = ui.checkbox('skill_creation.enabled', value=_sk_cr.get('enabled', False))
                        w_sk_cr_max = ui.number('skill_creation.max_skills', value=_sk_cr.get('max_skills', 500), min=1, step=10).classes('w-full')
                        w_sk_cr_sim = ui.number('skill_creation.similarity_threshold', value=_sk_cr.get('similarity_threshold', 0.85), min=0.0, max=1.0, step=0.01).classes('w-full')
                        ui.label('skill_improvement').classes('text-caption text-blue-7 text-bold')
                        _sk_im = skills.get('skill_improvement', {})
                        w_sk_im_enabled = ui.checkbox('skill_improvement.enabled', value=_sk_im.get('enabled', True))
                        w_sk_im_cooldown = ui.number('skill_improvement.cooldown_secs', value=_sk_im.get('cooldown_secs', 3600), min=60, step=300).classes('w-full')

                    # ══ Memory ═══════════════════════════════════════════════
                    with ui.tab_panel(t_mem):
                        ui.label(T['section_storage']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        cur_mb = memory.get('backend', 'sqlite')
                        w_mem_backend = ui.select(['sqlite', 'lucid', 'markdown', 'none'], label='backend',
                            value=cur_mb if cur_mb in ['sqlite','lucid','markdown','none'] else 'sqlite').classes('w-full')
                        cur_sm = str(memory.get('search_mode', 'hybrid'))
                        w_mem_search_mode = ui.select(['hybrid', 'fts', 'vector', 'cache'], label='search_mode',
                            value=cur_sm if cur_sm in ['hybrid','fts','vector','cache'] else 'hybrid').classes('w-full')
                        w_mem_auto_save     = ui.checkbox('auto_save',       value=memory.get('auto_save', True))
                        w_mem_hygiene       = ui.checkbox('hygiene_enabled', value=memory.get('hygiene_enabled', True))
                        w_mem_auto_hydrate  = ui.checkbox('auto_hydrate',    value=memory.get('auto_hydrate', True))
                        w_mem_archive_days  = ui.number('archive_after_days',          value=memory.get('archive_after_days', 7),   min=1, step=1).classes('w-full')
                        w_mem_purge_days    = ui.number('purge_after_days',            value=memory.get('purge_after_days', 30),    min=1, step=1).classes('w-full')
                        w_mem_conv_retention= ui.number('conversation_retention_days', value=memory.get('conversation_retention_days', 30), min=1, step=1).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_embedding']).classes('text-subtitle2 text-grey-7')
                        cur_ep = memory.get('embedding_provider', 'none')
                        w_mem_embed_provider = ui.select(['none', 'openai', 'custom:<url>'], label='embedding_provider',
                            value=cur_ep if cur_ep in ['none','openai','custom:<url>'] else 'none').classes('w-full')
                        w_mem_embed_model  = ui.input('embedding_model',   value=str(memory.get('embedding_model', 'text-embedding-3-small'))).classes('w-full')
                        w_mem_embed_dims   = ui.number('embedding_dimensions', value=memory.get('embedding_dimensions', 1536),   min=64,  step=128).classes('w-full')
                        w_mem_vec_weight   = ui.number('vector_weight',        value=memory.get('vector_weight', 0.7),           min=0.0, max=1.0, step=0.05).classes('w-full')
                        w_mem_kw_weight    = ui.number('keyword_weight',       value=memory.get('keyword_weight', 0.3),          min=0.0, max=1.0, step=0.05).classes('w-full')
                        w_mem_min_relevance= ui.number('min_relevance_score',  value=memory.get('min_relevance_score', 0.4),     min=0.0, max=1.0, step=0.05).classes('w-full')
                        w_mem_cache_size   = ui.number('embedding_cache_size', value=memory.get('embedding_cache_size', 10000),  min=0,   step=1000).classes('w-full')
                        w_mem_chunk_tokens = ui.number('chunk_max_tokens',     value=memory.get('chunk_max_tokens', 512),        min=64,  step=64).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_cache']).classes('text-subtitle2 text-grey-7')
                        w_mem_resp_cache   = ui.checkbox('response_cache_enabled', value=memory.get('response_cache_enabled', False))
                        w_mem_snapshot     = ui.checkbox('snapshot_enabled',       value=memory.get('snapshot_enabled', False))
                        w_mem_snap_hygiene = ui.checkbox('snapshot_on_hygiene',    value=memory.get('snapshot_on_hygiene', False))
                        w_mem_resp_ttl     = ui.number('response_cache_ttl_minutes',  value=memory.get('response_cache_ttl_minutes', 60),   min=1, step=5).classes('w-full')
                        w_mem_resp_max     = ui.number('response_cache_max_entries',  value=memory.get('response_cache_max_entries', 5000), min=0, step=500).classes('w-full')

                    # ══ Comms ════════════════════════════════════════════════
                    with ui.tab_panel(t_comm):
                        ui.label(T['section_gateway']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_gw_port    = ui.number('port', value=gateway.get('port', 42617), min=1024, max=65535, step=1).classes('w-full')
                        w_gw_host    = ui.input('host',  value=str(gateway.get('host', '127.0.0.1'))).classes('w-full')
                        w_gw_pairing = ui.checkbox('require_pairing',   value=gateway.get('require_pairing', True))
                        w_gw_public  = ui.checkbox('allow_public_bind', value=gateway.get('allow_public_bind', False))
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_tunnel']).classes('text-subtitle2 text-grey-7')
                        cur_tn = tunnel.get('provider', tunnel.get('tunnel_provider', 'none'))
                        _tunnel_backends = ['none', 'cloudflare', 'tailscale', 'ngrok', 'openvpn', 'pinggy', 'custom']
                        w_tunnel = ui.select(_tunnel_backends, label='tunnel.provider',
                            value=cur_tn if cur_tn in _tunnel_backends else 'none').classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_channels_global']).classes('text-subtitle2 text-grey-7')
                        w_cli_enabled = ui.checkbox(T['lbl_cli'], value=ch_conf_top.get('cli', True))
                        w_msg_timeout = ui.number('message_timeout_secs', value=ch_conf_top.get('message_timeout_secs', 300), min=30, step=30).classes('w-full')
                        w_ch_ack_reactions    = ui.checkbox('ack_reactions',       value=bool(ch_conf_top.get('ack_reactions', True)))
                        w_ch_show_tool_calls  = ui.checkbox('show_tool_calls',     value=bool(ch_conf_top.get('show_tool_calls', False)))
                        w_ch_session_persist  = ui.checkbox('session_persistence', value=bool(ch_conf_top.get('session_persistence', True)))
                        cur_sb = str(ch_conf_top.get('session_backend', 'sqlite'))
                        w_ch_session_backend  = ui.select(['sqlite', 'memory', 'none'], label='session_backend',
                            value=cur_sb if cur_sb in ['sqlite','memory','none'] else 'sqlite').classes('w-full')
                        w_ch_session_ttl      = ui.number('session_ttl_hours',  value=ch_conf_top.get('session_ttl_hours', 0),  min=0, step=1).classes('w-full')
                        w_ch_debounce_ms      = ui.number('debounce_ms',         value=ch_conf_top.get('debounce_ms', 0),        min=0, step=50).classes('w-full')

                    # ══ Channels ═════════════════════════════════════════════
                    with ui.tab_panel(t_ch):
                        ui.label(T['section_channels']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['hint_channels']).classes('text-caption text-grey-5')
                        channel_container = ui.column().classes('w-full')
                        for ch_key in CHANNEL_KEYS:
                            if ch_key in ch_conf_top and isinstance(ch_conf_top[ch_key], dict):
                                build_channel_card(channel_container, ch_key, ch_conf_top[ch_key])
                        ui.separator().classes('q-my-sm')
                        with ui.row().classes('w-full gap-2 items-end'):
                            new_ch_select = ui.select({k: v for k, v in CHANNEL_LABELS.items()},
                                label=T['lbl_channel_type']).classes('flex-1')
                            def _add_channel():
                                ch_key = new_ch_select.value
                                if not ch_key: ui.notify(T['warn_channel_empty'], type='warning'); return
                                if ch_key in channel_panels:
                                    ui.notify(T['warn_channel_exists'].format(CHANNEL_LABELS.get(ch_key, ch_key)), type='warning'); return
                                build_channel_card(channel_container, ch_key, {})
                            ui.button(T['btn_add_channel'], on_click=_add_channel).props('outline color=green')

                    # ══ Security ═════════════════════════════════════════════
                    with ui.tab_panel(t_sec):
                        with ui.expansion(T['exp_dashboard_access'], icon='vpn_key').classes('w-full'):
                            ui.label(T['lbl_change_password']).classes('text-subtitle2 q-mt-xs')
                            w_cur_pw  = ui.input(T['lbl_cur_pw'],    password=True, password_toggle_button=True).classes('w-full')
                            w_new_pw  = ui.input(T['lbl_new_pw'],    password=True, password_toggle_button=True).classes('w-full')
                            w_new_pw2 = ui.input(T['lbl_confirm_pw'], password=True, password_toggle_button=True).classes('w-full')
                            def do_change_pw():
                                d = _load_auth()
                                if not d or not _verify_pw(w_cur_pw.value, d['password_hash']):
                                    ui.notify(T['notify_pw_wrong'], type='negative'); return
                                if len(w_new_pw.value) < 6:
                                    ui.notify(T['notify_pw_short'], type='warning'); return
                                if w_new_pw.value != w_new_pw2.value:
                                    ui.notify(T['notify_pw_mismatch'], type='warning'); return
                                d['password_hash'] = _hash_pw(w_new_pw.value)
                                _save_auth(d)
                                ui.notify(T['notify_pw_changed'], type='positive')
                                w_cur_pw.set_value(''); w_new_pw.set_value(''); w_new_pw2.set_value('')
                            ui.button(T['btn_change_pw'], on_click=do_change_pw).props('outline color=blue').classes('q-mb-sm')
                        with ui.expansion(T['exp_resources'], icon='memory').classes('w-full'):
                            w_sec_mem         = ui.number('max_memory_mb',        value=sec_res.get('max_memory_mb', 512),        min=64,  step=64).classes('w-full')
                            w_sec_cpu         = ui.number('max_cpu_time_seconds', value=sec_res.get('max_cpu_time_seconds', 60),  min=5,   step=5).classes('w-full')
                            w_sec_procs       = ui.number('max_subprocesses',     value=sec_res.get('max_subprocesses', 10),      min=1,   step=1).classes('w-full')
                            w_sec_mem_monitor = ui.checkbox('memory_monitoring',  value=bool(sec_res.get('memory_monitoring', True)))
                        with ui.expansion(T['exp_sandbox'], icon='shield').classes('w-full'):
                            cur_sb = sec_sandbox.get('backend', 'auto')
                            w_sec_sandbox = ui.select(['auto', 'firejail', 'none'], label='sandbox.backend',
                                value=cur_sb if cur_sb in ['auto','firejail','none'] else 'auto').classes('w-full')
                        with ui.expansion(T['exp_audit'], icon='fact_check').classes('w-full'):
                            w_sec_audit_enabled  = ui.checkbox('enabled',     value=bool(sec_audit.get('enabled', True)))
                            w_sec_audit_log_path = ui.input('log_path',       value=str(sec_audit.get('log_path', 'audit.log'))).classes('w-full')
                            w_sec_audit_max      = ui.number('max_size_mb',   value=sec_audit.get('max_size_mb', 100), min=1, step=10).classes('w-full')
                            w_sec_audit_sign     = ui.checkbox('sign_events', value=bool(sec_audit.get('sign_events', False)))
                        with ui.expansion(T['exp_otp'], icon='lock').classes('w-full'):
                            w_sec_otp_enabled = ui.checkbox('enabled', value=bool(sec_otp.get('enabled', False)))
                            cur_om = sec_otp.get('method', 'totp')
                            w_sec_otp_method  = ui.select(['totp', 'pairing', 'cli-prompt'], label='method',
                                value=cur_om if cur_om in ['totp','pairing','cli-prompt'] else 'totp').classes('w-full')
                            w_sec_otp_ttl     = ui.number('token_ttl_secs',   value=sec_otp.get('token_ttl_secs', 30),    min=10, step=5).classes('w-full')
                            w_sec_otp_cache   = ui.number('cache_valid_secs', value=sec_otp.get('cache_valid_secs', 300), min=30, step=30).classes('w-full')
                            ui.label(T['lbl_otp_actions']).classes('text-caption text-grey-6')
                            w_sec_otp_actions = ui.textarea(value='\n'.join(sec_otp.get('gated_actions',
                                ['shell', 'file_write', 'browser_open', 'browser', 'memory_forget']))).classes('w-full').props('outlined rows=4')
                            ui.label(T['lbl_otp_domains']).classes('text-caption text-grey-6')
                            w_sec_otp_domains = ui.textarea(value='\n'.join(sec_otp.get('gated_domains', []))).classes('w-full').props('outlined rows=3')
                        with ui.expansion(T['exp_estop'], icon='emergency').classes('w-full'):
                            w_sec_estop_enabled = ui.checkbox('enabled',               value=bool(sec_estop.get('enabled', False)))
                            w_sec_estop_file    = ui.input('state_file',               value=str(sec_estop.get('state_file', '~/.zeroclaw/estop-state.json'))).classes('w-full')
                            w_sec_estop_otp     = ui.checkbox('require_otp_to_resume', value=bool(sec_estop.get('require_otp_to_resume', True)))
                        with ui.expansion(T['exp_reliability'], icon='sync').classes('w-full'):
                            w_rel_retries    = ui.number('provider_retries',             value=reliability.get('provider_retries', 2),            min=0, step=1).classes('w-full')
                            w_rel_backoff    = ui.number('provider_backoff_ms',          value=reliability.get('provider_backoff_ms', 500),        min=0, step=100).classes('w-full')
                            w_rel_ch_backoff = ui.number('channel_initial_backoff_secs', value=reliability.get('channel_initial_backoff_secs', 2), min=1, step=1).classes('w-full')
                            w_rel_ch_max     = ui.number('channel_max_backoff_secs',     value=reliability.get('channel_max_backoff_secs', 60),    min=5, step=5).classes('w-full')
                        with ui.expansion(T['exp_scheduler'], icon='schedule').classes('w-full'):
                            w_sched_enabled    = ui.checkbox('enabled', value=scheduler.get('enabled', True))
                            w_sched_tasks      = ui.number('max_tasks',      value=scheduler.get('max_tasks', 64),     min=1, step=8).classes('w-full')
                            w_sched_concurrent = ui.number('max_concurrent', value=scheduler.get('max_concurrent', 4), min=1, step=1).classes('w-full')

                    # ══ Features ═════════════════════════════════════════════
                    with ui.tab_panel(t_feat):
                        with ui.expansion(T['exp_webfetch'], icon='download').classes('w-full'):
                            w_wf_enabled  = ui.checkbox('enabled', value=web_fetch.get('enabled', False))
                            ui.label(T['lbl_wf_allowed']).classes('text-caption text-grey-6')
                            w_wf_domains  = ui.textarea(value='\n'.join(web_fetch.get('allowed_domains', ['*']))).classes('w-full').props('outlined rows=3')
                            ui.label(T['lbl_wf_blocked']).classes('text-caption text-grey-6')
                            w_wf_blocked  = ui.textarea(value='\n'.join(web_fetch.get('blocked_domains', []))).classes('w-full').props('outlined rows=3')
                            w_wf_max_size = ui.number('max_response_size (bytes)', value=web_fetch.get('max_response_size', 500000), min=1000, step=100000).classes('w-full')
                            w_wf_timeout  = ui.number('timeout_secs',              value=web_fetch.get('timeout_secs', 30),         min=5,    step=5).classes('w-full')
                        with ui.expansion(T['exp_websearch'], icon='search').classes('w-full'):
                            w_ws_enabled  = ui.checkbox('enabled', value=web_search.get('enabled', False))
                            cur_wsp = web_search.get('provider', web_search.get('search_provider', 'duckduckgo'))
                            _ws_providers = ['duckduckgo', 'brave', 'tavily', 'searxng']
                            w_ws_provider = ui.select(_ws_providers, label='provider',
                                value=cur_wsp if cur_wsp in _ws_providers else 'duckduckgo').classes('w-full')
                            w_ws_max      = ui.number('max_results',  value=web_search.get('max_results', 5),   min=1, step=1).classes('w-full')
                            w_ws_timeout  = ui.number('timeout_secs', value=web_search.get('timeout_secs', 15), min=5, step=5).classes('w-full')
                        with ui.expansion(T['exp_httpreq'], icon='http').classes('w-full'):
                            w_http_enabled  = ui.checkbox('enabled', value=http_request.get('enabled', False))
                            ui.label(T['lbl_http_allowed']).classes('text-caption text-grey-6')
                            w_http_domains  = ui.textarea(value='\n'.join(http_request.get('allowed_domains', []))).classes('w-full').props('outlined rows=3')
                            w_http_max_size = ui.number('max_response_size (bytes)', value=http_request.get('max_response_size', 1000000), min=1000, step=100000).classes('w-full')
                            w_http_timeout  = ui.number('timeout_secs',              value=http_request.get('timeout_secs', 30),           min=5,    step=5).classes('w-full')
                        with ui.expansion(T['exp_browser'], icon='open_in_browser').classes('w-full'):
                            w_br_enabled   = ui.checkbox('enabled', value=browser.get('enabled', False))
                            ui.label(T['lbl_br_allowed']).classes('text-caption text-grey-6')
                            w_br_domains   = ui.textarea(value='\n'.join(browser.get('allowed_domains', []))).classes('w-full').props('outlined rows=3')
                            cur_bb = browser.get('backend', 'agent_browser')
                            w_br_backend   = ui.select(['agent_browser', 'rust_native', 'computer_use', 'auto'], label='backend',
                                value=cur_bb if cur_bb in ['agent_browser','rust_native','computer_use','auto'] else 'agent_browser').classes('w-full')
                            w_br_headless  = ui.checkbox('native_headless',      value=bool(browser.get('native_headless', True)))
                            w_br_webdriver = ui.input('native_webdriver_url',    value=str(browser.get('native_webdriver_url', 'http://127.0.0.1:9515'))).classes('w-full')
                        with ui.expansion(T['exp_multimodal'], icon='image').classes('w-full'):
                            w_mm_images     = ui.number('max_images',        value=multimodal.get('max_images', 4),        min=1, step=1).classes('w-full')
                            w_mm_image_size = ui.number('max_image_size_mb', value=multimodal.get('max_image_size_mb', 5), min=1, step=1).classes('w-full')
                            w_mm_remote     = ui.checkbox('allow_remote_fetch', value=bool(multimodal.get('allow_remote_fetch', False)))
                        with ui.expansion(T['exp_cost'], icon='attach_money').classes('w-full'):
                            w_cost_enabled  = ui.checkbox('enabled',         value=cost.get('enabled', False))
                            w_cost_override = ui.checkbox('allow_override',  value=bool(cost.get('allow_override', False)))
                            w_cost_daily    = ui.number('daily_limit_usd',   value=cost.get('daily_limit_usd', 10.0),    min=0, step=1.0).classes('w-full')
                            w_cost_monthly  = ui.number('monthly_limit_usd', value=cost.get('monthly_limit_usd', 100.0), min=0, step=5.0).classes('w-full')
                            w_cost_warn     = ui.number('warn_at_percent',   value=cost.get('warn_at_percent', 80),      min=10, max=100, step=5).classes('w-full')
                        with ui.expansion(T['exp_composio'], icon='hub').classes('w-full'):
                            w_comp_enabled = ui.checkbox('enabled', value=bool(composio_c.get('enabled', False)))
                            w_comp_entity  = ui.input('entity_id', value=str(composio_c.get('entity_id', 'default'))).classes('w-full')
                        with ui.expansion(T['exp_hooks'], icon='webhook').classes('w-full'):
                            w_hooks_enabled = ui.checkbox('hooks.enabled', value=bool(hooks.get('enabled', True)))
                        with ui.expansion(T['exp_hardware'], icon='developer_board').classes('w-full'):
                            w_hw_enabled    = ui.checkbox('enabled', value=bool(hardware.get('enabled', False)))
                            cur_ht = hardware.get('transport', 'none')
                            w_hw_transport  = ui.select(['None', 'native', 'serial', 'probe'], label='transport',
                                value=cur_ht if cur_ht in ['None','native','serial','probe'] else 'None').classes('w-full')
                            w_hw_baud       = ui.number('baud_rate',           value=hardware.get('baud_rate', 115200), min=1200, step=9600).classes('w-full')
                            w_hw_datasheets = ui.checkbox('workspace_datasheets', value=bool(hardware.get('workspace_datasheets', False)))

                    # ══ System ═══════════════════════════════════════════════
                    with ui.tab_panel(t_sys):
                        ui.label(T['section_transcription']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_tr_enabled      = ui.checkbox('enabled', value=transcription.get('enabled', False))
                        w_tr_url          = ui.input('api_url', value=str(transcription.get('api_url', 'https://api.groq.com/openai/v1/audio/transcriptions'))).classes('w-full')
                        w_tr_model        = ui.input('model',   value=str(transcription.get('model', 'whisper-large-v3-turbo'))).classes('w-full')
                        w_tr_max_duration = ui.number('max_duration_secs', value=transcription.get('max_duration_secs', 120), min=10, step=10).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_heartbeat']).classes('text-subtitle2 text-grey-7')
                        w_hb_enabled  = ui.checkbox('enabled', value=heartbeat.get('enabled', False))
                        w_hb_interval = ui.number('interval_minutes', value=heartbeat.get('interval_minutes', 30), min=1, step=5).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_cron']).classes('text-subtitle2 text-grey-7')
                        w_cron_enabled     = ui.checkbox('enabled', value=cron.get('enabled', True))
                        w_cron_max_history = ui.number('max_run_history', value=cron.get('max_run_history', 50), min=1, step=10).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_logs']).classes('text-subtitle2 text-grey-7')
                        with ui.row().classes('w-full gap-2'):
                            ui.button(T['btn_view_logs'], icon='article', on_click=lambda: ui.notify(
                                subprocess.getoutput('journalctl -u zeroclaw.service -n 30 --no-pager'),
                                multi_line=True, timeout=15000)).props('outline').classes('flex-1')
                            ui.button(T['btn_service_status'], icon='info', on_click=do_status).props('outline').classes('flex-1')

                ui.separator()
                # ── Pre-save hook: collect form values that live in this scope ──
                def _pre_save():
                    conf.setdefault('secrets',  {})['encrypt'] = w_secrets_encrypt.value
                    conf.setdefault('identity', {})['format']  = w_identity_format.value
                def _do_save():
                    _pre_save()
                    do_save()
                def _do_save_deploy():
                    _pre_save()
                    do_save_deploy()
                with ui.row().classes('w-full gap-2 q-pa-sm'):
                    ui.button(T['btn_save'],         on_click=_do_save).props('elevated').classes('flex-1 bg-blue text-white')
                    ui.button(T['btn_save_deploy'], on_click=_do_save_deploy).props('elevated').classes('flex-1 bg-green text-white')

            # ── ZeroClaw › Pair Device ─────────────────────────────────────
            with ui.tab_panel(t_zc_pair):
                # ── Gateway pair code ──────────────────────────────────────
                with ui.card().classes('w-full q-mb-sm'):
                    ui.label(T['pair_gw_title']).classes('text-subtitle1 text-bold q-mb-xs')
                    ui.label(T['pair_gw_hint']).classes('text-caption text-grey-6 q-mb-sm')
                    paircode_box = ui.card().classes('w-full bg-grey-2 q-pa-md items-center')
                    with paircode_box:
                        paircode_lbl = ui.label('…').classes(
                            'text-h4 text-bold text-blue-9 text-center letter-spacing-wide'
                        )
                        paircode_status = ui.label(T['pair_loading']).classes('text-caption text-grey-6 text-center q-mt-xs')

                    _PAIRCODE_SCRIPT = os.path.join(SCRIPT_DIR, 'clawberry_paircode.py')

                    def _parse_paircode(raw: str):
                        """Extract the numeric code from box-drawing output. Returns None if not found."""
                        for _line in raw.splitlines():
                            _s = _line.strip()
                            if _s.startswith('│') and _s.endswith('│'):
                                _inner = _s[1:-1].strip()
                                if _inner:
                                    return _inner
                        return None  # no code in output

                    def _push_to_display(code: str):
                        """Queue pair code for the clawberry-display service via handoff file."""
                        try:
                            import importlib
                            import clawberry_paircode as _cp
                            importlib.reload(_cp)
                            _cp.request_paircode_display(code)
                            paircode_status.set_text('✅ Queued — sending to e-ink display')
                        except Exception as exc:
                            paircode_status.set_text(f'⚠️ Could not queue display: {exc}')
                            ui.notify(f'Display queue error: {exc}', type='warning')

                    def _fetch_paircode(new: bool = False, push_display: bool = True):
                        """Fetch current (or generate new) pair code and update UI.
                        Only pushes to e-ink display when push_display=True."""
                        paircode_lbl.set_text('…')
                        paircode_status.set_text('Generating new code…' if new else 'Fetching current code…')
                        try:
                            cmd = ['zeroclaw', 'gateway', 'get-paircode']
                            if new:
                                cmd.append('--new')
                            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                            raw = r.stdout.strip() or r.stderr.strip()
                            if r.returncode != 0:
                                paircode_lbl.set_text('Error')
                                paircode_status.set_text(f'Command failed: {raw[:80]}')
                                ui.notify(f'❌ {raw}', type='negative')
                                return
                            code = _parse_paircode(raw)
                            if code is None:
                                paircode_lbl.set_text('NA')
                                paircode_status.set_text('No available pair code')
                                return
                            paircode_lbl.set_text(code)
                            if push_display:
                                _push_to_display(code)
                                if new:
                                    ui.notify(f'✅ New pair code: {code}', type='positive')
                            else:
                                paircode_status.set_text('Code fetched (not pushed to display)')
                        except FileNotFoundError:
                            paircode_lbl.set_text('N/A')
                            paircode_status.set_text('zeroclaw not found in PATH')
                            ui.notify('❌ zeroclaw not found in PATH', type='negative')
                        except subprocess.TimeoutExpired:
                            paircode_lbl.set_text('Timeout')
                            paircode_status.set_text('Command timed out')
                            ui.notify('❌ Command timed out', type='negative')
                        except Exception as exc:
                            paircode_lbl.set_text('Error')
                            paircode_status.set_text(str(exc))
                            ui.notify(f'❌ {exc}', type='negative')

                    # Idle state — user presses a button to load/generate
                    paircode_lbl.set_text('NA')
                    paircode_status.set_text(T['pair_idle'])

                    with ui.row().classes('w-full gap-2 q-mt-sm'):
                        ui.button(T['pair_btn_refresh'], on_click=lambda: _fetch_paircode(new=False, push_display=True)).props(
                            'outline color=blue-8'
                        ).classes('flex-1')
                        ui.button(T['pair_btn_new'], on_click=lambda: _fetch_paircode(new=True, push_display=True)).props(
                            'elevated color=blue-8'
                        ).classes('flex-1')

                # ── Paired Devices ─────────────────────────────────────────
                with ui.card().classes('w-full q-mb-sm'):
                    ui.label(T['pair_devices_title']).classes('text-subtitle1 text-bold q-mb-xs')
                    device_list = ui.column().classes('w-full')
                    def _refresh_devices():
                        device_list.clear()
                        d2 = _load_auth()
                        devs = d2.get('paired_devices', []) if d2 else []
                        if not devs:
                            with device_list:
                                ui.label(T['pair_no_devices']).classes('text-caption text-grey-5')
                            return
                        for dv in devs:
                            dt = datetime.fromtimestamp(dv['paired_at']).strftime('%Y-%m-%d %H:%M')
                            with device_list:
                                with ui.row().classes('w-full items-center justify-between'):
                                    ui.label(f"📱 {dv['name']}  ({dt})").classes('text-caption')
                                    def _revoke(t=dv['token']):
                                        d3 = _load_auth()
                                        if d3:
                                            d3['paired_devices'] = [x for x in d3.get('paired_devices', []) if x['token'] != t]
                                            _save_auth(d3)
                                        _refresh_devices()
                                    ui.button(icon='delete', on_click=_revoke).props('flat round dense color=negative')
                    _refresh_devices()
                    ui.separator()
                    invite_lbl = ui.label('').classes('text-caption text-blue-7 q-mt-xs break-all')
                    def _gen_invite():
                        it = secrets.token_urlsafe(16)
                        _invite_tokens[it] = _time.time() + 300
                        base = str(request.base_url).rstrip('/')
                        link = f'{base}/pair?token={it}'
                        invite_lbl.set_text(f'🔗 {link}  (valid 5 min)')
                        ui.clipboard.write(link)
                        ui.notify(T['pair_invite_copied'], type='positive')
                    ui.button(T['pair_invite_btn'], on_click=_gen_invite).props('outline color=green')

            # ── ZeroClaw › Characters ──────────────────────────────────────
            with ui.tab_panel(t_zc_char):
                _build_character_tab('/var/lib/zeroclaw/.zeroclaw/workspace', 'zeroclaw', 'blue-8')

            # ── ZeroClaw › Skills ─────────────────────────────────────────
            with ui.tab_panel(t_zc_skills):
                _build_skills_tab('/var/lib/zeroclaw/.zeroclaw/workspace', 'zeroclaw', 'blue-8')
    return zc_content
