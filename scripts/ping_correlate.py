#!/usr/bin/env python3
"""
ping_correlate.py

Pulls today's (or a specified) ping log from a secondary host via scp,
parses it alongside the primary host's local log, and reports which
outage windows are CORRELATED (both machines dropped around the same
time -> likely upstream/WAN or fiber issue) vs ISOLATED (only one
machine dropped -> likely local WiFi / extender issue on that machine).

Usage:
    python3 ping_correlate.py [YYYYMMDD]

If no date is given, defaults to today.

Requires: passwordless SSH (key-based auth) to the secondary host for scp
to run unattended. If you haven't set that up yet, run once manually and
enter the password when prompted -- scp will still work, it just won't
be silent.
"""

import subprocess
import sys
import re
import tempfile
import os
import time
from datetime import datetime, timedelta

# ---- Config -----------------------------------------------------------
# NOTE: real host/IP/port redacted for this public repo — replace with
# your own before running.
ASYLUM_LOG_DIR = "/var/log/asylum-ping"
GRUMPY_HOST = "user@REPLACE_WITH_HOST_TAILSCALE_IP"
GRUMPY_SSH_PORT = "REPLACE_WITH_SSH_PORT"
GRUMPY_LOG_DIR = "/var/log/grumpy-ping"
GRUMPY_SSH_KEY = os.path.expanduser("~/.ssh/grumpy_key")

# Outage windows within this many seconds of each other count as correlated
CORRELATION_WINDOW_SECONDS = 30

# Outage windows longer than this are treated as "machine was offline"
# (e.g. rebooting, WiFi fully dropped for hours) rather than a real
# ping-flap event, and are excluded from correlation analysis / reported
# separately so they don't swallow up unrelated events in the same span.
MAX_OUTAGE_SECONDS = 600  # 10 minutes

# A "window" needs at least this many consecutive DOWN pings before it's
# treated as a real outage. A single missed ping between two UP pings is
# normal jitter on any link (wired or WiFi) and just adds noise -- this
# filters that out before it ever reaches the report.
MIN_CONSECUTIVE_MISSES = 2

LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<status>UP|DOWN)\s+target=(?P<target>\S+)"
)


def write_prom_metrics(wifi_outages, wan_outages, output_path="/var/lib/node_exporter/textfile_collector/ping_correlate.prom"):
    """Write WiFi/WAN outage stats in Prometheus textfile format."""
    tmp_path = output_path + ".tmp"  # atomic write avoids partial reads by node_exporter
    lines = [
        "# HELP wifi_outage_count Number of correlated WiFi-only outages for the last run period",
        "# TYPE wifi_outage_count gauge",
        f"wifi_outage_count {wifi_outages}",
        "",
        "# HELP wan_outage_count Number of correlated WAN/upstream outages for the last run period",
        "# TYPE wan_outage_count gauge",
        f"wan_outage_count {wan_outages}",
        "",
        "# HELP ping_correlate_last_run Unix timestamp of the last successful run",
        "# TYPE ping_correlate_last_run gauge",
        f"ping_correlate_last_run {int(time.time())}",
        "",
    ]
    with open(tmp_path, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp_path, output_path)  # atomic rename


def date_str(arg_date=None):
    if arg_date:
        return arg_date
    return datetime.now().strftime("%Y%m%d")


def fetch_grumpy_log(datestr, local_path):
    remote_path = f"{GRUMPY_LOG_DIR}/ping_{datestr}.log"
    cmd = [
        "scp", "-P", GRUMPY_SSH_PORT,
        f"{GRUMPY_HOST}:{remote_path}", local_path,
    ]
    print(f"[*] Pulling secondary host log: {remote_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[!] scp failed:\n{result.stderr}")
        sys.exit(1)


def parse_log(path):
    """Return list of (datetime, status, target) tuples."""
    events = []
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = LOG_LINE_RE.match(line.strip())
            if not m:
                continue
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            events.append((ts, m.group("status"), m.group("target")))
    return events


def outage_windows(events):
    """
    Collapse DOWN events into contiguous outage windows (start, end, count).
    A window ends when an UP event follows a DOWN run. count is the number
    of consecutive DOWN pings that made up the window, used later to filter
    out single-ping blips.
    """
    windows = []
    current_start = None
    current_end = None
    current_count = 0

    for ts, status, target in sorted(events, key=lambda e: e[0]):
        if status == "DOWN":
            if current_start is None:
                current_start = ts
                current_count = 0
            current_end = ts
            current_count += 1
        else:  # UP
            if current_start is not None:
                windows.append((current_start, current_end, current_count))
                current_start = None
                current_end = None
                current_count = 0

    if current_start is not None:
        windows.append((current_start, current_end, current_count))

    return windows


def merge_close_windows(windows, gap_seconds=5):
    """Merge windows that are within gap_seconds of each other (same outage,
    just multiple targets timing out at slightly different moments).
    Preserves/sums the DOWN-ping count across merged windows."""
    if not windows:
        return []
    merged = [windows[0]]
    for start, end, count in windows[1:]:
        last_start, last_end, last_count = merged[-1]
        if (start - last_end).total_seconds() <= gap_seconds:
            merged[-1] = (last_start, max(last_end, end), last_count + count)
        else:
            merged.append((start, end, count))
    return merged


