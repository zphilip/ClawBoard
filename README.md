# ClawBoard

A mobile-friendly web dashboard for editing [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw) `config.toml` at runtime — built with [NiceGUI](https://nicegui.io).

## Features

- **10-tab layout** covering every section of `config.toml`:
  | Tab | Covers |
  |-----|--------|
  | 通用 | `api_key`, `default_provider` (dropdown), `default_model`, `default_temperature` |
  | Providers | Dynamic `[model_providers.*]` cards — add / remove any number of provider aliases |
  | 自主 | `[autonomy]` — level, risk controls, allowed commands, forbidden paths |
  | Agent | `[agent]`, `[observability]` — tool iterations, history, tracing |
  | 记忆 | `[memory]` — backend, hygiene, retention, embedding settings |
  | 通信 | `[gateway]`, `[tunnel]`, global `[channels_config]` |
  | Channels | Dynamic `[channels_config.*]` cards — add / remove from 18 channel types |
  | 安全 | `[security.resources]`, `[reliability]`, `[scheduler]` |
  | 功能 | `[web_fetch]`, `[web_search]`, `[browser]`, `[cost]` |
  | 系统 | `[transcription]`, `[heartbeat]`, `[cron]`, service log viewer |

- **Dynamic Providers tab** — each `[model_providers.<alias>]` entry gets its own card with a provider-id dropdown (37 known providers from the official reference), base_url override, `requires_openai_auth`, and per-provider `api_key`
- **Dynamic Channels tab** — supports all 18 channel types (Telegram, Discord, Slack, Mattermost, Matrix, Signal, WhatsApp, DingTalk, QQ, Lark/Feishu, Email, IRC, Webhook, Nostr, Nextcloud Talk, Linq, iMessage) with full per-channel field schemas
- **💾 Save** and **🔄 Save & Restart** buttons — writes `config.toml` and optionally restarts `zeroclaw.service` via `sudo systemctl`
- Fully mobile-friendly (Quasar/Material UI via NiceGUI)

## Requirements

```
pip install nicegui toml
```

## Usage

```bash
cd ClawBoard
python3 dashboard.py
```

Open `http://<host>:8080` in your browser (or phone).

## Config file location

The dashboard looks for `config.toml` in the same directory as `dashboard.py`, then falls back to the current working directory.

## Restart service

The **Save & Restart** button runs:

```bash
sudo systemctl restart zeroclaw.service
```

Make sure the user running the dashboard has passwordless sudo for that command, or run the dashboard as root.

## Security note

`config.toml` may contain sensitive credentials (API keys, channel secrets). The included `config.toml` uses ZeroClaw's `enc2:` encrypted key format for the global `api_key`. Do **not** commit plain-text secrets to a public repository.

## Related

- [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw) — the AI agent runtime this configures
- [NiceGUI docs](https://nicegui.io/documentation) — Python web UI framework used for this dashboard
- [ZeroClaw config reference](https://github.com/zeroclaw-labs/zeroclaw/blob/master/docs/reference/api/config-reference.md)
