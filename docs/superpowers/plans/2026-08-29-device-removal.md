# Device Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A device removed from the UniFi controller is marked absent after ten minutes, deleted with all its history after seven days, and can be deleted by hand from the dashboard at any point in between.

**Architecture:** `devices.updated_at` already means "last time the controller's inventory reported this device", because the upsert at `persist.py:212` is its only writer — so absence needs no new column, only a threshold. One `delete_device()` cascade is shared by the automatic sweep and a new `DELETE` endpoint, so there is a single deletion path. Because `tick.devices` is built from the live controller fetch and never from the database, absent devices are merged back into the tick from a `state` cache refreshed once per persist cycle.

**Tech Stack:** Python 3.14, aiohttp, SQLite (WAL), vanilla JS. No build step, no test framework.

**Spec:** `docs/superpowers/specs/2026-08-29-device-removal-design.md`
**Domain terms:** `CONTEXT.md` — WAN Path, Gateway Device, Cellular Modem, Link Type
**Identity decision:** `docs/adr/0001-wan-path-identity.md`

## Global Constraints

- **Never delete on a zero-device poll.** `sweep_absent_devices(db, 0)` must change nothing. A controller returning an empty inventory is a credential or API problem, not eighteen simultaneous removals.
- **A powered-off device is not an absent device.** `device_status()` returns `"offline"` for `state != 1`, and such a device still appears in `get_devices()` and still has its `updated_at` refreshed. Only a device *missing from the inventory* is a candidate. Never key absence on `status == "offline"`.
- **`speedtests` rows are never deleted by the cascade** — their `wan_path_id` is set to `NULL`. A speedtest measures the internet service, not the box.
- **The delete endpoint only ever touches an `absent` device.** This dashboard is published to the LAN unauthenticated; the precondition is the only thing bounding what the route can destroy.
- **Migrations are additive only** — `CREATE TABLE IF NOT EXISTS`, then `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`. This feature needs no migration at all; do not add one.
- **Never open, modify or commit `unifi_clients.db`.** It is 237MB of real client MACs, hostnames and IPs. Every verification below sets `UNIFI_DB_PATH` to a temp file; `db.py` reads that env var at import time into `DB_FILE`.
- **Never print, echo or log any `UNIFI_NETWORK_*` value.** The MCP plugin injects real credentials into every shell.
- **Bind port 8791 for verification only.** 8787 is production, 8788 is in use.
- **No test framework exists and none is to be introduced.** Verification is executable shell commands, matching the codebase idiom (see CLAUDE.md "Running").
- All timestamps are UTC ISO-8601 strings. Never commit `*.db`, `*.log`, `.env`, `data/`, `__pycache__`.
- Run all commands from `/Users/hector/Projects/.unifi-dashboard`. Branch: `device-removal`.

---

### Task 1: The cascade and its constants

**Files:**
- Modify: `unifi_lib/db.py:13` (constants), `unifi_lib/db.py:402` (beside `prune_old`)
- Modify: `unifi_lib/db.py:337`, `unifi_lib/db.py:411-414` (comment wording)
- Modify: `live_server.py:485`, `live_server.py:549` (comment wording)
- Create (temporary, deleted in Task 7): `scratch_cascade_check.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DEVICE_ABSENT_AFTER_MINUTES = 10`, `DEVICE_ABSENCE_DAYS = 7`, and `delete_device(db: sqlite3.Connection, mac: str) -> None`. Tasks 2 and 5 both call `delete_device` with exactly this signature.

- [ ] **Step 1: Add the two constants**

In `unifi_lib/db.py`, directly below `RETENTION_DAYS = 30` (line 13):

```python
# How long the controller must stop reporting a device before the dashboard
# says so, and before it removes the device entirely. Ten minutes rather than
# a single 60s cycle so a brief blip does not flap rows in and out of the
# table; seven days so a device unplugged for a long weekend, or a controller
# outage over a holiday, still has room to come back before anything is lost.
DEVICE_ABSENT_AFTER_MINUTES = 10
DEVICE_ABSENCE_DAYS = 7
```

- [ ] **Step 2: Write `delete_device`**

In `unifi_lib/db.py`, immediately above `def prune_old(` (line 402):

