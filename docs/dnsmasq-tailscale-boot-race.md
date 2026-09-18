# Fixing a dnsmasq Boot Race Condition on Tailscale Split-DNS

**TL;DR:** dnsmasq configured with `bind-interfaces` + a fixed `listen-address` can lose a race against Tailscale's interface coming up on boot, causing the DNS service to fail outright rather than degrade gracefully. A blind `sleep N` in the systemd unit is not a reliable fix. Switching to `bind-dynamic` + `interface=<name>` binds to the interface itself instead of a specific IP, which sidesteps the race entirely — and as a side effect, fixed an unintended LAN-side DNS exposure.

## Environment

- Host: Ubuntu 25.10, dnsmasq 2.91, systemd
- Tailscale providing split-DNS for a `.asylum` internal domain, MagicDNS enabled tailnet-wide
- dnsmasq's job: answer `*.asylum` queries authoritatively, forward everything else upstream to ISP DNS
- Config previously used `listen-address=<LAN IP>,<Tailscale IP>` + `bind-interfaces`

## Symptom

A separate device on the tailnet reported, via `tailscale status`:

```
Health check:
    - Tailscale can't reach the configured DNS servers. Internet connectivity may be affected.
```

`.asylum` domain resolution was broken tailnet-wide. On the DNS host itself:

```
$ systemctl status dnsmasq
× dnsmasq.service - dnsmasq - A lightweight DHCP and caching DNS server
     Active: failed (Result: exit-code) since Wed 2026-09-16 15:57:13 CDT; 1 day 21h ago
```

## Root cause

```
$ journalctl -u dnsmasq -b --no-pager
dnsmasq: failed to create listening socket for 100.105.153.99: Cannot assign requested address
dnsmasq: FAILED to start up
```

