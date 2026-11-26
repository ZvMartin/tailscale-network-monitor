#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║      TAILSCALE NETWORK MONITOR  v2               ║
║  Copyright (c) 2026 Zachary V. Martin, CC        ║
║  Plug in your credentials in the two lines below ║
╚══════════════════════════════════════════════════╝

Note on dependencies:
  Requires `requests` and `rich`. If your OS blocks system-wide `pip install`
  (PEP 668 / externally-managed-environment), run inside a Python venv:
    python3 -m venv .venv && source .venv/bin/activate
    pip install requests rich
"""

# ─────────────────────────────────────────────────
#  YOUR CREDENTIALS  ←  EDIT THESE TWO LINES ONLY
# ─────────────────────────────────────────────────
API_KEY  = "tskey-api-YOUR_KEY_HERE"
TAILNET  = "YOUR_TAILNET_OR_EMAIL"   # e.g. "yourname@gmail.com" or "example.ts.net"
# ─────────────────────────────────────────────────

import sys
import time
import subprocess
import threading
import platform
import re
import requests
from datetime import datetime, timezone

try:
    from rich.console import Console, Group
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich.live import Live
    from rich import box
    from rich.align import Align
    from rich.rule import Rule
except ImportError:
    print("Installing 'rich'...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rich"])
    from rich.console import Console, Group
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich.live import Live
    from rich import box
    from rich.align import Align
    from rich.rule import Rule

# ─────────────────────────────────────────────────
REFRESH_SECONDS  = 10          # how often to poll Tailscale API
PING_TIMEOUT     = 3           # seconds per ping
METRICS_TIMEOUT  = 3           # seconds to fetch /metrics
METRICS_PORT     = 5252        # Tailscale local metrics port
API_BASE         = "https://api.tailscale.com/api/v2"
console = Console()

OS_ICONS = {
    "linux": "🐧", "windows": "🪟", "android": "🤖",
    "ios": "📱", "macos": "🍎", "darwin": "🍎",
    "tvos": "📺", "freebsd": "😈", "openbsd": "🐡",
}
def os_icon(s): return OS_ICONS.get((s or "").lower(), "💻")

# ─────────────────────────────────────────────────
# SHARED STATE  (written by background threads, read by UI)
# ─────────────────────────────────────────────────
ping_results   = {}   # ip -> ms float or None
metrics_data   = {}   # ip -> metrics dict
prev_ts        = {}   # ip -> timestamp of last metrics poll
state_lock     = threading.Lock()

# watch mode: tracks online state per node id across polls
prev_online    = {}   # node_id -> bool
watch_events   = []   # list of (timestamp, name, label, color)

# ─────────────────────────────────────────────────
# PING  (background thread per node)
# ─────────────────────────────────────────────────

def _ping_one(ip: str):
    """Ping an IP once, store result in ping_results."""
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT * 1000), ip]
    elif system == "linux" or "android" in system or "termux" in system:
        cmd = ["ping", "-c", "1", "-W", str(PING_TIMEOUT), ip]
    else:  # macOS / BSD
        cmd = ["ping", "-c", "1", "-t", str(PING_TIMEOUT), ip]

    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=PING_TIMEOUT + 1
        )
        text = out.stdout + out.stderr
        m = re.search(r"time[=<]([\d.]+)\s*ms", text)
        ms = float(m.group(1)) if m else None
    except Exception:
        ms = None

    with state_lock:
        ping_results[ip] = ms


def ping_all_async(ips: list):
    """Fire off ping threads for all IPs, non-blocking."""
    threads = []
    for ip in ips:
        t = threading.Thread(target=_ping_one, args=(ip,), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.time() + PING_TIMEOUT + 2
    for t in threads:
        remaining = max(0, deadline - time.time())
        t.join(timeout=remaining)


# ─────────────────────────────────────────────────
# METRICS  (background thread per node)
# ─────────────────────────────────────────────────

def _parse_prometheus(text: str) -> list:
    """Parse Prometheus-format metrics from Tailscale's /metrics endpoint."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            m = re.match(r'^([A-Za-z_]\S*?)\{([^}]*)\}\s+([\d.e+\-]+)$', line)
            if m:
                name, labels_str, value = m.groups()
                labels = dict(re.findall(r'(\w+)="([^"]*)"', labels_str))
                rows.append((name, labels, float(value)))
            else:
                parts = line.split()
                if len(parts) == 2:
                    rows.append((parts[0], {}, float(parts[1])))
        except Exception:
            pass
    return rows


