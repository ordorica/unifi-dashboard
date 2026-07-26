# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-process live dashboard for a home UniFi network. It holds one long-lived
authenticated controller session, pushes live numbers to the browser over a WebSocket,
persists periodic snapshots to SQLite for history charts, and serves a single-page UI
on `http://127.0.0.1:8787` (loopback only — the process holds live controller
credentials, so it is deliberately not exposed beyond the machine).

There is no build step, no framework, and no test suite. The UI is one hand-written
HTML file with inline CSS/JS.

## Running

The server runs continuously under launchd as `com.hector.unifi-live-monitor`
(plist at `~/Library/LaunchAgents/`). Backend changes require a restart:

```bash
launchctl kickstart -k gui/$(id -u)/com.hector.unifi-live-monitor
```

Frontend-only changes (`static/live_dashboard.html`) need just a browser reload.

Run in the foreground for debugging:

```bash
uv run --python 3.14 --with unifi-core --with aiounifi python3 live_server.py
```

Syntax-check before restarting (there is no linter configured):

```bash
python3 -c "import ast; ast.parse(open('live_server.py').read())"
```

Check for errors after a restart — the log also contains aiohttp access lines, so
filter to the section after startup:

```bash
tail -300 live_server.log | sed -n '/Live server ready/,$p' | grep -i "loop failed\|tick failed\|Traceback"
```

`poll_unifi.py` is a legacy one-shot poller kept for manual backfills; the live
server owns all recurring polling now.

## Credentials

`unifi_lib/fetch.py` reads controller host/username/password from the `env` block of
`../../.claude/settings.local.json` (i.e. outside this directory). Nothing is stored
in the repo, but note that `unifi_clients.db` contains real client MACs, hostnames
and IPs — relevant if this is ever published.

## Architecture

### Three polling tiers (`live_server.py`)

The tier a piece of data belongs to is the single most important design decision here.

| Loop | Interval | Purpose |
|---|---|---|
| `fast_loop` | 1s | Cheap live numbers → built into a `tick` dict → broadcast over WebSocket. Never do heavy calls here. |
| `persist_loop` | 60s | Durable SQLite snapshot; also refreshes the network map and broadcasts `offline-update`. |
| `slow_loop` | 60s | Expensive calls (speedtest archive, neighbor AP scan). The neighbor-history append within it is further throttled to `ROGUE_HISTORY_INTERVAL` (300s) — see Database growth below. |

Anything expensive that the UI needs on demand (flows, health, firewall, routing,
config, usage) is **not** in a loop. It is fetched by a REST handler and memoised in
`state.flow_cache` via `_cached_flow(key, producer)` with a 30s TTL, so multiple
browser tabs don't hammer the controller.

### Data flow

```
UniFi controller
   └─ unifi_lib/fetch.py     UnifiSession — one reconnecting session, all controller reads
        ├─ fast_loop  ──► tick dict ──► WebSocket ──► onTick() in the browser
        ├─ persist_loop ──► unifi_lib/persist.py ──► SQLite (unifi_lib/db.py)
        └─ REST handlers ──► _cached_flow (30s TTL) ──► fetch()  in the browser
```

`unifi_lib/` responsibilities are strict: `fetch.py` only reads from the controller,
`persist.py` only writes to SQLite plus pure field-extraction helpers, `db.py` owns
schema and pruning. `live_server.py` holds the loops, HTTP/WS routes and response
shaping.

### Deliberate duplication

`live_server.py` and `persist.py` each compute some of the same fields (e.g. gateway
rx/tx rates, uplink byte rates) rather than sharing a helper. This is intentional and
called out in comments — the live-tick shape and the persisted shape evolve
independently. Follow the existing pattern instead of unifying them.

### Schema and migrations

Migrations are inline in `db.init_db()`: `CREATE TABLE IF NOT EXISTS`, then
`PRAGMA table_info` + `ALTER TABLE ADD COLUMN` for new columns. Always additive, so
a restart on an older DB is safe. Every time-series table must also get a
`DELETE FROM … WHERE ts < cutoff` line in `prune_old()` (`RETENTION_DAYS = 30`).

Two distinct byte-column conventions coexist, and mixing them corrupts the usage math:
- `*_rate_bps` — instantaneous rate at that sample.
- `*_bytes_total` — **monotonic cumulative** counters. `_usage_since()` computes usage
  as a delta between two of these, so never write per-interval deltas into them.
  The `stat/report` backfill only populates rate columns for exactly this reason.

### Controller history backfill