```python
def delete_device(db: sqlite3.Connection, mac: str) -> None:
    """Remove one device and every row keyed to it.

    Children first, parent last. The WAN Path steps match nothing for a
    switch or an AP -- they own no paths and write no gateway_stats -- so one
    code path serves every category without branching on `category`.

    `speedtests` rows are deliberately kept and their `wan_path_id` set back
    to NULL. A speedtest measures the internet service, not the box that ran
    it: replace a gateway and the ISP performance history before the swap is
    still meaningful. NULL is a state the schema, the attribution code and
    the UI already handle -- unattributed speedtests stay unattributed.

    `clients.parent_mac`/`parent_name` are deliberately NOT cleared. They are
    denormalised labels recording where a client *was* attached, and a client
    that sat on a since-removed AP genuinely did sit on it.
    """
    path_ids = [r[0] for r in db.execute(
        "SELECT id FROM wan_paths WHERE gateway_mac = ?", (mac,)
    ).fetchall()]

    if path_ids:
        marks = ",".join("?" * len(path_ids))
        db.execute(
            f"UPDATE speedtests SET wan_path_id = NULL WHERE wan_path_id IN ({marks})",
            path_ids,
        )
        db.execute(f"DELETE FROM wan_stats WHERE wan_path_id IN ({marks})", path_ids)
        db.execute(f"DELETE FROM rtt_path_monitors WHERE wan_path_id IN ({marks})", path_ids)
        # rtt_monitors is legacy and no longer written, but old rows carry a
        # wan_path_id and would otherwise outlive their gateway.
        db.execute(f"DELETE FROM rtt_monitors WHERE wan_path_id IN ({marks})", path_ids)

    db.execute("DELETE FROM wan_paths WHERE gateway_mac = ?", (mac,))
    db.execute("DELETE FROM speedtest_observations WHERE gateway_mac = ?", (mac,))
    db.execute("DELETE FROM gateway_stats WHERE gateway_mac = ?", (mac,))
    db.execute("DELETE FROM device_stats WHERE mac = ?", (mac,))
    db.execute("DELETE FROM port_stats WHERE mac = ?", (mac,))
    db.execute("DELETE FROM ap_radios WHERE ap_mac = ?", (mac,))
    # ap_mac here is *our* AP that observed the neighbour, so these rows are
    # this device's observations and go with it.
    db.execute("DELETE FROM rogue_aps WHERE ap_mac = ?", (mac,))
    db.execute("DELETE FROM rogue_aps_history WHERE ap_mac = ?", (mac,))
    db.execute("DELETE FROM devices WHERE mac = ?", (mac,))
```

The f-strings interpolate only `?` placeholders whose count comes from
`len(path_ids)`; every value is still bound. Do not build these strings from
`mac` or from any path id directly.

- [ ] **Step 3: Narrow the "never pruned" comments**

`wan_paths` is documented in four places as never pruned. That rule is about
**time-based** pruning — an identity row must not age out merely because it is
old. Deleting because the owning Gateway Device no longer exists is a different
justification, and it strictly helps the concern already recorded at
`persist.py:437` and `:460`. Reword so the code and its stated invariant agree.

At `unifi_lib/db.py:337`, change "is deliberately NOT a time series and is
never pruned" to "is deliberately NOT a time series and is never pruned **by
age** (`delete_device` still removes a gateway's paths when the gateway itself
is removed from the controller)".

At `unifi_lib/db.py:411-414`, change "wan_paths is deliberately NOT pruned
here" to "wan_paths is deliberately NOT pruned **by age** here (see
`delete_device` for the one case that does remove a path row)".

At `live_server.py:485` and `live_server.py:549`, change each "deliberately
never pruned" to "deliberately never pruned by age".

- [ ] **Step 4: Write the check script**

Create `scratch_cascade_check.py`:

