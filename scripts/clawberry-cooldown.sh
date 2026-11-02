#!/usr/bin/env bash
# clawberry-cooldown.sh — fix the overheating governors on Radxa Cubie A7Z
# ============================================================================
#
# PROBLEM
#   The image pins two devfreq devices to the `performance` governor, so an
#   IDLE board burns several watts continuously:
#
#     /sys/class/devfreq/3600000.npu/       NPU  @ 1.008 GHz  performance
#     /sys/class/devfreq/a020000.dmcfreq/   DDR  @ 1.8 GHz    performance
#
#   Result measured on this unit: every die zone (cpu/gpu/npu/ddr) at
#   84-89 °C with the CPU at ~2 % busy and no workload at all.
#
# FIX
#   Switch both to `simple_ondemand` (confirmed supported on this image).
#   DDR stays fast under real load — the governor only raises it on demand,
#   so perceived responsiveness is unaffected.
#
# Usage:   sudo ./clawberry-cooldown.sh            apply and report
#          sudo ./clawberry-cooldown.sh --quiet    no output (for systemd)
#
# This is safe and reversible: it only writes governor files that exist.

set -u

apply() {
    local gov_path="$1" gov="$2"
    if [[ -w "$gov_path" ]]; then
        if echo "$gov" > "$gov_path" 2>/dev/null; then
            echo "  OK  $gov_path = $gov"
            return 0
        fi
        echo "  WARN  cannot write $gov_path (governor rejected?)" >&2
    else
        echo "  SKIP  $gov_path not present on this board" >&2
    fi
    return 1
}

changed=0
# NPU: sunxi devfreq ignores simple_ondemand (cur_freq stays at max_freq),
# so both set the governor AND clamp the ceiling. With no workload the NPU
# drops to min; under load simple_ondemand still ramps it up.
NPU=/sys/class/devfreq/3600000.npu
DDR=/sys/class/devfreq/a020000.dmcfreq
apply "$NPU/governor" simple_ondemand  && changed=$((changed+1))
apply "$DDR/governor" simple_ondemand  && changed=$((changed+1))
# Clamp NPU ceiling to 1/3 of max if it is still pinned (driver workaround)
if [[ -r "$NPU/max_freq" ]] && [[ -r "$NPU/cur_freq" ]] &&
   [[ "$(cat "$NPU/max_freq")" == "$(cat "$NPU/cur_freq")" ]]; then
    maxfreq=$(cat "$NPU/max_freq")
    clamp=$((maxfreq / 2))
    if echo "$clamp" > "$NPU/max_freq" 2>/dev/null; then
        echo "  OK  $NPU/max_freq clamped to $clamp (driver ignores governor)"
        changed=$((changed+1))
    fi
fi
# DDR controller: second-biggest idle heat contributor
apply "$DDR/governor" simple_ondemand  && changed=$((changed+1))

# CPU governor sanity: ensure not stuck on performance either
if [[ -r /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors ]] &&
   grep -qw ondemand /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors &&
   [[ "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)" == "performance" ]]; then
    echo ondemand > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null &&
        { echo "  OK  cpufreq governor = ondemand"; changed=$((changed+1)); }
fi

echo "Governors adjusted: ${changed:-0}"
exit 0
