# TS-MON v2 — Tailscale Network Monitor

Copyright (c) 2026 Zachary V. Martin, CC

Real-time terminal dashboard for monitoring Tailscale tailnet nodes, latency (`ping`), live `tx`/`rx` throughput and DERP-vs-direct routing hints via Tailscale's local Prometheus `/metrics` endpoint (`:5252`), and online/offline state changes using `rich`.

## Quick Setup

1. Clone the repository:
   ```bash
   git clone [https://github.com/ZvMartin/tailscale-network-monitor.git](https://github.com/ZvMartin/tailscale-network-monitor.git)
   cd tailscale-network-monitor
   ```
2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Edit the credential block at the top of `tailmon.py`:
   - `API_KEY` — Tailscale API access token (Admin Console → Settings → Keys)
   - `TAILNET` — Your tailnet name (e.g., `example.ts.net` or your tailnet identifier)
4. Run the monitor:
   ```bash
   python3 tailmon.py
   ```

## Notes
- Requires read access to the Tailscale API and optional reachability to port `5252` on nodes exposing `/metrics`.
- Designed to integrate cleanly into terminal dashboards (such as `ttyd` + Homepage) or run standalone in any terminal.