```python
"""Seed one gateway, one AP and a row in every table keyed to them, delete
both, and assert exactly what survives. Runs against a temp DB only."""
import os, sqlite3, sys, tempfile

DB = os.path.join(tempfile.mkdtemp(), "check.db")
os.environ["UNIFI_DB_PATH"] = DB
from unifi_lib import db  # noqa: E402  (must follow the env var)

conn = db.connect()
db.init_db(conn)

GW, AP = "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"
TS = "2026-08-29T00:00:00+00:00"

conn.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)",
             (GW, "gw", "UDMPRO", "gateway", "online", 1, "10.0.0.1", TS, None))
conn.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)",
             (AP, "ap", "U7", "ap", "online", 1, "10.0.0.2", TS, None))
conn.execute("INSERT INTO wan_paths (gateway_mac, wan_key, ifname, first_seen, last_seen) "
             "VALUES (?,?,?,?,?)", (GW, "WAN", "eth9", TS, TS))
pid = conn.execute("SELECT id FROM wan_paths WHERE gateway_mac = ?", (GW,)).fetchone()[0]

conn.execute("INSERT INTO wan_stats (ts, wan_path_id) VALUES (?,?)", (TS, pid))
conn.execute("INSERT INTO rtt_path_monitors (ts, wan_path_id, target, monitor_type) "
             "VALUES (?,?,?,?)", (TS, pid, "1.1.1.1", "icmp"))
conn.execute("INSERT INTO rtt_monitors (ts, gateway_kind, target, monitor_type, wan_path_id) "
             "VALUES (?,?,?,?,?)", (TS, "primary", "1.1.1.1", "icmp", pid))
conn.execute("INSERT INTO gateway_stats (ts, gateway_mac, gateway_kind) VALUES (?,?,?)",
             (TS, GW, "primary"))
conn.execute("INSERT INTO speedtest_observations (observed_at, ifname, xput_download, "
             "xput_upload, gateway_mac) VALUES (?,?,?,?,?)", (TS, "eth9", 900.0, 40.0, GW))
conn.execute("INSERT INTO speedtests (ts, download_mbps, wan_path_id) VALUES (?,?,?)",
             (TS, 900.0, pid))
conn.execute("INSERT INTO port_stats (ts, mac, port_idx) VALUES (?,?,?)", (TS, GW, 1))
conn.execute("INSERT INTO device_stats (ts, mac) VALUES (?,?)", (TS, AP))
conn.execute("INSERT INTO ap_radios (ts, ap_mac, band) VALUES (?,?,?)", (TS, AP, "ng"))
conn.execute("INSERT INTO rogue_aps (bssid, ap_mac, updated_at) VALUES (?,?,?)",
             ("de:ad:be:ef:00:01", AP, TS))
conn.execute("INSERT INTO rogue_aps_history (ts, bssid, ap_mac) VALUES (?,?,?)",
             (TS, "de:ad:be:ef:00:01", AP))
conn.commit()

db.delete_device(conn, GW)
db.delete_device(conn, AP)
conn.commit()

def count(sql, *a):
    return conn.execute(sql, a).fetchone()[0]

fails = []
def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")

check("devices emptied", count("SELECT COUNT(*) FROM devices"), 0)
check("wan_paths emptied", count("SELECT COUNT(*) FROM wan_paths"), 0)
check("wan_stats emptied", count("SELECT COUNT(*) FROM wan_stats"), 0)
check("rtt_path_monitors emptied", count("SELECT COUNT(*) FROM rtt_path_monitors"), 0)
check("rtt_monitors emptied", count("SELECT COUNT(*) FROM rtt_monitors"), 0)
check("gateway_stats emptied", count("SELECT COUNT(*) FROM gateway_stats"), 0)
check("speedtest_observations emptied", count("SELECT COUNT(*) FROM speedtest_observations"), 0)
check("port_stats emptied", count("SELECT COUNT(*) FROM port_stats"), 0)
check("device_stats emptied", count("SELECT COUNT(*) FROM device_stats"), 0)
check("ap_radios emptied", count("SELECT COUNT(*) FROM ap_radios"), 0)
check("rogue_aps emptied", count("SELECT COUNT(*) FROM rogue_aps"), 0)
check("rogue_aps_history emptied", count("SELECT COUNT(*) FROM rogue_aps_history"), 0)

# The one row that must survive, with its link cut rather than the row removed.
check("speedtest row kept", count("SELECT COUNT(*) FROM speedtests"), 1)
check("speedtest link nulled",
      count("SELECT COUNT(*) FROM speedtests WHERE wan_path_id IS NULL"), 1)

# Deleting a device that owns nothing must be a no-op, not an error.
db.delete_device(conn, "00:00:00:00:00:99")

print("\n".join(fails) if fails else "ALL CASCADE CHECKS PASS")
sys.exit(1 if fails else 0)
```

- [ ] **Step 5: Run it**

```bash
python3 scratch_cascade_check.py
```

Expected: `ALL CASCADE CHECKS PASS`. If any check fails, fix `unifi_lib/db.py`
— do not edit the check to match the code.

- [ ] **Step 6: Confirm the real database was never touched**

```bash
git status --short unifi_clients.db && echo "unifi_clients.db unmodified"
```

Expected: no output before the echo.

- [ ] **Step 7: Commit**

```bash
git add unifi_lib/db.py live_server.py
git commit -m "feat: add delete_device cascade and absence thresholds"
```

---

### Task 2: The sweep

**Files:**
- Modify: `unifi_lib/db.py` (below `delete_device`)
- Create (temporary, deleted in Task 7): `scratch_sweep_check.py`

**Interfaces:**
- Consumes: `delete_device(db, mac)`, `DEVICE_ABSENT_AFTER_MINUTES`, `DEVICE_ABSENCE_DAYS` from Task 1.
- Produces: `sweep_absent_devices(db: sqlite3.Connection, seen_device_count: int) -> list[str]`, returning the MACs it deleted. Task 3 calls it; Task 4 reads the `status = 'absent'` rows it writes.

- [ ] **Step 1: Write the sweep**

In `unifi_lib/db.py`, directly below `delete_device`:

```python
def sweep_absent_devices(db: sqlite3.Connection, seen_device_count: int) -> list[str]:
    """Mark devices the controller has stopped reporting, then delete the
    ones it stopped reporting a week ago. Returns the MACs deleted.

    Must run *after* persist_devices_and_gateways within the same
    transaction, so every device present in this cycle already carries a
    fresh `updated_at` and cannot be caught by either threshold.

    Absence is stored rather than computed from the live device list because
    `state.last_fast` is None immediately after a restart -- a set-difference
    approach would briefly consider every device absent. `status` self-heals:
    when the device comes back, the normal upsert overwrites 'absent' with
    'online'/'offline' with no special handling anywhere.
    """
    # A controller returning an empty inventory -- expired credentials, an API
    # change, a permissions change -- would otherwise mark every device absent
    # within ten minutes and delete every device and all its history a week
    # later. The grace period alone does not cover this, because the empty
    # response repeats on every cycle. Refusing to act on a zero-device poll
    # is the difference between "the dashboard looks broken until you fix the
    # credentials" and "the dashboard destroyed your history while you were
    # away".
    if seen_device_count <= 0:
        return []

    now = datetime.now(timezone.utc)
    absent_cutoff = (now - timedelta(minutes=DEVICE_ABSENT_AFTER_MINUTES)).isoformat()
    delete_cutoff = (now - timedelta(days=DEVICE_ABSENCE_DAYS)).isoformat()

    db.execute(
        "UPDATE devices SET status = 'absent' WHERE updated_at < ? AND status != 'absent'",
        (absent_cutoff,),
    )

    doomed = [r[0] for r in db.execute(
        "SELECT mac FROM devices WHERE updated_at < ?", (delete_cutoff,)
    ).fetchall()]
    for mac in doomed:
        delete_device(db, mac)
    return doomed
```

- [ ] **Step 2: Write the check script**

Create `scratch_sweep_check.py`:

```python
"""Exercise both thresholds, the zero-poll guard, and self-healing."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone

DB = os.path.join(tempfile.mkdtemp(), "check.db")
os.environ["UNIFI_DB_PATH"] = DB
from unifi_lib import db  # noqa: E402

now = datetime.now(timezone.utc)
FRESH = now.isoformat()
STALE_11M = (now - timedelta(minutes=11)).isoformat()
STALE_8D = (now - timedelta(days=8)).isoformat()

conn = db.connect()
db.init_db(conn)

def seed(mac, status, updated_at):
    conn.execute(
        "INSERT OR REPLACE INTO devices "
        "(mac, name, model, category, status, uptime_sec, ip, updated_at, parent_name) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mac, mac, "M", "ap", status, 1, "10.0.0.9", updated_at, None))

fails = []
def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")

def status_of(mac):
    r = conn.execute("SELECT status FROM devices WHERE mac = ?", (mac,)).fetchone()
    return None if r is None else r[0]

# --- The guard: a zero-device poll must change nothing at all. ---
seed("00:00:00:00:00:01", "online", STALE_8D)
conn.commit()
check("guard deletes nothing", db.sweep_absent_devices(conn, 0), [])
check("guard marks nothing", status_of("00:00:00:00:00:01"), "online")

# --- Fresh device: untouched by either threshold. ---
seed("00:00:00:00:00:02", "online", FRESH)
# --- A powered-off but still-adopted device is refreshed every cycle, so it
#     looks exactly like the fresh one and must survive. ---
seed("00:00:00:00:00:03", "offline", FRESH)
# --- Absent 11 minutes: marked, not deleted. ---
seed("00:00:00:00:00:04", "online", STALE_11M)
conn.commit()

deleted = db.sweep_absent_devices(conn, 3)
conn.commit()

check("stale-8d device deleted", "00:00:00:00:00:01" in deleted, True)
check("stale-8d row gone", status_of("00:00:00:00:00:01"), None)
check("fresh online untouched", status_of("00:00:00:00:00:02"), "online")
check("powered-off survives", status_of("00:00:00:00:00:03"), "offline")
check("11m marked absent", status_of("00:00:00:00:00:04"), "absent")
check("11m row still exists",
      conn.execute("SELECT COUNT(*) FROM devices WHERE mac = ?",
                   ("00:00:00:00:00:04",)).fetchone()[0], 1)

# --- Self-healing: the normal upsert clears 'absent' with no special case. ---
seed("00:00:00:00:00:04", "online", datetime.now(timezone.utc).isoformat())
conn.commit()
check("returning device heals", status_of("00:00:00:00:00:04"), "online")

print("\n".join(fails) if fails else "ALL SWEEP CHECKS PASS")
sys.exit(1 if fails else 0)
```

- [ ] **Step 3: Run it**

```bash
python3 scratch_sweep_check.py
```

Expected: `ALL SWEEP CHECKS PASS`.

- [ ] **Step 4: Commit**

```bash
git add unifi_lib/db.py
git commit -m "feat: sweep devices the controller has stopped reporting"
```

---