def _fetch_metrics_one(ip: str):
    """Fetch /metrics from one node and compute tx/rx totals, speeds, and DERP vs direct split."""
    url = f"http://{ip}:{METRICS_PORT}/metrics"
    try:
        r = requests.get(url, timeout=METRICS_TIMEOUT)
        if r.status_code != 200:
            with state_lock:
                metrics_data[ip] = {"ok": False, "err": f"HTTP {r.status_code}"}
            return

        rows = _parse_prometheus(r.text)

        tx_total   = 0.0
        rx_total   = 0.0
        tx_derp    = 0.0
        rx_derp    = 0.0
        tx_direct  = 0.0
        rx_direct  = 0.0
        dropped_in = 0.0
        dropped_out= 0.0
        pkts_in    = 0.0
        pkts_out   = 0.0
        derp_region= None
        health_warn= 0

        for name, labels, val in rows:
            path = labels.get("path", "")

            if name == "tailscaled_outbound_bytes_total":
                tx_total += val
                if path == "derp":
                    tx_derp += val
                elif "direct" in path:
                    tx_direct += val

            elif name == "tailscaled_inbound_bytes_total":
                rx_total += val
                if path == "derp":
                    rx_derp += val
                elif "direct" in path:
                    rx_direct += val

            elif name == "tailscaled_inbound_dropped_packets_total":
                dropped_in += val

            elif name == "tailscaled_outbound_dropped_packets_total":
                dropped_out += val

            elif name == "tailscaled_inbound_packets_total":
                pkts_in += val

            elif name == "tailscaled_outbound_packets_total":
                pkts_out += val

            elif name == "tailscaled_home_derp_region_id":
                derp_region = int(val)

            elif name == "tailscaled_health_messages":
                health_warn += val

        with state_lock:
            now       = time.time()
            prev      = metrics_data.get(ip, {})
            prev_time = prev_ts.get(ip, now)
            dt        = max(now - prev_time, 1.0)

            tx_speed = max(0.0, (tx_total - prev.get("tx_bytes", tx_total)) / dt)
            rx_speed = max(0.0, (rx_total - prev.get("rx_bytes", rx_total)) / dt)

            metrics_data[ip] = {
                "ok":          True,
                "tx_bytes":    tx_total,
                "rx_bytes":    rx_total,
                "tx_speed":    tx_speed,
                "rx_speed":    rx_speed,
                "tx_derp":     tx_derp,
                "rx_derp":     rx_derp,
                "tx_direct":   tx_direct,
                "rx_direct":   rx_direct,
                "pkts_in":     pkts_in,
                "pkts_out":    pkts_out,
                "dropped_in":  dropped_in,
                "dropped_out": dropped_out,
                "derp_region": derp_region,
                "health_warn": health_warn,
            }
            prev_ts[ip] = now

    except requests.exceptions.ConnectionError:
        with state_lock:
            metrics_data[ip] = {"ok": False, "err": "port closed"}
    except Exception as e:
        with state_lock:
            metrics_data[ip] = {"ok": False, "err": str(e)[:30]}


