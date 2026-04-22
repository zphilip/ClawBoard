#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPATIBLE=""

if [[ -r /proc/device-tree/compatible ]]; then
    COMPATIBLE="$(tr '\000' ' ' < /proc/device-tree/compatible | tr '[:upper:]' '[:lower:]' | xargs echo -n)"
fi

if [[ "$COMPATIBLE" == *"radxa,cubie-a7z"* || "$COMPATIBLE" == *"radxa"* || "$COMPATIBLE" == *"sun60iw2p1"* ]]; then
    echo "Radxa board detected (${COMPATIBLE}) — delegating to wifi-connect-gpio-launch-radxa.py"
    exec python3 "$HERE/wifi-connect-gpio-launch-radxa.py" "$@"
fi

exec python3 "$HERE/wifi-connect-gpio-launch-rpi.py" "$@"