### Task 3: Wire the sweep into both persistence paths

**Files:**
- Modify: `live_server.py:376-381` (inside `persist_loop`)
- Modify: `poll_unifi.py:37-43`

**Interfaces:**
- Consumes: `db.sweep_absent_devices(conn, seen_device_count)` from Task 2.
- Produces: nothing new. Task 4 depends on this task having run so that `status = 'absent'` rows actually exist.

- [ ] **Step 1: Call the sweep in `persist_loop`**

In `live_server.py`, the block currently reads:

```python
            persist.persist_devices_and_gateways(conn, fast["devices"], ts)
            wan_rows = persist.persist_wan_stats(conn, fast["devices"], ts)
            db.prune_old(conn)
            conn.commit()
```

Change it to:

```python
            persist.persist_devices_and_gateways(conn, fast["devices"], ts)
            wan_rows = persist.persist_wan_stats(conn, fast["devices"], ts)
            # After the upsert, so devices seen this cycle carry a fresh
            # updated_at and cannot be mistaken for absent. The count is the
            # guard: a zero-device poll is a controller problem, not eighteen
            # simultaneous removals.
            removed = db.sweep_absent_devices(conn, len(fast["devices"]))
            db.prune_old(conn)
            conn.commit()
            if removed:
                log.info("Removed %d device(s) absent for %d+ days: %s",
                         len(removed), db.DEVICE_ABSENCE_DAYS, ", ".join(removed))
```

- [ ] **Step 2: Call the sweep in `poll_unifi.py`**

The block currently reads:

```python
    persist.persist_devices_and_gateways(conn, fast["devices"], ts)
    st_count = persist.persist_speedtests(conn, speedtests)
    rogue_count = persist.persist_rogue_aps(conn, rogue, ts)
    db.prune_old(conn)
```

Change it to:

```python
    persist.persist_devices_and_gateways(conn, fast["devices"], ts)
    st_count = persist.persist_speedtests(conn, speedtests)
    rogue_count = persist.persist_rogue_aps(conn, rogue, ts)
    removed = db.sweep_absent_devices(conn, len(fast["devices"]))
    db.prune_old(conn)
```

and add to the existing `log.info` call after `conn.close()`:

```python
    if removed:
        log.info("Removed %d device(s) absent for %d+ days: %s",
                 len(removed), db.DEVICE_ABSENCE_DAYS, ", ".join(removed))
```

- [ ] **Step 3: Verify both modules still import**

```bash
python3 -c "import ast,sys; [ast.parse(open(f).read(), f) for f in ('live_server.py','poll_unifi.py')]; print('both parse')"
```

Expected: `both parse`.

- [ ] **Step 4: Verify against the live controller**

```bash
UNIFI_DB_PATH=/tmp/device-removal-t3.db uv run --python 3.14 \
  --with unifi-core --with aiounifi --with aiohttp python3 -c "
import asyncio
from unifi_lib import db, persist
from unifi_lib.fetch import UnifiSession
async def main():
    s = UnifiSession(); await s.ensure_connected()
    fast = await s.fetch_fast()
    conn = db.connect(); db.init_db(conn)
    ts = persist.now_iso()
    persist.persist_devices_and_gateways(conn, fast['devices'], ts)
    removed = db.sweep_absent_devices(conn, len(fast['devices']))
    conn.commit()
    n = conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
    absent = conn.execute(\"SELECT COUNT(*) FROM devices WHERE status='absent'\").fetchone()[0]
    print(f'persisted={n} removed={len(removed)} absent={absent}')
    await s.close()
asyncio.run(main())
"; rm -f /tmp/device-removal-t3.db*
```

Expected: `persisted=18 removed=0 absent=0` — every device was just seen, so
nothing is stale. A non-zero `removed` or `absent` here means the sweep is
running before the upsert, or reading the wrong column.

- [ ] **Step 5: Commit**

```bash
git add live_server.py poll_unifi.py
git commit -m "feat: run the absent-device sweep in both persistence paths"
```

---

### Task 4: Merge absent devices into the tick

**Files:**
- Modify: `live_server.py:78` (the `State` class)
- Modify: `live_server.py:325-333` (end of the device loop in `fast_loop`)
- Modify: `live_server.py:376-381` (refresh the cache in `persist_loop`)

**Interfaces:**
- Consumes: the `status = 'absent'` rows written by Task 2, via Task 3.
- Produces: `state.absent_devices: list[dict]`, each shaped exactly like a live `tick.devices` entry. Task 5 prunes this list on manual delete; Task 6 renders from it.

- [ ] **Step 1: Add the cache to `State`**

In `live_server.py`, below `self.wan_path_ids` (line 78):

