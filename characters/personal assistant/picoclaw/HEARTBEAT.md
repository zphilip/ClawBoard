# HEARTBEAT.md

# Keep this file empty (or with only comments) to skip heartbeat work.
# Add tasks below when you want 凌音 (Anya) to check something periodically.
#
# Examples:
# - 检查今日日历，提醒今日重要事项
# - 扫描 memory/todo.md，汇报过期或即将到期的任务
# - 检查重要联系人的未读消息
# - Check device status (e.g., MaixCam)

# Heartbeat Check List

This file contains tasks for the heartbeat service to check periodically.

## Examples

- Check for unread messages
- Review upcoming calendar events
- Check device status (e.g., MaixCam)

## Instructions

- Execute ALL tasks listed below. Do NOT skip any task.
- For simple tasks (e.g., report current time), respond directly.
- For complex tasks that may take time, use the spawn tool to create a subagent.
- The spawn tool is async - subagent results will be sent to the user automatically.
- After spawning a subagent, CONTINUE to process remaining tasks.
- Only respond with HEARTBEAT_OK when ALL tasks are done AND nothing needs attention.

---

Add your heartbeat tasks below this line:

## Check Mobile Phone Connection

- Run `adb devices` to check if any Android phone/device is currently connected
- If a device is connected (status `device`), remember the device serial and set a context flag: **PHONE_CONNECTED=true**
- If no device is connected, clear the flag: **PHONE_CONNECTED=false**
- Report connection status only if it changed since the last check (connected → disconnected or vice versa)

## Check ha-lite Smart Home Server

- Run `curl -s http://localhost:8090/api/health` to check ha-lite server status
- Parse the response: record `cloud_authed`, `oauth_authed`, `device_count`
- If `status` is not `"ok"` or the server is unreachable, report: "⚠️ ha-lite server unreachable — smart home control unavailable"
- If `cloud_authed: false` AND `oauth_authed: false`, report: "⚠️ ha-lite not authenticated — run token refresh fallback (see TOOLS.md)"
- If all healthy, update `MEMORY.md` smart home status section silently
- Report only if status changed since last check