def fetch_metrics_async(ips: list):
    """Fire off metrics fetch threads for all IPs."""
    threads = []
    for ip in ips:
        t = threading.Thread(target=_fetch_metrics_one, args=(ip,), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.time() + METRICS_TIMEOUT + 1
    for t in threads:
        remaining = max(0, deadline - time.time())
        t.join(timeout=remaining)


# ─────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────

def fmt_bytes(n: float) -> str:
    """Human-readable bytes."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_speed(bps: float) -> str:
    """Human-readable bytes/sec as bits/sec."""
    bps = bps * 8
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if abs(bps) < 1000:
            return f"{bps:.1f} {unit}"
        bps /= 1000
    return f"{bps:.1f} Tbps"


def ping_color(ms):
    if ms is None:     return "dim red"
    if ms < 20:        return "bold green"
    if ms < 60:        return "green"
    if ms < 150:       return "yellow"
    return "red"


def parse_last_seen(last_seen_str: str):
    if not last_seen_str:
        return True, "● now"
    try:
        dt = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 90:    return True,  f"{secs}s ago"
        if secs < 3600:  return False, f"{secs//60}m ago"
        if secs < 86400: return False, f"{secs//3600}h ago"
        return False, f"{secs//86400}d ago"
    except Exception:
        return False, "unknown"


def format_ip(addresses):
    for a in (addresses or []):
        if a.startswith("100."):
            return a
    return addresses[0] if addresses else ""


def key_expiry_warning(expires: str):
    if not expires:
        return None
    try:
        dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        days = (dt - datetime.now(timezone.utc)).days
        if days < 0:    return "EXPIRED"
        if days <= 14:  return f"key {days}d"
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────
# WATCH MODE
# ─────────────────────────────────────────────────

def update_watch(devices: list):
    """Detect nodes that came online or went offline since last poll."""
    global prev_online, watch_events
    for dev in devices:
        node_id   = dev.get("id", dev.get("nodeId", ""))
        name      = (dev.get("hostname") or dev.get("name", "?")).split(".")[0]
        connected = dev.get("connectedToControl", False)
        ls        = dev.get("lastSeen", "")
        online, _ = parse_last_seen(ls)
        if connected:
            online = True

        if node_id in prev_online:
            was = prev_online[node_id]
            if online and not was:
                watch_events.append((datetime.now(), name, "came online", "green"))
            elif not online and was:
                watch_events.append((datetime.now(), name, "went offline", "red"))

        prev_online[node_id] = online

    watch_events = watch_events[-20:]


# ─────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────

def get_devices() -> list:
    url     = f"{API_BASE}/tailnet/{TAILNET}/devices"
    headers = {"Authorization": f"Bearer {API_KEY}"}
    r = requests.get(url, headers=headers, params={"fields": "all"}, timeout=10)
    r.raise_for_status()
    return r.json().get("devices", [])


# ─────────────────────────────────────────────────
# RENDERING
# ─────────────────────────────────────────────────

def build_header(total: int, online: int) -> Panel:
    now  = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    left = Text()
    left.append("  TS-MON v2", style="bold cyan")
    left.append("  //  Tailscale Network Monitor", style="dim white")
    right = Text(justify="right")
    right.append("tailnet: ", style="dim")
    right.append(f"{TAILNET}   ", style="yellow")
    right.append(f"🟢 {online} online  ", style="green")
    right.append(f"⚫ {total - online} offline  ", style="dim red" if total > online else "dim")
    right.append(f"🕐 {now}", style="dim")
    return Panel(Columns([left, Align(right, align="right")], expand=True),
                 style="cyan", padding=(0, 1))


def build_summary_panels(devices: list) -> list:
    online  = sum(1 for d in devices
                  if d.get("connectedToControl") or not d.get("lastSeen"))
    offline = len(devices) - online

    pct   = int(online / max(len(devices), 1) * 100)
    col   = "green" if pct == 100 else "yellow" if pct > 50 else "red"
    ht    = Text(justify="center")
    ht.append(f"{pct}%\n",           style=f"bold {col}")
    ht.append(f"{online}/{len(devices)} nodes\n", style="dim")
    ht.append("network health",      style="dim cyan")
    panels = [Panel(Align(ht, align="center"), title="[cyan]Health[/cyan]",
                    border_style="cyan", width=22)]

    with state_lock:
        pings = dict(ping_results)
    good = [v for v in pings.values() if v is not None]
    pt = Text(justify="center")
    if good:
        pt.append(f"{min(good):.0f} / {sum(good)/len(good):.0f} / {max(good):.0f} ms\n",
                  style="green")
        pt.append("min / avg / max\n", style="dim")
    else:
        pt.append("pinging...\n", style="dim")
    pt.append(f"{len(good)}/{len(pings)} reachable", style="dim cyan")
    panels.append(Panel(Align(pt, align="center"), title="[cyan]Ping Summary[/cyan]",
                        border_style="cyan", width=28))

    with state_lock:
        mx = dict(metrics_data)
    total_tx = sum(v.get("tx_speed", 0) for v in mx.values() if v.get("ok"))
    total_rx = sum(v.get("rx_speed", 0) for v in mx.values() if v.get("ok"))
    tt = Text(justify="center")
    tt.append(f"↑ {fmt_speed(total_tx)}\n", style="green")
    tt.append(f"↓ {fmt_speed(total_rx)}\n", style="cyan")
    tt.append("combined network I/O",        style="dim cyan")
    panels.append(Panel(Align(tt, align="center"), title="[cyan]Traffic[/cyan]",
                        border_style="cyan", width=28))

    need_update  = sum(1 for d in devices if d.get("updateAvailable"))
    expiry_warns = sum(1 for d in devices if key_expiry_warning(d.get("expires", "")))
    at = Text()
    if need_update:  at.append(f"  ↑  {need_update} update(s) available\n",   style="yellow")
    if expiry_warns: at.append(f"  ⚠  {expiry_warns} key(s) expiring soon\n", style="yellow")
    if offline:      at.append(f"  ⚫  {offline} node(s) offline\n",           style="dim red")
    if not at.plain.strip(): at.append("  ✓  All clear\n", style="green")
    panels.append(Panel(at, title="[cyan]Alerts[/cyan]", border_style="cyan", width=30))

    return panels


def build_devices_table(devices: list) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                  header_style="bold cyan", border_style="dim cyan",
                  expand=True, padding=(0, 1))

    table.add_column("Status",       width=9,  no_wrap=True)
    table.add_column("Device",       min_width=14, no_wrap=True)
    table.add_column("OS",           width=8,  no_wrap=True)
    table.add_column("Tailscale IP", width=15, no_wrap=True)
    table.add_column("Ping",         width=9,  no_wrap=True)
    table.add_column("↑ Speed",      width=12, no_wrap=True)
    table.add_column("↓ Speed",      width=12, no_wrap=True)
    table.add_column("TX Total",     width=11, no_wrap=True)
    table.add_column("RX Total",     width=11, no_wrap=True)
    table.add_column("Last Seen",    width=11, no_wrap=True)
    table.add_column("Flags",        min_width=12)

    online_rows, offline_rows = [], []

    for dev in devices:
        connected = dev.get("connectedToControl", False)
        ls_str    = dev.get("lastSeen", "")
        online, ls_human = parse_last_seen(ls_str)
        if connected:
            online   = True
            ls_human = "● now"

        bucket = online_rows if online else offline_rows
        bucket.append((dev, online, ls_human))

    all_rows = online_rows + offline_rows
    sep_at   = len(online_rows) - 1

    for idx, (dev, online, ls_human) in enumerate(all_rows):
        name     = (dev.get("hostname") or dev.get("name", "?")).split(".")[0]
        ts_ip    = format_ip(dev.get("addresses", []))
        os_str   = dev.get("os", "")
        key_warn = key_expiry_warning(dev.get("expires", ""))
        routes   = dev.get("advertisedRoutes", [])
        upd      = dev.get("updateAvailable", False)
        shield   = dev.get("blocksIncomingConnections", False)

        just_changed = any(e[1] == name for e in watch_events[-3:]) if watch_events else False

        if online:
            st = Text("● ONLINE", style="bold green")
        else:
            st = Text("○ offline", style="dim red")
        if just_changed:
            st = Text("◉ CHANGE!", style="bold yellow")

        nm = Text()
        if just_changed:
            nm.append(name, style="bold yellow")
        elif online:
            nm.append(name, style="bold white")
        else:
            nm.append(name, style="dim")

        os_t = Text(f"{os_icon(os_str)} {os_str[:3].upper()}",
                    style="white" if online else "dim")

        ip_t = Text(ts_ip, style="cyan" if online else "dim")

        with state_lock:
            ms = ping_results.get(ts_ip)
        if not online:
            ping_t = Text("—", style="dim")
        elif ts_ip not in ping_results:
            ping_t = Text("...", style="dim")
        elif ms is None:
            ping_t = Text("timeout", style="dim red")
        else:
            ping_t = Text(f"{ms:.1f}ms", style=ping_color(ms))

        ls_t = Text(ls_human, style="green" if online else "dim red")

        flags = []
        if key_warn: flags.append(f"⚠ {key_warn}")
        if upd:      flags.append("↑ upd")
        if shield:   flags.append("🛡")
        if routes:   flags.append(f"🔀{len(routes)}rt")
        fl_t = Text("  ".join(flags), style="yellow") if flags else Text("", style="dim")

        with state_lock:
            mx = dict(metrics_data.get(ts_ip, {}))

        if online and mx.get("ok"):
            tx_spd = Text(fmt_speed(mx.get("tx_speed", 0)), style="green")
            rx_spd = Text(fmt_speed(mx.get("rx_speed", 0)), style="cyan")
            tx_b   = mx.get("tx_bytes", 0)
            rx_b   = mx.get("rx_bytes", 0)
            tx_d   = mx.get("tx_derp",  0)
            rx_d   = mx.get("rx_derp",  0)
            tx_via = " (D)" if tx_b > 0 and tx_d / max(tx_b, 1) > 0.1 else ""
            rx_via = " (D)" if rx_b > 0 and rx_d / max(rx_b, 1) > 0.1 else ""
            tx_tot = Text(fmt_bytes(tx_b) + tx_via,
                          style="dim yellow" if tx_via else "dim green")
            rx_tot = Text(fmt_bytes(rx_b) + rx_via,
                          style="dim yellow" if rx_via else "dim cyan")
            dropped = mx.get("dropped_in", 0) + mx.get("dropped_out", 0)
            if dropped > 1000:
                fl_t.append(f"  ✗{int(dropped/1000)}Kdrp", style="dim red")
            elif dropped > 0:
                fl_t.append(f"  ✗{int(dropped)}drp", style="dim red")
            if not fl_t.plain.strip():
                fl_t = Text("—", style="dim")
        elif online and ts_ip not in metrics_data:
            tx_spd = rx_spd = Text("...", style="dim")
            tx_tot = rx_tot = Text("...", style="dim")
            if not fl_t.plain.strip():
                fl_t = Text("—", style="dim")
        else:
            err = mx.get("err", "no metrics") if mx else "port closed"
            tx_spd = rx_spd = Text("—", style="dim")
            tx_tot = Text(err[:14], style="dim red")
            rx_tot = Text("—", style="dim")
            if not fl_t.plain.strip():
                fl_t = Text("—", style="dim")

        table.add_row(st, nm, os_t, ip_t, ping_t,
                      tx_spd, rx_spd, tx_tot, rx_tot,
                      ls_t, fl_t)

        if idx == sep_at and offline_rows:
            table.add_section()

    return table


def build_watch_panel() -> Panel:
    if not watch_events:
        t = Text("  No state changes detected yet", style="dim")
    else:
        t = Text()
        for ts, name, event, color in reversed(watch_events[-8:]):
            ts_str = ts.strftime("%H:%M:%S")
            t.append(f"  {ts_str}  ", style="dim")
            t.append(f"{name:<20}", style="bold white")
            t.append(f"  {event}\n", style=f"bold {color}")
    return Panel(t, title="[cyan]Watch Events[/cyan]", border_style="cyan")


def build_display(devices: list, status_msg: str) -> Group:
    online_count = sum(
        1 for d in devices
        if d.get("connectedToControl") or not d.get("lastSeen")
    )
    return Group(
        build_header(len(devices), online_count),
        Columns(build_summary_panels(devices), expand=False),
        Rule(style="dim cyan"),
        build_devices_table(devices),
        Rule(style="dim cyan"),
        build_watch_panel(),
        Rule(style="dim cyan"),
        Text(
            f"  Refreshing every {REFRESH_SECONDS}s  ·  Ctrl+C to quit  ·  {status_msg}",
            style="dim", justify="center"
        ),
    )


# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

def check_credentials():
    if "YOUR_" in API_KEY or "YOUR_" in TAILNET:
        console.print(Panel(
            "[bold red]Credentials not set![/bold red]\n\n"
            "Edit this script and fill in:\n\n"
            "  [yellow]API_KEY[/yellow]  = your Tailscale API key\n"
            "           (admin console → Settings → Keys → Generate access token)\n\n"
            "  [yellow]TAILNET[/yellow]  = your tailnet name\n"
            "           (e.g.  yourname@gmail.com  or  example.ts.net)",
            title="[red]Setup Required[/red]", border_style="red",
        ))
        sys.exit(1)


def main():
    check_credentials()
    devices = []
    status  = "Starting up..."

    console.print(f"[cyan]Connecting to Tailscale API →[/cyan] [yellow]{TAILNET}[/yellow]\n")

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        while True:
            try:
                devices = get_devices()
                status  = f"Updated {datetime.now().strftime('%H:%M:%S')}"
            except requests.exceptions.HTTPError as e:
                code = e.response.status_code
                status = f"❌ API {code} — {'bad API key' if code==401 else 'tailnet not found' if code==404 else 'server error'}"
            except requests.exceptions.ConnectionError:
                status = "❌ No connection"
            except Exception as e:
                status = f"❌ {e}"

            if not devices:
                live.update(Panel(f"[yellow]{status}[/yellow]\n\n[dim]Retrying...[/dim]",
                                  title="[cyan]TS-MON v2[/cyan]", border_style="cyan"))
                time.sleep(REFRESH_SECONDS)
                continue

            update_watch(devices)

            online_ips = []
            for dev in devices:
                connected = dev.get("connectedToControl", False)
                ls        = dev.get("lastSeen", "")
                online, _ = parse_last_seen(ls)
                if connected: online = True
                if online:
                    ip = format_ip(dev.get("addresses", []))
                    if ip:
                        online_ips.append(ip)

            ping_thread = threading.Thread(
                target=ping_all_async, args=(online_ips,), daemon=True)
            metrics_thread = threading.Thread(
                target=fetch_metrics_async, args=(online_ips,), daemon=True)
            ping_thread.start()
            metrics_thread.start()

            deadline = time.time() + REFRESH_SECONDS
            while time.time() < deadline:
                if devices:
                    live.update(build_display(devices, status))
                time.sleep(0.5)

            ping_thread.join(timeout=1)
            metrics_thread.join(timeout=1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim cyan]Goodbye.[/dim cyan]\n")
```[cite: 30]

---

### 2. Step-by-Step to Add It on GitHub
1. Go to the **root** of your `security-engineering-portfolio` repo (`ZvMartin/security-engineering-portfolio`)[cite: 28, 29].
2. Click **`Add file`** → **`Create new file`**[cite: 28].
3. In the filename box, type:
   ```text
   scripts/tailmon.py