```python
        # Devices the sweep has marked absent, shaped like a tick entry and
        # refreshed once per persist_loop cycle (60s). fast_loop builds
        # tick["devices"] purely from the live controller fetch, so a device
        # forgotten on the controller drops out of the table the instant it
        # is removed -- never rendering as absent, and never offering its own
        # delete button. This cache merges those rows back in without a DB
        # read on every 1s tick, the same trade-off as wan_path_ids above.
        self.absent_devices: list[dict] = []
```

- [ ] **Step 2: Merge the cache into the tick**

In `fast_loop`, immediately after the `for raw in fast["devices"]:` loop ends
and before `tick = {`:

```python
            # Absent devices are appended, never merged over a live entry: if
            # a device reappears mid-cycle it is in fast["devices"] already,
            # and the cache is up to 60s stale.
            live_macs = {d["mac"] for d in devices}
            devices.extend(d for d in state.absent_devices if d["mac"] not in live_macs)
```

- [ ] **Step 3: Refresh the cache in `persist_loop`**

In `persist_loop`, after `conn.commit()` and beside the existing
`state.vendor_cache` refresh:

```python
            state.absent_devices = [
                {"mac": r[0], "name": r[1], "model": r[2], "category": r[3],
                 "status": "absent", "uptimeSec": None, "ip": r[4],
                 "parent": r[5], "parentMac": None,
                 "rxBps": None, "txBps": None, "ports": []}
                for r in conn.execute(
                    "SELECT mac, name, model, category, ip, parent_name "
                    "FROM devices WHERE status = 'absent'"
                ).fetchall()
            ]
```

Every live metric is `None` and `ports` is `[]` because nothing is reporting
them any more. `fmtUptime`/`fmtBits` already render `null` as `—`.

- [ ] **Step 4: Verify the merge shape**

```bash
UNIFI_DB_PATH=/tmp/device-removal-t4.db python3 -c "
import os, sqlite3
from unifi_lib import db
conn = db.connect(); db.init_db(conn)
conn.execute('INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)',
  ('aa:bb:cc:00:00:01','Ghost-AP','U7','ap','absent',None,'10.0.0.9',
   '2026-08-20T00:00:00+00:00',None))
conn.commit()
absent = [
    {'mac': r[0], 'name': r[1], 'model': r[2], 'category': r[3],
     'status': 'absent', 'uptimeSec': None, 'ip': r[4],
     'parent': r[5], 'parentMac': None,
     'rxBps': None, 'txBps': None, 'ports': []}
    for r in conn.execute('SELECT mac, name, model, category, ip, parent_name '
                          \"FROM devices WHERE status = 'absent'\").fetchall()
]
live = [{'mac': 'aa:bb:cc:00:00:02', 'status': 'online'}]
live_macs = {d['mac'] for d in live}
live.extend(d for d in absent if d['mac'] not in live_macs)
assert len(live) == 2, live
assert live[1]['status'] == 'absent' and live[1]['name'] == 'Ghost-AP'
assert live[1]['rxBps'] is None and live[1]['ports'] == []
# A device that is BOTH live and cached absent must not be duplicated.
live2 = [{'mac': 'aa:bb:cc:00:00:01', 'status': 'online'}]
lm2 = {d['mac'] for d in live2}
live2.extend(d for d in absent if d['mac'] not in lm2)
assert len(live2) == 1 and live2[0]['status'] == 'online', live2
print('MERGE SHAPE OK — absent appended, live never shadowed')
"; rm -f /tmp/device-removal-t4.db*
```

Expected: `MERGE SHAPE OK — absent appended, live never shadowed`.

- [ ] **Step 5: Commit**

```bash
git add live_server.py
git commit -m "feat: merge absent devices into the live tick"
```

---

### Task 5: `DELETE /api/devices/{mac}`

**Files:**
- Modify: `live_server.py` (new handler beside `handle_devices`, line 1397)
- Modify: `live_server.py:1477` (route registration)

**Interfaces:**
- Consumes: `db.delete_device(conn, mac)` from Task 1; `state.absent_devices` from Task 4.
- Produces: `DELETE /api/devices/{mac}` returning `{"deleted": mac}` on 200, `{"error": ...}` on 404/409. Task 6 calls this endpoint.

- [ ] **Step 1: Write the handler**

In `live_server.py`, directly below `handle_devices`:

```python
async def handle_device_delete(request):
    """Remove an absent device and its history on request.

    The `absent` precondition is the safety property, not a convenience. This
    dashboard is published to the LAN without authentication and every other
    route is read-only, so this is the one request that can destroy data. A
    device the controller has stopped reporting for DEVICE_ABSENT_AFTER_MINUTES
    is the only thing it is allowed to touch; a live device's history cannot be
    reached through it at all.
    """
    mac = request.match_info["mac"]
    conn = db.connect()
    try:
        row = conn.execute("SELECT status FROM devices WHERE mac = ?", (mac,)).fetchone()
        if row is None:
            return web.json_response({"error": "unknown device"}, status=404)
        if row[0] != "absent":
            return web.json_response(
                {"error": "device is not absent", "status": row[0]}, status=409)
        db.delete_device(conn, mac)
        conn.commit()
    finally:
        conn.close()

    # Drop it from the tick cache too, or it reappears for up to 60s until
    # the next persist cycle rebuilds the list.
    state.absent_devices = [d for d in state.absent_devices if d["mac"] != mac]
    log.info("Device %s deleted by request", mac)
    return web.json_response({"deleted": mac})
```

- [ ] **Step 2: Register the route**

In `live_server.py`, directly below the `add_get("/api/devices", ...)` line:

```python
    app.router.add_delete("/api/devices/{mac}", handle_device_delete)
```

- [ ] **Step 3: Start a verification server on 8791**

```bash
UNIFI_DB_PATH=/tmp/device-removal-t5.db BIND_PORT=8791 \
  uv run --python 3.14 --with unifi-core --with aiounifi --with aiohttp \
  python3 live_server.py > /tmp/t5-server.log 2>&1 &
sleep 8 && echo "started"
```

- [ ] **Step 4: Seed one absent and one online device, then exercise all three responses**

```bash
python3 -c "
import os
os.environ['UNIFI_DB_PATH'] = '/tmp/device-removal-t5.db'
from unifi_lib import db
conn = db.connect(); db.init_db(conn)
for mac, st in (('aa:bb:cc:00:00:01','absent'), ('aa:bb:cc:00:00:02','online')):
    conn.execute('INSERT OR REPLACE INTO devices VALUES (?,?,?,?,?,?,?,?,?)',
                 (mac, mac, 'U7', 'ap', st, 1, '10.0.0.9',
                  '2026-08-20T00:00:00+00:00', None))
conn.commit()
print('seeded')
"
echo "--- 404 unknown ---"
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:8791/api/devices/de:ad:be:ef:00:00
echo "--- 409 online device is protected ---"
curl -s -w " <- %{http_code}\n" -X DELETE http://127.0.0.1:8791/api/devices/aa:bb:cc:00:00:02
echo "--- 200 absent device is removed ---"
curl -s -w " <- %{http_code}\n" -X DELETE http://127.0.0.1:8791/api/devices/aa:bb:cc:00:00:01
```

Expected exactly:
- `404`
- `{"error": "device is not absent", "status": "online"} <- 409`
- `{"deleted": "aa:bb:cc:00:00:01"} <- 200`

The 409 is the important one. If an online device can be deleted, stop and fix
the precondition before going further.

- [ ] **Step 5: Confirm the online device survived and stop the server**

```bash
python3 -c "
import os
os.environ['UNIFI_DB_PATH'] = '/tmp/device-removal-t5.db'
from unifi_lib import db
conn = db.connect()
rows = conn.execute('SELECT mac, status FROM devices ORDER BY mac').fetchall()
assert rows == [('aa:bb:cc:00:00:02','online')], rows
print('ONLY THE ABSENT DEVICE WAS DELETED')
"
pkill -f "live_server.py" ; rm -f /tmp/device-removal-t5.db* /tmp/t5-server.log
```

Expected: `ONLY THE ABSENT DEVICE WAS DELETED`.

- [ ] **Step 6: Commit**

```bash
git add live_server.py
git commit -m "feat: add DELETE /api/devices/{mac} for absent devices"
```

---

### Task 6: The trashcan

**Files:**
- Modify: `static/live_dashboard.html:105` (status dot colour)
- Modify: `static/live_dashboard.html:359` (table header)
- Modify: `static/live_dashboard.html:1489-1500` (row markup and `colspan`)
- Modify: `static/live_dashboard.html:1521-1528` (click handlers)

**Interfaces:**
- Consumes: `status: "absent"` entries in `tick.devices` from Task 4; `DELETE /api/devices/{mac}` from Task 5.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Give the absent dot its own colour**

At `static/live_dashboard.html:105`, extend the existing rule:

```css
  .status-dot.online { background: var(--good); } .status-dot.offline { background: var(--critical); }
  .status-dot.absent { background: var(--warning); }
  .icon-btn { background: none; border: none; padding: 2px 4px; cursor: pointer; font-size: 13px; line-height: 1; color: var(--text-muted); border-radius: 4px; }
  .icon-btn:hover { color: var(--critical); background: var(--grid); }
```

The label needs no work: `${d.status[0].toUpperCase()+d.status.slice(1)}`
already renders `absent` as "Absent".

- [ ] **Step 2: Add the header cell**

At `static/live_dashboard.html:359`, after the `data-key="txBps"` header, add a
non-sortable trailing cell:

```html
      <th style="width:1%;"></th>
```

