#!/usr/bin/env bash
# whos-hot.sh — find which service/process/thread is hogging the CPU (heat culprit)
#
# Usage:   sudo ./whos-hot.sh [seconds]        (default 30)
# Sample every second, aggregate %CPU, and at the end print:
#   1. top processes   (by name, sustained)
#   2. top threads     (inside the worst processes)
#   3. the systemd unit each hot PID belongs to
#
# No arguments beyond duration. Read-only — changes nothing.

set -u
SECS="${1:-30}"
OUT="/tmp/whos-hot.$$"
trap 'rm -f "$OUT" "$OUT.procs" "$OUT.churn"' EXIT

# names belonging to the sampler itself — exclude from results
SKIP='^(ps|sudo|whos-hot|head|tail|awk|sort|grep|bash|sleep)$'

echo "Sampling CPU usage for ${SECS}s ... (idle it is fine, do not press Ctrl-C)"

# ── sample loop ──────────────────────────────────────────────────────────────
prev_pids=0; first_run=1; churn_max=0
end=$(( $(date +%s) + SECS ))
while (( $(date +%s) < end )); do
    # per-process instantaneous CPU%
    ps -eo pid,comm,%cpu --sort=-%cpu | tail -n +2 | head -20 >> "$OUT.procs"
    echo "---" >> "$OUT.procs"

    # process-count churn: detects spawn/die loops too fast to show in %CPU
    n=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)
    if (( first_run )); then
        prev_pids=$n; first_run=0
    else
        d=$(( n - prev_pids )); prev_pids=$n
        (( d < 0 )) && d=0
        echo "$d" >> "$OUT.churn"
        (( d > churn_max )) && churn_max=$d
    fi
    sleep 1
done

# ── aggregate: average %CPU per process name across all samples ──────────────
echo
echo "════════ TOP CPU PROCESSES (average %CPU over ${SECS}s) ════════"
awk -v skip="$SKIP" '/^---$/{next} {sum[$2]+=$3; cnt[$2]++}
     END{for (c in sum) if (c !~ skip) printf "%8.1f %%  %s\n", sum[c]/cnt[c], c}' "$OUT.procs" \
    | sort -rn | head -15

# ── spawn/die churn: restart loops and subprocess-per-second patterns ────────
if [[ -s "$OUT.churn" ]]; then
    total=$(awk '{s+=$1} END{print s+0}' "$OUT.churn")
    peak=$churn_max
    echo
    echo "════════ PROCESS-SPAWN CHURN (fast spawn/die loops) ════════"
    echo "  total new processes in ${SECS}s : $total"
    echo "  max new in a single second     : $peak"
    if (( total > 50 )); then
        echo "  ⚠ HIGH churn — a restart loop or subprocess-per-second poller is running!"
        echo "    watch it live with:   journalctl -f   or   watch -n1 'ps -e | wc -l'"
        echo
        echo "  Currently-alive duplicated python/sh processes (loop evidence):"
        ps -eo comm | sort | uniq -c | sort -rn | head -8 | sed 's/^/    /'
    fi
fi

# ── blocked (D-state) tasks: load grows while CPU idles — these are why ──────
echo
echo "════════ BLOCKED (D-state) TASKS ════════"
dstate=$(ps -eo pid,stat,comm | awk '$2 ~ /^D/ {printf "  pid %-8s %s  %s\n", $1, $2, $3}')
if [[ -z "$dstate" ]]; then
    echo "  none right now (re-run if load average keeps climbing)"
else
    echo "$dstate"
    echo "  → processes waiting on disk/GPIO/hardware. Check: sudo dmesg | tail -20"
fi

# ── top 5 processes right now, with PID + systemd unit ───────────────────────
echo
echo "════════ HOT PIDs RIGHT NOW (+ systemd unit) ════════"
ps -eo pid,%cpu,comm --sort=-%cpu | tail -n +2 | head -5 |
while read -r pid cpu comm; do
    unit=$(grep -o '[a-zA-Z0-9@._-]*\.service' "/proc/$pid/cgroup" 2>/dev/null | head -1)
    unit=${unit:-"(no unit / user process)"}
    printf '  pid %-7s %6.1f %%  %-20s %s\n' "$pid" "$cpu" "$comm" "$unit"
done

# ── threads inside the top-3 processes ───────────────────────────────────────
echo
echo "════════ TOP THREADS (inside hottest processes) ════════"
for pid in $(ps -eo pid,%cpu --sort=-%cpu | tail -n +2 | head -3 | awk '{print $1}'); do
    name=$(ps -p "$pid" -o comm= 2>/dev/null)
    echo "  --- PID $pid ($name) hottest threads ---"
    ps -L -p "$pid" -o tid,%cpu,comm --sort=-%cpu 2>/dev/null | tail -n +2 | head -5 |
        awk '{printf "    tid %-8s %6.1f %%  %s\n", $1, $2, $3}'
done

echo
echo "Tip: run again after boot, and compare with:  systemctl list-units --state=running"
