# Postmortem: Intermittent WAN Outages (August 2026)

**Status:** Resolved
**Duration:** ~2 weeks of intermittent impact, ~4 days of active diagnosis
**Impact:** Recurring internet drops of a few seconds to a few minutes, at unpredictable times, affecting all devices on the network

## Summary

Following a suspected lightning strike, the home network began experiencing intermittent outages — sometimes multiple times a day, sometimes going hours without issue. The obvious suspects (fiber line, ONT, WiFi) were investigated first and ruled out one at a time using real data rather than guesswork. The actual root cause turned out to be the ISP-provided router/modem itself, which was replaced, fully resolving the issue.

The key tool that made this possible: a custom Python script (`ping_correlate.py`) that continuously logs ping results from two independent hosts — one wired, one WiFi — and classifies every outage as either **correlated** (both hosts dropped at once → points upstream, to the WAN/ISP side) or **isolated** (only one host dropped → points local, to WiFi/wiring on that host specifically). That distinction is what turned "the internet keeps cutting out" from a guessing game into a data-driven investigation.

## Timeline

| Date | Event |
|---|---|
| — | Suspected lightning strike; intermittent outages begin |
| — | `ping_correlate.py` built and deployed, running nightly via cron, producing daily correlation reports |
| — | Initial data: **30 correlated outages** (upstream/WAN pattern) vs. only **9 real isolated WiFi events** — pointing away from the WiFi network and toward the WAN/fiber path |
| — | Cox outage occurs; Cox dispatches a tech and replaces the ONT |
| — | Two field techs verify the physical line is clean — one tests the outside run from the house to the junction, a second checks the box at the road — both confirm good signal |
| +2 days | `ping_correlate.py` run: still **11 correlated outages, 0 wired-only** — the ONT replacement did **not** resolve the issue. Physical layer was clean; the fault was elsewhere. |
| same day | New Cox-provided fiber modem/router installed (separate from, and in addition to, the earlier ONT swap) |
| +1 day | `ping_correlate.py` run: **0 correlated, 0 wired-only, 1 minor WiFi-only blip** — the correlated/upstream pattern is gone |
| +2 days | Confirmed holding: 0 correlated, 0 wired-only. WiFi-only blip count did rebound to 11 that day, clustered 11am–12:51pm — but critically, *no one is home 8am–4:30pm on weekdays*, ruling out household device load as the cause of that clustering |
| +3 days | 62 single-ping WiFi blips logged, but all below the significance threshold (a single dropped ping isn't a real outage) — correctly filtered out by the script's own noise-reduction logic rather than inflating the report |

## Root Cause

The ISP-provided fiber modem/router itself was flaky — not the ONT, not the fiber line, not in-home wiring. This only became clear because the ONT swap and the physical-layer verification happened *first*, and the data showed the correlated-outage pattern **persisted through both** of those fixes. That ruled them out definitively rather than leaving lingering doubt. Replacing the router/modem was the actual fix.

## How the Diagnosis Actually Worked

The core insight: an outage that hits **two independent machines on two different connection types at the exact same moment** can't be a coincidence of local WiFi flakiness — it has to be something both machines share, which is the path upstream of both of them (the router, or beyond it). An outage that hits **only one machine** is, by the same logic, local to that machine's link.

`ping_correlate.py` operationalizes that:
1. Both hosts continuously ping a mix of targets (local gateway, a known-stable LAN device, and public DNS resolvers like 8.8.8.8/1.1.1.1) and log UP/DOWN status with timestamps.
2. Consecutive DOWN pings get collapsed into "outage windows," with a minimum-consecutive-miss threshold to filter out single dropped packets (normal jitter on any link, not a real event).
3. Windows from both hosts are compared — if they overlap within a small time tolerance, they're correlated; if not, each is isolated to whichever host saw it.
4. Results are printed to a daily report **and** written out as Prometheus metrics, so the diagnosis feeds directly into the existing Grafana dashboards instead of living in a one-off script output.

That third point mattered in practice — it's what made the "0 correlated outages, holding steady" confirmation after the router swap something that could be watched over multiple days on a dashboard, not something that had to be manually re-checked by re-running a script.

Full source: [`scripts/ping_correlate.py`](../scripts/ping_correlate.py). The companion tool [`scripts/ping_extender.sh`](../scripts/ping_extender.sh) does the same correlation logic one layer deeper — pinging the WiFi extender's own management IP directly, to tell backhaul (extender-to-router) issues apart from client-radio issues once the WAN-level problem was out of the picture.

## What Went Well

- Built the right diagnostic tool *before* escalating to the ISP, rather than describing a vague "internet keeps cutting out" complaint — every conversation with Cox techs was backed by specific timestamps and a clear correlated-vs-isolated breakdown.
- Didn't stop investigating after the first fix (the ONT swap) looked plausible — kept collecting data afterward, which is exactly what caught that the real problem was still there.
- The significance threshold (ignoring single-ping blips) kept the daily reports meaningful instead of drowning in noise from normal network jitter.

## What Could Be Better Next Time

- The WiFi-only blip clustering around 11am–12:51pm was never fully root-caused — it's below the threshold that matters for the WAN investigation, but it's a loose thread. Worth a future pass once other priorities clear, possibly tied to a neighboring network's scheduled activity or a periodic firmware task on the extender itself.
- A 5GHz USB WiFi adapter for the secondary host is still a queued upgrade — the onboard card being 2.4GHz-only limits how precisely WiFi-side issues can be isolated from backhaul issues.

## Related

- [`scripts/ping_correlate.py`](../scripts/ping_correlate.py) — the core diagnostic tool
- [`scripts/ping_extender.sh`](../scripts/ping_extender.sh) — extender-specific follow-up diagnostic
- [`docs/stack-reference.md`](stack-reference.md) — full service inventory, including how these feed into Prometheus/Grafana