def filter_significant(windows, min_consecutive_misses=MIN_CONSECUTIVE_MISSES):
    """Drop windows that don't have enough consecutive DOWN pings to count
    as a real outage rather than a single dropped packet."""
    kept = [(s, e) for s, e, c in windows if c >= min_consecutive_misses]
    dropped = len(windows) - len(kept)
    return kept, dropped


def windows_overlap(w1, w2, tolerance_seconds):
    s1, e1 = w1
    s2, e2 = w2
    s1 -= timedelta(seconds=tolerance_seconds)
    e1 += timedelta(seconds=tolerance_seconds)
    return s1 <= e2 and s2 <= e1


def fmt(dt):
    return dt.strftime("%H:%M:%S")


def main():
    datestr = date_str(sys.argv[1] if len(sys.argv) > 1 else None)

    asylum_log = os.path.join(ASYLUM_LOG_DIR, f"ping_{datestr}.log")
    if not os.path.exists(asylum_log):
        print(f"[!] Local primary-host log not found: {asylum_log}")
        sys.exit(1)

    tmp_grumpy_log = os.path.join(tempfile.gettempdir(), f"grumpy_ping_{datestr}.log")
    fetch_grumpy_log(datestr, tmp_grumpy_log)

    asylum_events = parse_log(asylum_log)
    grumpy_events = parse_log(tmp_grumpy_log)

    if not asylum_events:
        print(f"[!] No parseable events in primary-host log: {asylum_log}")
        sys.exit(1)
    if not grumpy_events:
        print(f"[!] No parseable events in secondary-host log: {tmp_grumpy_log}")
        sys.exit(1)

    asylum_windows_raw = merge_close_windows(outage_windows(asylum_events))
    grumpy_windows_raw = merge_close_windows(outage_windows(grumpy_events))

    asylum_windows_all, asylum_blips = filter_significant(asylum_windows_raw)
    grumpy_windows_all, grumpy_blips = filter_significant(grumpy_windows_raw)

    def split_long(windows):
        short = [w for w in windows if (w[1] - w[0]).total_seconds() <= MAX_OUTAGE_SECONDS]
        long = [w for w in windows if (w[1] - w[0]).total_seconds() > MAX_OUTAGE_SECONDS]
        return short, long

    asylum_windows, asylum_offline = split_long(asylum_windows_all)
    grumpy_windows, grumpy_offline = split_long(grumpy_windows_all)

    print(f"\n===== Ping Correlation Report: {datestr} =====")
    print(f"Primary (wired) outage windows: {len(asylum_windows)}  "
          f"({len(asylum_offline)} excluded as offline, {asylum_blips} single-ping blips ignored)")
    print(f"Secondary (WiFi) outage windows: {len(grumpy_windows)}  "
          f"({len(grumpy_offline)} excluded as offline, {grumpy_blips} single-ping blips ignored)\n")

    if asylum_offline or grumpy_offline:
        print(f"---- MACHINE OFFLINE (outage > {MAX_OUTAGE_SECONDS // 60} min, excluded from correlation) ----")
        for w in asylum_offline:
            dur = (w[1] - w[0]).total_seconds() / 60
            print(f"  Primary offline {fmt(w[0])}-{fmt(w[1])}  ({dur:.0f} min)")
        for w in grumpy_offline:
            dur = (w[1] - w[0]).total_seconds() / 60
            print(f"  Secondary offline {fmt(w[0])}-{fmt(w[1])}  ({dur:.0f} min)")
        print()

    matched_grumpy = set()
    correlated = []

    for aw in asylum_windows:
        found = False
        for i, gw in enumerate(grumpy_windows):
            if windows_overlap(aw, gw, CORRELATION_WINDOW_SECONDS):
                correlated.append((aw, gw))
                matched_grumpy.add(i)
                found = True
        if not found:
            correlated.append((aw, None))

    isolated_grumpy = [gw for i, gw in enumerate(grumpy_windows) if i not in matched_grumpy]

    print("---- CORRELATED (both machines dropped -> likely upstream/WAN/fiber) ----")
    any_correlated = False
    for aw, gw in correlated:
        if gw is not None:
            any_correlated = True
            print(f"  Primary {fmt(aw[0])}-{fmt(aw[1])}  <->  Secondary {fmt(gw[0])}-{fmt(gw[1])}")
    if not any_correlated:
        print("  (none)")

    print("\n---- ISOLATED: Primary (wired) only dropped -> check wired path / gateway ----")
    isolated_asylum = [aw for aw, gw in correlated if gw is None]
    if isolated_asylum:
        for aw in isolated_asylum:
            print(f"  Primary {fmt(aw[0])}-{fmt(aw[1])}")
    else:
        print("  (none)")

    print("\n---- ISOLATED: Secondary (WiFi) only dropped -> likely WiFi/extender issue ----")
    if isolated_grumpy:
        for gw in isolated_grumpy:
            print(f"  Secondary {fmt(gw[0])}-{fmt(gw[1])}")
    else:
        print("  (none)")

    total = len(asylum_windows) + len(isolated_grumpy)
    corr_count = sum(1 for aw, gw in correlated if gw is not None)
    print(f"\n===== Summary =====")
    print(f"Correlated outages (likely WAN/fiber): {corr_count}")
    print(f"Wired-only outages: {len(isolated_asylum)}")
    print(f"WiFi-only outages: {len(isolated_grumpy)}")

    write_prom_metrics(wifi_outages=len(isolated_grumpy), wan_outages=corr_count)


if __name__ == "__main__":
    main()