- [ ] **Step 3: Add the action cell and fix the colspan**

In `renderDeviceTable`, after the `${fmtBits(d.txBps)}` cell:

```html
      <td>${d.status === "absent" ? `<button class="icon-btn device-delete" data-del-mac="${d.mac}" title="Delete this device and all its history">🗑</button>` : ""}</td>
```

In the same function, the drill row's `colspan="9"` becomes `colspan="10"`.
Missing this silently breaks the port-stats layout.

- [ ] **Step 4: Wire the button**

In the existing row-click handler, add a guard beside the `.device-link` one:

```javascript
    tr.addEventListener("click", e => {
      if (e.target.closest(".device-link")) return;
      if (e.target.closest(".device-delete")) return;
      const mac = tr.dataset.mac;
      openDeviceRow = openDeviceRow === mac ? null : mac;
      renderDeviceTable(devices);
    });
```

and register the button's own handler beside the `.device-link` block:

```javascript
  document.querySelectorAll("#ov-device-tbody .device-delete").forEach(el => {
    el.addEventListener("click", async e => {
      e.stopPropagation();
      const mac = el.dataset.delMac;
      const dev = (lastDevices || []).find(d => d.mac === mac);
      const label = dev && dev.name ? `${dev.name} (${mac})` : mac;
      if (!confirm(`Delete ${label} and all of its stored history?\n\nThis cannot be undone.`)) return;
      el.disabled = true;
      const res = await fetch(`/api/devices/${encodeURIComponent(mac)}`, { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        el.disabled = false;
        alert(`Could not delete ${label}: ${body.error || res.status}`);
        return;
      }
      lastDevices = (lastDevices || []).filter(d => d.mac !== mac);
      if (openDeviceRow === mac) openDeviceRow = null;
      renderDeviceTable(lastDevices);
    });
  });
```

- [ ] **Step 5: Verify in a browser**

```bash
UNIFI_DB_PATH=/tmp/device-removal-t6.db BIND_PORT=8791 \
  uv run --python 3.14 --with unifi-core --with aiounifi --with aiohttp \
  python3 live_server.py > /tmp/t6-server.log 2>&1 &
sleep 20 && python3 -c "
import os
os.environ['UNIFI_DB_PATH'] = '/tmp/device-removal-t6.db'
from unifi_lib import db
conn = db.connect()
conn.execute('UPDATE devices SET status = ? WHERE mac = (SELECT mac FROM devices LIMIT 1)',
             ('absent',))
conn.commit()
print('one device forced absent — wait 60s for the persist cycle to cache it')
"
```

Open `http://127.0.0.1:8791`, go to the Devices tab, and confirm all of:

1. The forced-absent row shows an amber dot and the label "Absent".
2. That row, and only that row, shows a trashcan.
3. Clicking the trashcan opens a confirm dialog naming the device; cancelling
   changes nothing.
4. Clicking the row *body* still opens its port-stats drill, and the drill
   panel spans the full table width (this is the `colspan` check).
5. Confirming the dialog removes the row immediately.

Then stop the server:

```bash
pkill -f "live_server.py" ; rm -f /tmp/device-removal-t6.db* /tmp/t6-server.log
```

- [ ] **Step 6: Commit**

```bash
git add static/live_dashboard.html
git commit -m "feat: delete an absent device from the devices table"
```

---

### Task 7: Documentation and final review

**Files:**
- Modify: `CLAUDE.md` ("Database growth" section)
- Delete: `scratch_cascade_check.py`, `scratch_sweep_check.py`

- [ ] **Step 1: Document the behaviour in `CLAUDE.md`**

Add to the "Database growth" section:

```markdown
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
```

- [ ] **Step 2: Delete the scratch scripts**

```bash
rm -f scratch_cascade_check.py scratch_sweep_check.py
```

- [ ] **Step 3: Confirm no scratch files or databases are staged**

```bash
git status --short
```

Expected: only `CLAUDE.md` modified. No `*.db`, no `scratch_*.py`, no `*.log`.

- [ ] **Step 4: Request a final review**

Use `superpowers:requesting-code-review` against the full diff for this
branch. Direct the reviewer's attention at these five in particular:

1. **The zero-poll guard.** Can any path reach `delete_device` when the
   controller returned no devices?
2. **Ordering.** Is `sweep_absent_devices` called after
   `persist_devices_and_gateways` in *both* `live_server.py` and
   `poll_unifi.py`, and inside the same transaction?
3. **The 409.** Can a device that is not `absent` be deleted through the
   endpoint by any route, including a MAC with unusual characters?
4. **Cascade completeness.** Is every table keyed on a device MAC or on a
   `wan_path_id` covered, and is `speedtests` the only survivor?
5. **The `colspan`.** Does the device drill row span all ten columns?

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record device absence and removal behaviour"
```