`backfill_gateway_history()` runs once at startup and seeds `gateway_stats` from the
controller's own `stat/report` rollups, so long-range charts aren't empty after a
restart. The controller keeps 5-minute data for 24h, hourly for 7d, daily for 30d;
the backfill takes each row from the finest scope covering it and keeps the regions
disjoint. It uses `INSERT OR IGNORE` so it never clobbers live rows.

### Network/VLAN mapping

`persist.set_networks()` builds a subnet→name map from the controller's real network
config each persist cycle; `network_for(ip)` resolves via `ipaddress` containment,
sorted **most-specific-first** (a /30 transit net sits inside a /24 LAN and must win).
Do not reintroduce a hardcoded IP-prefix table — that previously mislabelled every
VPN subnet as `other`. Controller names can carry stray whitespace and are stripped
before being used as keys.

## Frontend (`static/live_dashboard.html`)

One file, ~3300 lines: inline CSS, then markup for nine `.page` divs, then the JS.
Tabs: overview, devices, wifi, routing, config, firewall, health, flows, clients.

Conventions worth knowing before editing:

- **Tick-driven tables re-render every second.** Anything stateful (sort key, filter,
  search text, which row is expanded) must live in a module-level variable outside the
  render function, or it resets each tick. Rebuilding a table does not disturb a focused
  `<input>` because inputs sit outside the `<tbody>` that gets replaced.
- **`renderLineChart(el, {series, xLabels, xValues, ...})`** is the shared chart. Pass
  `xValues` (epoch ms, via `bucketTime()`) so points are positioned by *time*, not by
  array index — without it, mixed-granularity data (backfilled daily + live per-minute)
  collapses most of the range into a sliver. It renders an "all values null" series as
  the empty state, and switches to a data-fit y-axis only when values go negative (dBm).
- **Click-to-drill-down rows** follow one pattern: a module-level "which row is open"
  variable, an extra `<tr>` injected by the render function, a `{key: rows}` cache so
  live re-renders redraw from cache instead of flashing "Loading…", and a fetch only on
  click plus a slow `setInterval`.
- **Shared helpers**: `paginate`/`renderPaginationControls`, `renderApChipFilter`
  (with a rebuild guard so per-tick re-renders don't drop click state),
  `sparklineSvg`, `jumpToDevice(mac)`, `fmtBits`/`fmtBytes`/`fmtUptime`, `clBadge`.
- **`NETWORKS` is populated at runtime** from `/api/networks`; `loadNetworks()` must
  resolve before anything that colours or filters by network.
- **Spacing**: `.section` has a top margin only (`28px 0 0`). Page-level control rows
  must be wrapped in a block-level `.controls` div — a bare `.seg` is `inline-flex` and
  its margin will *add* to the section margin instead of collapsing.

## Reporting controller data honestly

Several controller fields are ambiguous, and the codebase makes deliberate choices that
should be preserved:

- A WAN latency of `0` means "standby link, not measured", not a real 0ms RTT. Both
  interface-level and per-monitor latencies normalise `0` → `null`, and the UI shows
  "standby — not measured".
- `uptime_stats.WAN.monitors` and `.alerting_monitors` are two *different* sets, not a
  fallback pair; read both (`persist.wan_monitors()`). The same host can be probed over
  both ICMP and DNS, so `(target, type)` is the key, not target alone.
- DPI/QoS application and category IDs come back as bare integers with
  `application_name: null`. Show the numeric id rather than inventing a label.
- `/api/flows/stats` (whole-period totals) and `/api/flows/recent` (newest ≤1000 flows)
  deliberately disagree in magnitude; the UI labels which is which.
- Per-client "connected for" is derived from the absolute `assoc_time` epoch at query
  time, not from the controller's relative `uptime` counter, which would be up to 60s
  stale by the time it is read.

## Database growth

`rogue_aps_history` dominates the database — one row per neighbor sighting per write,
~330 sightings here. Written at the `SLOW_INTERVAL` 60s cadence it produced ~490k
rows/day (~654k rows of a 208MB DB in two days, projecting to ~15M at 30-day
retention), so the history append is now gated to `ROGUE_HISTORY_INTERVAL` (300s),
cutting that growth 5×.

`persist_rogue_aps(..., write_history=False)` skips only the history append; the
`rogue_aps` latest-state upsert still runs every cycle, because the neighbor list
reads it and its `updated_at` drives stale-neighbor pruning. Keep that split if you
change the cadence again.

Note the existing ~654k rows were written at the old 60s rate and will age out of the
30-day window on their own. `live_server.log` still grows unbounded via aiohttp
access logging.
