# n8n Workflows

Four workflows run in production on the primary host, handling alerting, health reporting, patching, and cleanup without manual intervention. Raw JSON exports are omitted here since they embed internal hostnames/credentials by default — this doc covers the design instead, which is the more interesting/portable part anyway.

## 1. Alert Approval / Triage

Incoming alerts (from Prometheus Alertmanager) hit an n8n webhook. Instead of immediately paging, the workflow passes the alert to a **locally-hosted LLM (Ollama, Llama 3.1)** for a first-pass triage decision — is this actionable, or noise? Only alerts the model flags as worth a human's attention get pushed to Telegram for approval before any remediation action runs.

**Why it matters:** cuts alert fatigue without losing signal, and keeps the triage step local — no alert content leaves the network to a third-party API.

## 2. Nightly Health Digest

Runs on a nightly cron trigger. Pulls current-state queries from Prometheus (device up/down status, disk usage, memory) and formats them into a single digest message sent to Telegram each morning.

**Design note:** an early version of this workflow had a subtle bug — it was reading the wrong nested field from Prometheus's response shape, and results from earlier steps in the chain were getting silently dropped by the time the final formatting step ran, so most of the digest came back blank except for whichever metric happened to run last. Fixed by referencing each upstream node's output explicitly by name instead of relying on implicit pass-through. A good reminder that a workflow builder's "just chain the nodes" simplicity can still hide real data-flow bugs.

## 3. Automated Patching

Scheduled `apt` update/upgrade cycle across managed hosts, with results reported back through the same alerting channel.

## 4. Disk Warning Cleanup

Triggered on a disk-usage threshold alert; runs a scoped cleanup routine and reports what was reclaimed.

---

*If you're evaluating this repo: the workflows themselves are standard n8n JSON (trigger → HTTP/Prometheus query nodes → code/formatting node → notification node). The interesting engineering is in the failure modes above and the fact that the pipeline makes its own triage decisions rather than just forwarding every alert.*
