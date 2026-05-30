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
