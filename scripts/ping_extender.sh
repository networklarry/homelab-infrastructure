#!/bin/bash
# ping_extender.sh
# Continuously pings a WiFi extender's management IP and logs timestamped
# results, so blips can be cross-referenced against the secondary host's
# ping log to determine if outages are backhaul (extender-side) or
# client-side (radio-side).
#
# Usage:
#   ./ping_extender.sh
#   (runs in foreground - use nohup or a screen/tmux session to leave it
#   running overnight, see notes below)

EXTENDER_IP="REPLACE_WITH_EXTENDER_LAN_IP"
LOG_DIR="/var/log/grumpy-ping"
LOG_FILE="${LOG_DIR}/extender_ping_$(date +%Y%m%d).log"

mkdir -p "$LOG_DIR"

echo "[*] Pinging extender ${EXTENDER_IP}, logging to ${LOG_FILE}"
echo "[*] Ctrl-C to stop"

# -D prepends a unix epoch timestamp to each line from ping itself.
# We also prefix a human-readable timestamp so the log lines up visually
# with the existing ping log format used elsewhere in this project.
ping -D -i 1 "$EXTENDER_IP" | while IFS= read -r line; do
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "${ts} ${line}" >> "$LOG_FILE"
done