dnsmasq was configured to bind to a specific IP address (the Tailscale interface's address) via `listen-address`. On boot, systemd started dnsmasq before `tailscaled` had actually assigned that IP to the interface — so the address didn't exist yet, and the bind failed with `EADDRNOTAVAIL`. Because dnsmasq treats a failed bind as fatal at startup (rather than retrying), the unit exited and stayed dead until manually restarted.

This is a classic startup-ordering race: `After=tailscaled.service` in a systemd unit only guarantees that the *tailscaled process* has started, not that it has finished bringing the interface up and assigning addresses. Those are asynchronous from systemd's point of view.

### The band-aid: a fixed sleep

An earlier fix for the same failure mode on this host (which had previously hit the identical race with Nginx) was a systemd override:

```ini
[Service]
ExecStartPre=/bin/sleep 5
```

This worked for a while, but a fixed sleep is a guess, not a guarantee — boot timing varies (disk I/O contention, other services competing for startup, etc.), so `sleep 5` isn't always long enough. Eventually the timing lost again and dnsmasq failed the same way.

## The real fix

Two changes, addressing the same root cause from different angles:

### 1. Poll instead of guessing

Replace the fixed sleep with a loop that actually checks whether the interface is ready, up to a timeout:

```ini
[Service]
ExecStartPre=
ExecStartPre=/usr/share/dnsmasq/systemd-helper checkconfig
ExecStartPre=/bin/sh -c 'for i in $(seq 1 30); do ip addr show tailscale0 | grep -q "100.105.153.99" && exit 0; sleep 1; done; exit 1'
```

(The empty `ExecStartPre=` first clears the directives inherited from the base unit, since `systemctl edit` appends by default — otherwise you'd end up running both the old sleep and the new check.)

This buys time reliably instead of hoping 5 seconds is enough, and it fails fast if the interface genuinely never comes up rather than starting dnsmasq into a broken state.

### 2. Stop binding to a specific IP address at all

The deeper fix: don't ask dnsmasq to bind an address that might not exist yet — ask it to bind an *interface* instead, and let it pick up whatever addresses eventually land on that interface.

**Before** (`/etc/dnsmasq.conf`):
```
listen-address=192.168.0.180,100.105.153.99
bind-interfaces
```

**After:**
```
bind-dynamic
interface=tailscale0
```

`bind-dynamic` binds by interface name and re-binds automatically if the interface's address changes or the interface goes down and comes back — it doesn't need the address to exist at process start. This makes the poll-loop workaround largely redundant (a safety net rather than load-bearing), and is the more correct fix for any service sitting on top of an interface whose IP can appear late or change (VPN interfaces, DHCP interfaces, etc.).

Validate before restarting:
```
$ sudo dnsmasq --test
dnsmasq: syntax check OK.
```

### Bonus finding: unintended LAN exposure

The original config's `bind-interfaces` had also been quietly listening on the LAN IP (`192.168.0.180`), not just the Tailscale IP — dnsmasq logged this explicitly on every start:

```
LOUD WARNING: listening on 100.105.153.99 may accept requests via interfaces other than tailscale0
LOUD WARNING: use --bind-dynamic rather than --bind-interfaces
```

The intent had always been tailnet-only resolution for `.asylum` (deliberately not exposed to LAN-only clients). `bind-interfaces` with an IP-based `listen-address` doesn't actually restrict *which interface* can reach that IP — it's weaker isolation than it looks like. Switching to `interface=tailscale0` fixed this as a side effect: dnsmasq now only listens where explicitly told, full stop.

## Verification

1. **Config owner check** — confirm which process actually holds the ports:
   ```
   $ sudo ss -tulnp | grep :53
   udp  UNCONN  100.105.153.99:53  dnsmasq
   udp  UNCONN  127.0.0.1:53       dnsmasq   # loopback bind is default/expected, harmless
   ...
   ```
   No `192.168.0.180:53` listener present — confirms LAN exposure is gone.

2. **Negative test from the LAN side:**
   ```
   $ nslookup the-asylum.asylum 192.168.0.180
   ;; communications error to 192.168.0.180#53: connection refused
   ```
   Confirms it's genuinely unreachable via LAN now, not just unadvertised.

3. **Positive test from the tailnet side:**
   ```
   $ Resolve-DnsName the-asylum.asylum -Server 100.105.153.99
   Name                Type  IPAddress
   the-asylum.asylum   A     100.105.153.99
   ```

4. **Reboot test** — the real proof the race is fixed at the root:
   ```
   $ sudo reboot
   ...
   $ journalctl -u dnsmasq -b --no-pager | head -20
   dnsmasq: started, version 2.91 cachesize 150
   dnsmasq: using nameserver 68.105.28.11#53
   dnsmasq: using nameserver 68.105.29.11#53
   ```
   Clean start, no `Cannot assign requested address` error.

5. **Stale health-check state** — after the fix, the remote device's `tailscale status` *still* showed the DNS health warning even though direct TCP/DNS tests to the resolver succeeded from that device. This turned out to be a cached health status rather than a live re-check:
   ```
   tailscale up --reset
   ```
   cleared it, and the warning was gone from `tailscale status` on the next check. Worth knowing: Tailscale's health check output isn't necessarily re-evaluated on every `status` call, so a fix can be real before the CLI reflects it.

## Lessons learned

- **`After=` in systemd is about process start, not readiness.** If a service depends on a dynamic resource (an IP on an interface, a mounted volume, a socket another service creates), a plain `After=`/`Wants=` ordering dependency doesn't guarantee that resource exists yet.
- **A fixed sleep is a temporary patch, not a fix.** It encodes an assumption about timing that will eventually be wrong. A polling loop with a timeout is more robust and fails predictably rather than silently.
- **Prefer binding by interface over binding by IP** for any service sitting on an interface whose address can appear late, change, or disappear (VPN tunnels, DHCP, failover interfaces). `bind-dynamic` (dnsmasq) or equivalent options in other daemons exist for exactly this reason.
- **A security-by-obscurity assumption should be verified, not just intended.** The `.asylum` domain was meant to be tailnet-only from day one — turns out the config had quietly allowed LAN clients to resolve it too. The bug that broke DNS also surfaced a config accuracy problem that had nothing to do with the original symptom.
- **Client-side status output can lag reality.** Don't fully trust a cached health indicator after a server-side fix — force a refresh (`tailscale up --reset`, or equivalent) before concluding the fix didn't work.

## Files changed

- `/etc/dnsmasq.conf` — replaced `listen-address`/`bind-interfaces` with `bind-dynamic`/`interface=tailscale0`
- `/etc/systemd/system/dnsmasq.service.d/override.conf` — replaced fixed `sleep 5` with a poll loop (kept as a defense-in-depth backstop, no longer strictly necessary)
