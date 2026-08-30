# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-process live dashboard for a home UniFi network. It holds one long-lived
authenticated controller session, pushes live numbers to the browser over a WebSocket,
persists periodic snapshots to SQLite for history charts, and serves a single-page UI
on `http://<nas-ip>:8787`. It runs as a Docker container on the NAS; the
dashboard has no authentication and is published to the LAN unrestricted,
which is a deliberate choice recorded in
`docs/superpowers/specs/2026-07-26-docker-containerization-design.md`.

There is no build step, no framework, and no test suite. The UI is one hand-written
HTML file with inline CSS/JS.

## Running

The server runs as a Docker container on the NAS, defined by
`docker-compose.yml`. Images are built by GitHub Actions for linux/amd64 and
linux/arm64 and published to ghcr.io; the NAS only pulls.

Deploy a change: push to `main`, wait for the Actions run, then on the NAS:

```bash
cd /volume1/docker/unifi-dashboard && docker compose pull && docker compose up -d
```

Run locally against the live controller (use a spare port to avoid clashing
with anything else):

```bash
cp .env.example .env    # fill in credentials; .env is gitignored
HOST_PORT=8788 docker compose up -d --build
docker compose logs -f
```

Run in the foreground without Docker, as before:

```bash
uv run --python 3.14 --with unifi-core --with aiounifi python3 live_server.py
```

Syntax-check before deploying (there is no linter configured):

```bash
python3 -c "import ast; ast.parse(open('live_server.py').read())"
```

Check for errors after a deploy:

```bash
docker compose logs --no-color | grep -iE "loop failed|tick failed|Traceback"
```

Configuration is entirely environment variables — see `.env.example`.
`BIND_HOST`, `BIND_PORT` and `UNIFI_DB_PATH` all default to the original
hardcoded values, so running the code directly still behaves as it always did.

`poll_unifi.py` is a legacy one-shot poller kept for manual backfills; the live
server owns all recurring polling now.

## Credentials

`unifi_lib/fetch.py` reads the controller host/username/password from the
process environment first (`UNIFI_NETWORK_HOST`, `UNIFI_NETWORK_USERNAME`,
`UNIFI_NETWORK_PASSWORD`), falling back to the `env` block of
`../../.claude/settings.local.json` when that file exists — which it does only
on the development Mac. In the container the values come from `.env`, which is
gitignored and must never be committed.

`unifi_clients.db` contains real client MACs, hostnames and IPs. It, the logs
and `.env` are all excluded by `.gitignore` and `.dockerignore`; that exclusion
is load-bearing, since this repository is on GitHub.

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

### WAN Paths vs Gateway Devices

A **WAN Path** is one internet connection (`WAN`, `WAN3`); a **Gateway Device**
is a physical box. They are not the same thing and must not be merged: here the
cellular WAN Path is a GRE tunnel on the UDM with no CPU of its own, while the
cellular modem that does have a CPU owns no WAN Path.

WAN Paths are discovered from `last_wan_interfaces` and given stable synthetic
ids in `wan_paths` (see `docs/adr/0001-wan-path-identity.md`), so history follows
the internet service rather than the socket. Per-sample WAN data lives in
`wan_stats` keyed on that id; device metrics stay in `gateway_stats` keyed on MAC.
Whether a path is cellular comes from `wan.type`, never from its key name.

Speedtests are attributed by observing `speedtest-status.interface_name` in
`fast_loop`. The archive carries no WAN field, and `speedtest-status.timestamp`
is a refresh time rather than a completion time, so timestamp correlation does
not work. Unattributable speedtests stay NULL — never guessed.

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

### No controller history backfill for WAN throughput

There is no startup backfill for `wan_stats` (or, since `wan-portability`, for
`gateway_stats`'s old `wan_*` columns — those columns are still in the schema,
additive-migration rule, but nothing writes or reads them any more). A prior
`backfill_gateway_history()` seeded `gateway_stats.wan_rx_rate_bps`/`wan_tx_rate_bps`
from the controller's `stat/report` rollups at startup, but nothing had read those
columns since WAN throughput moved to `wan_stats` keyed on `wan_path_id` — it was
writing data nothing consumed, so it was removed.

It isn't coming back as a `wan_stats` backfill either: `stat/report`'s `gw` scope
returns gateway-level totals (rx/tx summed across every WAN Path on the device), with
no per-path breakdown, so a row from it cannot be attributed to a specific WAN Path —
the same unattributable class as historical speedtests (see "Reporting controller data
honestly" below). Guessing an attribution (e.g. crediting it all to whichever path
happens to be first) would misrepresent history for exactly the multi-path gateways
this project exists to support, so none is invented.

**Consequence:** long-range WAN throughput charts are genuinely sparse right after a
fresh start (new install, empty DB, or restore) and only fill in as `wan_stats`
accumulates its own live samples at the normal `PERSIST_INTERVAL` cadence. This is
expected, not a bug.

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
- Which WAN Path a speedtest ran over is not a field on the archived record itself --
  it has none. Attribution instead observes `speedtest-status.interface_name` live in
  `fast_loop` and matches it to the archive by exact throughput, since
  `speedtest-status.timestamp` is a refresh time rather than a completion time and so
  cannot be correlated by time either. A speedtest that predates observation, or whose
  reading was missed between polls, stays unattributed (`wan_path_id IS NULL`) rather
  than being guessed.
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
30-day window on their own. Container logs are capped by the compose json-file driver
at 3 × 10MB, so the aiohttp access logging that previously grew `live_server.log`
without limit is now bounded.

### Devices that leave the controller

`devices` rows used to be immortal — the upsert in `persist.py` is the only
writer and nothing deleted. `sweep_absent_devices()` now marks a device
`absent` once the controller has not reported it for `DEVICE_ABSENT_AFTER_MINUTES`
(10) and deletes it, with every row keyed to its MAC, after
`DEVICE_ABSENCE_DAYS` (7).

A powered-off device is **not** absent: it still appears in `get_devices()`
with `state != 1`, so its `updated_at` keeps being refreshed. Only a device
forgotten in the controller stops being refreshed. The sweep refuses to act
on a zero-device poll, so an authentication failure cannot cascade into data
loss.

`delete_device()` keeps `speedtests` rows and nulls their `wan_path_id` — a
speedtest measures the internet service, not the box. It is also the one
thing that removes a `wan_paths` row: that table is never pruned *by age*,
but a path whose Gateway Device no longer exists goes with it.

Absent devices are merged into the tick from `state.absent_devices`, because
`tick["devices"]` is otherwise built purely from the live controller fetch and
a forgotten device would vanish from the UI instantly. They can be deleted
early via `DELETE /api/devices/{mac}`, which refuses any device that is not
`absent` — the only write route in the application.
