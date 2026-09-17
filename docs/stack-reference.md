# Stack Reference (Sanitized)

> Adapted from an internal versioned stack-reference doc I keep for myself, updated after significant changes. All IPs, ports exposed only within the VPN, and credentials below are placeholders.

## Hosts

| Host | Role | OS / Hardware |
|---|---|---|
| Primary | Runs the full monitoring/automation/security stack | Ubuntu 25.10, x86_64 SFF desktop, 16 GB RAM |
| Secondary | Docker host for the Heimdall dashboard | Ubuntu (laptop-class hardware) |
| Edge node | Media server | Raspberry Pi 5, ARM |
| Workstation | Monitored via Windows Exporter, no services hosted | Windows |

## Network Layer

| Service | Purpose | Notes |
|---|---|---|
| Tailscale | Mesh VPN — all inter-host and remote access | MagicDNS enabled; all sensitive ports scoped to this interface only |
| dnsmasq | Internal split-DNS | Resolves an internal domain suffix to the primary host, only for VPN clients — LAN-only devices intentionally do *not* get this resolution, keeping internal services gated to approved devices |
| Nginx | Reverse proxy in front of all internal web services | HTTP/1.1 + explicit `Connection` header handling to avoid chunked-encoding 502s; HSTS header stripped where it was forcing an HTTPS upgrade to a port nothing listens on |
| UFW | Firewall | Sensitive service ports restricted to VPN interface or LAN only; public SSH access removed |

## Observability Layer

| Service | Purpose |
|---|---|
| Prometheus | Time-series metrics scraping |
| Node Exporter / Blackbox Exporter | Host metrics + endpoint uptime probing |
| Grafana | Dashboards — including a dedicated kiosk-mode display for at-a-glance status |
| Alertmanager | Alert routing |

## Security Layer

| Service | Purpose |
|---|---|
| Suricata | Network intrusion detection |
| Fail2Ban | Brute-force / repeated-failure ban enforcement |
| UFW | Default-deny firewall posture, VPN-scoped exposure |

## Automation Layer

| Service | Purpose |
|---|---|
| n8n | Event-driven workflow automation — 4 active workflows (alert approval/triage, nightly health digest, automated patching, disk-space cleanup) |
| Ollama (Llama 3.1) | Local LLM inference, used by the alert-approval workflow to auto-triage incoming alerts before they page a human |
| Flask alert agent | Receives webhook alerts, executes an allow-listed set of remediation commands, bridges to a Telegram bot for notifications |

## Known Issues Resolved

- **Boot-order race condition** — reverse proxy and DNS resolver both failed intermittently to bind to the VPN interface IP on boot, because `systemd` started them before the VPN client had assigned the interface an address. Fixed with an `ExecStartPre` delay — see [`configs/systemd/`](../configs/systemd/).
- **Upstream WAN instability** — diagnosed and resolved; see the root README's "Problems I Solved" section and [`scripts/ping_correlate.py`](../scripts/ping_correlate.py).
- **Silent workflow/config drift** — a couple of prior fixes (a data-formatting bug in the health-digest workflow, a stale monitoring target) were found to have reverted to their pre-fix state on their own after a period of time. Root cause not yet confirmed; re-applied and flagged for monitoring. Worth documenting here as an open item, not just a footnote — it's a reminder that "fixed" isn't always "stays fixed" in a system with multiple moving automation pieces.

## RAM Budget (approximate, full load)

| Service | RAM |
|---|---|
| Local LLM inference | ~5 GB |
| OS base | ~1.2 GB |
| n8n | ~400 MB |
| Prometheus + exporters | ~350 MB |
| Grafana | ~150 MB |
| Flask agents | ~130 MB |
| Alertmanager | ~50 MB |
| Nginx + VPN client | ~80 MB |
| **Total** | **~7.4 GB** (well under half of available RAM) |
