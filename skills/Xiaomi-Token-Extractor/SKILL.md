---
name: xiaomi-token-extractor
version: 1.9.0
description: "[English] Extract Xiaomi device tokens from Xiaomi Cloud using a QR code login flow. Retrieves IP addresses, tokens (32-char hex), models, and BLE keys for all devices across all homes. Saves results to local files for use by other skills like xiaomi-home. | [中文] 通过扫码登录小米账号，从小米云端提取所有设备的 IP、Token（32位十六进制）、型号及 BLE 密钥，并保存到本地文件供 xiaomi-home 等技能直接使用。"
metadata: {"clawdbot":{"emoji":"🔑","requires":{"bins":["python3"]},"install":[{"id":"pip-deps","kind":"exec","command":"pip3 install requests pycryptodome pillow colorama","label":"Install Python dependencies"}]}}
---

# Xiaomi Token Extractor 🔑 | 小米设备 Token 提取

[English] | [中文](#中文说明)

---

## English

Authenticate with Xiaomi Cloud via QR code and download the IP address, Token, and model info for every device in the user's Mi Home account. Results are saved to `references/devices.json` (structured) and `references/devices.md` (table for other skills).

### 🚀 How It Works

1. Agent runs `scripts/extract_tokens.py` — QR image is downloaded, a detached QR server starts, and a `SESSION_FILE` path is emitted. **Script exits in ~2 s** so the agent can show the QR while the Xiaomi session is still fresh.
2. Agent shows the `QR_IMAGE_URL` link (and optionally `QR_URL`) to the user. `QR_IMAGE_B64` is only rendered inline if the user explicitly asks for it.
3. User scans the QR code with the **Mi Home app** (Profile → top-right → Scan).
4. Agent runs `scripts/extract_tokens.py --collect SESSION_FILE` — long-polls until the scan is detected, then fetches all device tokens from the cloud.
5. Results are written to `references/devices.json` and `references/devices.md`.
6. Agent summarises the discovered devices for the user.

---

## 🤖 Agent Workflow — Step by Step

### Intent: "Get my Xiaomi device tokens" / "Find token for device X"

**Step 1 — Start Phase 1 (download QR, emit output, exit fast)**

Run the script. Always use `--server cn` unless the user is on a non-China account; for global accounts use the appropriate server or omit `--server` to scan all.

```bash
python3 scripts/extract_tokens.py --server cn
```

For a specific device filter (search by name substring):
```bash
python3 scripts/extract_tokens.py --server cn --filter "热水器"
```

To scan ALL servers (slower but finds every device):
```bash
python3 scripts/extract_tokens.py
```

> ⚠️ **The script ALWAYS exits in ~2 seconds (Phase 1) unless `--interactive` is passed.**  
> It does NOT long-poll. Capture `SESSION_FILE` and `QR_COLLECT_CMD` from the output — you will need them in Step 3.  
> **Do NOT pass `--interactive`** — that flag is only for direct terminal use and will cause the same "expired" issue by blocking for 120 s before returning output.

**Step 2 — Parse the output and tell the user to scan**

The script outputs structured lines. Look for these keys:

| Output Line | Meaning |
| :--- | :--- |
| `QR_SERVER=http://<ip>:<port>/qr/<token>` | Informational — the QR server address pattern. Use `QR_IMAGE_URL` instead |
| `QR_IMAGE_URL=http://192.168.1.x:31415/qr/a3f8...` | ⚠️ **Copy this FULL value exactly as-is** (including the `/qr/...` path) — this is the URL the user opens to see the QR image. **Never strip the path.** |
| `QR_SERVER_PID=<pid>` | PID of the detached QR server process. Stays alive up to 5 min after Phase 1 exits. Self-terminates automatically. |
| `QR_IMAGE_B64=<base64 PNG>` | The QR PNG as base64, downloaded directly from Xiaomi's server. **Do NOT render this inline by default.** Only decode and display it if the user explicitly asks to see the QR image. `QR_IMAGE_URL` is sufficient for normal use. |
| `QR_URL=https://account.xiaomi.com/...` | **Only emitted when pyzbar or zxingcpp is installed.** The actual URL decoded from inside the PNG. If absent, `QR_IMAGE_B64` alone is sufficient. |
| `QR_LOGIN_URL=https://account.xiaomi.com/...` | Browser-only fallback. **Do NOT use as a scan target.** Requires Mi Account cookies; scanning this URL with Mi Home reports "expired". |
| `SESSION_FILE=<path>` | ⚠️ **Path to the saved QR session state file. Required for Phase 2.** Pass this exact path to `--collect`. |
| `QR_COLLECT_CMD=python3 <script> --collect <path>` | ⚠️ **Exact command to run after showing the QR to the user.** Copy and run verbatim. This completes login and collects tokens. |
| `QR_RETRY attempt=N of=M` | QR expired before scan (interactive/TTY mode only); script auto-regenerated a fresh QR — show the new `QR_IMAGE_URL` to the user immediately |
| `STATUS=waiting_for_scan` | QR presented, waiting for user to scan |
| `STATUS=login_success` | User scanned and approved |
| `STATUS=login_timeout` | No scan within `--timeout` seconds; re-run from Step 1 for a fresh QR |
| `STATUS=login_failed reason=cannot_get_qr_url` | Could not reach Xiaomi login endpoint (network/firewall) |
| `STATUS=login_failed reason=cannot_download_qr_image` | QR image download failed |
| `STATUS=login_failed reason=qr_server_start_failed detail=…` | Port already in use **or** server process could not be spawned — retry with `--port 31416` |
| `STATUS=login_failed reason=untrusted_url detail=…` | Server returned a non-Xiaomi URL (possible MITM) |
| `STATUS=login_failed reason=network_error detail=…` | Network error during login URL fetch |
| `STATUS=login_failed reason=poll_error detail=…` | Network error while polling for scan confirmation |
| `STATUS=login_failed reason=cannot_get_service_token` | Location exchange failed after successful scan |
| `STATUS=login_failed reason=invalid_session_file detail=…` | `--collect` could not read the session file — re-run from Step 1 |
| `WARNING=local_ip_detection_failed …` | Could not detect LAN IP — pass `--host <ip>` to fix |
| `DEVICE={"name":…,"ip":…,"token":…,"model":…,"server":…}` | One device (JSON) |
| `DEVICES_SAVED=<path>` | Path to the saved JSON file |
| `DONE count=<n> json=<path> md=<path>` | All done |

**Display the QR link to the user:**

By default, show the user the **`QR_IMAGE_URL`** (a clickable link to the QR image served locally) and, if present, `QR_URL` (the decoded login URL). This is enough for the user to open the image on their phone and scan it with Mi Home.

Tell the user:
> "Open this link on your phone (must be on the same WiFi): **[exact QR_IMAGE_URL value]**  
> Then scan the QR with the **Mi Home app** (Profile → top-right menu → Scan). I'll detect it automatically."

If `QR_URL` is also present, you may show it as a secondary hint:
> "Alternatively, scan this URL directly with Mi Home: `[QR_URL value]`"

**Only show `QR_IMAGE_B64` if the user explicitly asks** (e.g. "show me the QR code", "display the image"). In that case decode it and render it as an inline image.

> ⚠️ **`QR_IMAGE_URL` contains a secret `/qr/<hex-token>` path — always show the FULL URL verbatim. Never truncate it to just the host and port.**

> ⚠️ **`QR_LOGIN_URL` is a browser-session URL — do NOT show it as the QR scan target and do NOT regenerate a QR image from it.** Mi Home will report "expired" if it scans a QR encoding `QR_LOGIN_URL`. The only correct QR to scan is the image at `QR_IMAGE_URL`.

**Step 3 — Run Phase 2 to complete login and collect tokens**

Once you have shown the QR to the user and asked them to scan it, run the **exact** `QR_COLLECT_CMD` value from Step 1 output using a **blocking / foreground exec** tool (NOT `spawn`, NOT a background task). You MUST wait for it to finish and capture its full output.

```bash
# Use the exact QR_COLLECT_CMD value from Step 1 output, e.g.:
python3 scripts/extract_tokens.py --collect /tmp/qr_session_xxxx.json
```

> ⚠️ **MUST use blocking exec (foreground). Do NOT use `spawn` or any background/async tool for this command.** If run in the background, the output is lost and the user never receives their device tokens.

This command blocks until one of:
- `STATUS=login_success` — scan detected; `DEVICE=` and `DONE` lines follow.
- `STATUS=login_timeout` — no scan within `--timeout` seconds. Re-run Step 1 for a fresh QR.
- `STATUS=login_failed reason=…` — hard error; check the reason.

> ⚠️ **Because the agent already showed the QR in Step 2, the user may have already scanned — Phase 2's long-poll will return success immediately if so.**
> If `STATUS=login_timeout` appears, the session expired before the user scanned. Re-run from Step 1 to get a fresh QR.

**Step 4 — Report devices to the user (MANDATORY)**

After `--collect` finishes, you MUST immediately reply to the user with the results. Do NOT stay silent.

Collect every `DEVICE=` line from the output (each is a JSON object). When `DONE` appears, present a summary table:

```
| Device Name | IP | Token | Model |
|---|---|---|---|
| 热水器 | 192.168.1.10 | abc123…ef | cuco.plug.v3 |
…
```

Also tell the user:
> "Found **N** device(s). Tokens saved to `references/devices.json` — the xiaomi-home skill can use these directly."

If `DEVICE=` lines are present but `DONE` is missing, still report what was collected.

If `count=0` or no `DEVICE=` lines appear, tell the user no devices were found and suggest trying without `--server` to scan all regions.

**Step 5 — Optional: filter a specific device**

If the user asked for a specific device, search the `DEVICE=` JSON lines for `name` containing the user's query (case-insensitive) and show just that entry.

---

## 🗄️ Output Files

| File | Format | Description |
| :--- | :--- | :--- |
| `references/devices.json` | JSON array | Full structured device list, one object per device |
| `references/devices.md` | Markdown table | Human/agent-readable, compatible with xiaomi-home |

### `devices.json` schema
```json
[
  {
    "server": "cn",
    "home_id": "12345678",
    "name": "热水器",
    "did": "12345678",
    "ip": "192.168.1.10",
    "token": "181f7c047098b594883f88191a9e6c3a",
    "mac": "AA:BB:CC:DD:EE:FF",
    "model": "cuco.plug.v3",
    "ble_key": null
  }
]
```

---

## ⚙️ Script Options

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--server SERVER` | *(all)* | Cloud server to query: `cn` `de` `us` `ru` `tw` `sg` `in` `i2` |
| `--filter TEXT` | *(all)* | Only show devices whose name contains TEXT (case-insensitive) |
| `--host IP` | auto-detected | Override host IP for QR server URL |
| `--port PORT` | `31415` | Port for QR image HTTP server |
| `--timeout SECS` | `120` | Max seconds to wait per QR scan attempt |
| `--retries N` | `2` | Re-generate QR up to N times if it expires before scan (interactive/TTY mode only) |
| `--output-dir DIR` | `references/` | Directory to save `devices.json` and `devices.md` |
| `--collect SESSION_FILE` | — | **Phase 2**: load saved session state and complete login + token collection |
| `--interactive` | — | **Terminal-only**: single-process blocking mode. **Never use from an agent tool** — causes QR to expire before output is returned. |

---

## ❗ Error Handling

| Error | Action |
| :--- | :--- |
| `QR_RETRY attempt=N of=M` | QR expired (interactive/TTY mode) — show the new `QR_IMAGE_URL` immediately; do NOT report failure |
| `STATUS=login_timeout` | Session expired; re-run Step 1 for a fresh QR |
| `STATUS=login_failed reason=cannot_get_qr_url` | Network/firewall issue reaching Xiaomi; check connectivity and retry |
| `STATUS=login_failed reason=cannot_download_qr_image` | QR image download failed; check connectivity and retry |
| `STATUS=login_failed reason=qr_server_start_failed detail=…` | Port in use; re-run with `--port 31416` (or any free port) |
| `STATUS=login_failed reason=untrusted_url` | Possible MITM or API change; check network and retry |
| `STATUS=login_failed reason=network_error` | Network error fetching login URL; check connectivity |
| `STATUS=login_failed reason=poll_error` | Network error while waiting for scan; retry |
| `STATUS=login_failed reason=cannot_get_service_token` | Session exchange failed; try again |
| `STATUS=login_failed reason=invalid_session_file` | Session file missing or corrupt; re-run from Step 1 |
| `WARNING=local_ip_detection_failed` | Add `--host <device-ip>` so the QR server URL is reachable |
| "QR code expired" when visiting `QR_URL`/`QR_LOGIN_URL` | Expected — these URLs require Mi Account browser cookies. Tell user to **scan the QR image** instead |
| Missing token (`token=""`) | Device is offline or on a different LAN segment; token still stored |
| No devices found | Wrong server; try without `--server` flag to scan all |
| `pycryptodome` not found | Run `pip3 install pycryptodome` |

---

## 中文说明

通过扫描二维码登录小米账号，从云端提取所有设备的 IP 地址、Token（32位十六进制密钥）、型号等信息，无需手动输入密码。

### 🚀 核心功能
- **二维码免密登录**：生成登录二维码，用米家 App 扫一扫完成授权
- **全量设备提取**：自动遍历所有家庭、所有设备，含蓝牙 BLE 设备密钥
- **自动存档**：结果保存至 `references/devices.json` 和 `references/devices.md`，可直接被 `xiaomi-home` 技能使用
- **模糊过滤**：支持按设备名称关键词筛选

### 🤖 自然语言指令

| 用户说 | Agent 动作 |
| :--- | :--- |
| "帮我获取小米设备的 token" | 运行 `extract_tokens.py --server cn` |
| "找一下热水器的 token" | 运行 `extract_tokens.py --server cn --filter 热水器` |
| "刷新一下设备列表" | 重新运行脚本，覆盖旧文件 |
| "把 token 存好" | 已自动保存，告知用户路径 |

---

## 🔗 Links | 相关链接
- **Original extractor**: [https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)
- **xiaomi-home skill**: companion skill for local device control using the extracted tokens
