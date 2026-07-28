# WAN Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every network-specific assumption so the dashboard runs correctly on any UniFi network, replacing hardcoded WAN labels, a latency-threshold heuristic, and a two-slot primary/cellular model with API-derived WAN Paths carrying stable synthetic identities.

**Architecture:** A pure discovery module turns a gateway's raw dict into WAN Paths. A conservative matcher assigns each path a stable synthetic id stored in `wan_paths`, so history follows the internet service rather than the socket. Per-sample WAN data moves to `wan_stats` keyed on that id, device metrics stay in `gateway_stats`, and speedtests are attributed by observing `speedtest-status` in `fast_loop` rather than by guessing from latency.

**Tech Stack:** Python 3.14, aiohttp, SQLite (WAL), vanilla JS. No build step, no test framework.

**Spec:** `docs/superpowers/specs/2026-07-27-wan-portability-design.md`
**Domain terms:** `CONTEXT.md` — WAN Path, Gateway Device, Cellular Modem, Link Type, Speedtest Attribution
**Identity decision:** `docs/adr/0001-wan-path-identity.md`

## Global Constraints

- **A WAN Path is not a Gateway Device.** WAN-scoped data (throughput, RTT, speedtest, usage, ISP, status) is keyed on `wan_path_id`. Device-scoped data (CPU, memory, load, temps, uptime, carrier) stays keyed on `gateway_mac`. Never merge them.
- **`is_cellular` derives from `link_type` only** (`wireless_5g`, `wireless_lte`, any `wireless_*`), never from a WAN key name. `WAN3` is not inherently cellular.
- **Matching never merges under doubt.** Ambiguity always creates a new path. A wrong split is visible and correctable; a wrong merge is irreversible.
- **No heuristic speedtest classification.** `CELLULAR_LATENCY_THRESHOLD_MS` is deleted. Unattributable speedtests stay `NULL`.
- **Migrations are additive only** — `CREATE TABLE IF NOT EXISTS`, then `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`. A restart against an older DB must remain safe. Dead columns stay in place.
- **Every time-series table gets a `prune_old()` line.** `wan_paths` is identity, not time series — it is **not** pruned.
- **`*_bytes_total` columns are monotonic cumulative counters.** Never write per-interval deltas into them.
- **No test framework exists and none is to be introduced.** Verification is executable shell commands, matching the codebase idiom (see CLAUDE.md "Running").
- All timestamps are UTC. Never commit `*.db`, `*.log`, `.env`, `data/`, `__pycache__`.
- Run all commands from `/Users/hector/Projects/.unifi-dashboard`. Branch: `wan-portability`.

---

### Task 1: WAN discovery module

**Files:**
- Create: `unifi_lib/wan.py`
- Create (temporary, deleted in final step): `scratch_wan_check.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WanPath` frozen dataclass with fields `key, slot, ifname, link_type, is_cellular, isp_name, asn, ip, status, label`; and `discover_wan_paths(raw: dict) -> list[WanPath]`. Tasks 3, 4, 6 all import these exact names.

- [ ] **Step 1: Write the module**

```python
"""Turn a Gateway Device's raw controller dict into the WAN Paths it has.

Pure: no controller calls, no database. Everything comes from fields the
controller already returns, so this works on any UniFi network rather than
assuming this one's layout.
"""
from dataclasses import dataclass

# A WAN is cellular when the controller says its link is wireless, never
# because of its key name -- on some networks the cellular path is WAN2,
# and here WAN3 is a GRE tunnel reported as wireless_5g.
_CELLULAR_PREFIX = "wireless"


@dataclass(frozen=True)
class WanPath:
    key: str
    slot: str
    ifname: str
    link_type: str
    is_cellular: bool
    isp_name: str
    asn: int | None
    ip: str | None
    status: str
    label: str


def _slot_for(key: str) -> str:
    """"WAN" -> "wan1", "WAN3" -> "wan3". The controller's own convention."""
    suffix = key[3:]
    return f"wan{suffix}" if suffix.isdigit() else "wan1"


def discover_wan_paths(raw: dict) -> list[WanPath]:
    """WAN Paths for one gateway, in the controller's inventory order.

    Returns [] when the gateway reports no WAN interfaces -- callers must
    handle that rather than assuming at least one path exists.
    """
    inventory = raw.get("last_wan_interfaces") or {}
    statuses = raw.get("last_wan_status") or {}
    geo = raw.get("active_geo_info") or {}
    paths = []
    for key in inventory:
        slot = _slot_for(key)
        wan = raw.get(slot) or {}
        info = geo.get(key) or {}
        ifname = wan.get("ifname") or ""
        link_type = wan.get("type") or ""
        isp_name = info.get("isp_name") or ""
        paths.append(WanPath(
            key=key,
            slot=slot,
            ifname=ifname,
            link_type=link_type,
            is_cellular=link_type.startswith(_CELLULAR_PREFIX),
            isp_name=isp_name,
            asn=info.get("asn"),
            ip=(inventory.get(key) or {}).get("ip"),
            status=statuses.get(key) or "unknown",
            # ISP name is the most meaningful label; fall back to the
            # interface, then the key, so a failed geo lookup still names
            # the path rather than rendering blank.
            label=isp_name or ifname or key,
        ))
    return paths
```

- [ ] **Step 2: Write the verification script**

Create `scratch_wan_check.py`:

```python
"""Verification for Task 1. Not committed."""
from unifi_lib.wan import discover_wan_paths, WanPath

TWO_WAN = {
    "last_wan_interfaces": {"WAN": {"alive": True, "ip": "24.94.8.107"},
                            "WAN3": {"alive": True, "ip": "33.151.72.44"}},
    "last_wan_status": {"WAN": "online", "WAN3": "online"},
    "active_geo_info": {"WAN": {"isp_name": "Spectrum", "asn": 20115},
                        "WAN3": {"isp_name": "T-Mobile USA", "asn": 21928}},
    "wan1": {"ifname": "eth9", "type": "ethernet", "up": True},
    "wan3": {"ifname": "gre1", "type": "wireless_5g", "up": True},
}
ONE_WAN = {
    "last_wan_interfaces": {"WAN": {"ip": "1.2.3.4"}},
    "last_wan_status": {"WAN": "online"},
    "active_geo_info": {"WAN": {"isp_name": "Example ISP", "asn": 64500}},
    "wan1": {"ifname": "eth0", "type": "ethernet"},
}
THREE_WAN = {
    "last_wan_interfaces": {"WAN": {}, "WAN2": {}, "WAN3": {}},
    "last_wan_status": {"WAN": "online", "WAN2": "offline", "WAN3": "online"},
    "active_geo_info": {"WAN": {"isp_name": "A", "asn": 1}},
    "wan1": {"ifname": "eth0", "type": "ethernet"},
    "wan2": {"ifname": "eth1", "type": "wireless_lte"},
    "wan3": {"ifname": "eth2", "type": "ethernet"},
}
NO_WAN = {"last_wan_interfaces": {}}

fails = []
def check(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        fails.append(name)

p = discover_wan_paths(TWO_WAN)
check("two-wan: count", len(p) == 2, f"got {len(p)}")
check("two-wan: keys", [x.key for x in p] == ["WAN", "WAN3"], [x.key for x in p])
check("two-wan: slots", [x.slot for x in p] == ["wan1", "wan3"], [x.slot for x in p])
check("two-wan: labels from ISP",
      [x.label for x in p] == ["Spectrum", "T-Mobile USA"], [x.label for x in p])
check("two-wan: WAN is not cellular", p[0].is_cellular is False)
check("two-wan: WAN3 is cellular (wireless_5g, not key name)", p[1].is_cellular is True)
check("two-wan: asn carried", [x.asn for x in p] == [20115, 21928])

p = discover_wan_paths(ONE_WAN)
check("one-wan: count", len(p) == 1, f"got {len(p)}")
check("one-wan: not cellular", p[0].is_cellular is False)

p = discover_wan_paths(THREE_WAN)
check("three-wan: count", len(p) == 3, f"got {len(p)}")
check("three-wan: WAN2 cellular via wireless_lte", p[1].is_cellular is True)
check("three-wan: WAN3 ethernet is NOT cellular", p[2].is_cellular is False)
check("three-wan: missing geo falls back to ifname",
      [x.label for x in p] == ["A", "eth1", "eth2"], [x.label for x in p])
check("three-wan: offline status carried", p[1].status == "offline", p[1].status)

check("no-wan: empty list", discover_wan_paths(NO_WAN) == [])
check("no-wan: absent key also empty", discover_wan_paths({}) == [])

print("---")
print(f"{'FAILED: ' + ', '.join(fails) if fails else 'all checks passed'}")
raise SystemExit(1 if fails else 0)
```

The three-WAN case is the one that proves portability: `WAN2` is cellular because
its link is `wireless_lte`, and `WAN3` is *not* cellular despite its name.

- [ ] **Step 3: Run it — expect failure first**

```bash
python3 scratch_wan_check.py
```

Expected before the module exists: `ModuleNotFoundError`. If you wrote Step 1
first, expect all checks to pass. If any check fails, fix `unifi_lib/wan.py` — do
not edit the check to match the code.

- [ ] **Step 4: Verify against the live controller**

```bash
uv run --python 3.14 --with unifi-core --with aiounifi python3 -c "
import asyncio
from unifi_lib.fetch import UnifiSession
from unifi_lib.wan import discover_wan_paths
async def main():
    s = UnifiSession(); await s.ensure_connected()
    d = next(x for x in (await s.fetch_fast())['devices'] if x.get('type') in ('udm','ugw','usg'))
    for p in discover_wan_paths(d):
        print(f'{p.key:5} {p.slot:5} {p.ifname:6} {p.link_type:14} cell={p.is_cellular!s:5} {p.label}')
    await s.close()
asyncio.run(main())
"
```

Expected exactly two lines: `WAN` / `wan1` / `eth9` / `ethernet` / `cell=False` /
`Spectrum`, and `WAN3` / `wan3` / `gre1` / `wireless_5g` / `cell=True` /
`T-Mobile USA`.

- [ ] **Step 5: Commit**

```bash
rm scratch_wan_check.py
git add unifi_lib/wan.py
git commit -m "feat: discover WAN paths from controller data

Pure module turning a gateway's raw dict into the WAN Paths it actually
has, using last_wan_interfaces as the inventory, active_geo_info for ISP
identity, and wan.type to decide whether a path is cellular.

Nothing keys off a WAN's name: a cellular path on WAN2 classifies
correctly and a wired backup on WAN3 correctly does not. Verified
against saved one/two/three-WAN payloads and the live controller."
```

---

### Task 2: Schema for WAN paths, samples and speedtest observations

**Files:**
- Modify: `unifi_lib/db.py` — `init_db()` and `prune_old()` (line 281)

**Interfaces:**
- Consumes: nothing.
- Produces: tables `wan_paths`, `wan_stats`, `speedtest_observations`, and a nullable `speedtests.wan_path_id` column. Tasks 3, 4, 5, 6 read and write these.

- [ ] **Step 1: Add the tables in `init_db()`**

Add alongside the existing `CREATE TABLE IF NOT EXISTS` blocks:

```python
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS wan_paths (
            id INTEGER PRIMARY KEY,
            gateway_mac TEXT NOT NULL,
            wan_key TEXT NOT NULL,
            ifname TEXT,
            link_type TEXT,
            isp_name TEXT,
            asn INTEGER,
            label_override TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS wan_stats (
            ts TEXT NOT NULL,
            wan_path_id INTEGER NOT NULL,
            status TEXT,
            rx_rate_bps INTEGER,
            tx_rate_bps INTEGER,
            rx_bytes_total INTEGER,
            tx_bytes_total INTEGER,
            latency_ms REAL,
            PRIMARY KEY (ts, wan_path_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_wan_stats_path_ts ON wan_stats(wan_path_id, ts)")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS speedtest_observations (
            observed_at TEXT NOT NULL,
            ifname TEXT NOT NULL,
            xput_download REAL,
            xput_upload REAL,
            PRIMARY KEY (ifname, xput_download, xput_upload)
        )
        """
    )
```

`rx_bytes_total`/`tx_bytes_total` are monotonic cumulative counters, matching the
existing convention — never write per-interval deltas into them.

- [ ] **Step 2: Add the additive column migration**

Following the existing `PRAGMA table_info` pattern used for other tables:

```python
    existing_st_cols = {row[1] for row in db.execute("PRAGMA table_info(speedtests)").fetchall()}
    if "wan_path_id" not in existing_st_cols:
        db.execute("ALTER TABLE speedtests ADD COLUMN wan_path_id INTEGER")
```

The existing `source` column stays in place per the additive rule; Task 5 stops
writing it.

- [ ] **Step 3: Add pruning**

In `prune_old()`, after the `speedtests` delete at line 288:

```python
    db.execute("DELETE FROM wan_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM speedtest_observations WHERE observed_at < ?", (cutoff,))
```

**Do not prune `wan_paths`** — it is identity, not time series. Pruning it would
orphan `wan_stats` rows and destroy the history continuity the whole design
exists to provide.

- [ ] **Step 4: Verify the migration is safe and idempotent**

```bash
uv run --python 3.14 --with unifi-core --with aiounifi python3 -c "
import os, tempfile
os.environ['UNIFI_DB_PATH'] = tempfile.mktemp(suffix='.db')
from unifi_lib import db
c = db.connect(); db.init_db(c)
db.init_db(c)   # second call must be a no-op, proving idempotence
tables = {r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")}
for t in ('wan_paths','wan_stats','speedtest_observations'):
    assert t in tables, f'missing {t}'
cols = {r[1] for r in c.execute('PRAGMA table_info(speedtests)')}
assert 'wan_path_id' in cols, cols
assert 'source' in cols, 'source column must be kept'
db.prune_old(c)
print('PASS: tables created, migration idempotent, prune_old runs')
"
```

Then confirm it is safe against a **real existing** database — the NAS has live
data and a restart must not break it:

```bash
cp /Users/hector/Projects/.unifi-dashboard/unifi_clients.db /tmp/migrate-test.db
UNIFI_DB_PATH=/tmp/migrate-test.db uv run --python 3.14 --with unifi-core --with aiounifi python3 -c "
from unifi_lib import db
c = db.connect(); db.init_db(c); db.prune_old(c)
print('rows still readable:', c.execute('select count(*) from gateway_stats').fetchone()[0])
print('PASS: migrates an existing 240MB database without error')
"
rm -f /tmp/migrate-test.db
```

- [ ] **Step 5: Commit**

```bash
git add unifi_lib/db.py
git commit -m "feat: add wan_paths, wan_stats and speedtest_observations tables

wan_paths holds synthetic WAN Path identities so history follows the
internet service rather than the socket it is plugged into. wan_stats
holds per-sample WAN data keyed on that id; gateway_stats keeps
device-level metrics. speedtest_observations persists observed
speedtest-status readings so attribution survives a restart.

wan_paths is deliberately excluded from prune_old -- it is identity, not
time series, and pruning it would orphan wan_stats history. Verified
idempotent and safe against the existing 240MB production database."
```

---

### Task 3: WAN Path identity matching

**Files:**
- Create: `unifi_lib/wan_identity.py`
- Create (temporary, deleted in final step): `scratch_identity_check.py`

**Interfaces:**
- Consumes: `WanPath` from `unifi_lib.wan` (Task 1); `wan_paths` table (Task 2).
- Produces: `resolve_path_ids(db, gateway_mac: str, paths: list[WanPath], ts: str) -> list[int]`, returning ids positionally aligned with `paths`. Task 4 calls it.

**The interface is batch, not per-path, and that is load-bearing.** Two WAN Paths
sharing an ASN can only be told apart by knowing which candidate rows have
already been claimed *in this same cycle*. A per-path signature cannot know that:
resolving the second path sees the first as a lone, uncorroborated ASN match and
merges into it — the irreversible failure ADR 0001 exists to prevent. Within one
call, an id claimed by an earlier path in the batch is removed from the candidate
pool for every later one.

Implements the matching rule from `docs/adr/0001-wan-path-identity.md`.

- [ ] **Step 1: Write the module**

> **⚠️ The code in this step and the checks in Step 2 are SUPERSEDED.** They use
> the per-path `resolve_path_id(db, mac, path, ts)` signature, which was found
> during implementation to merge two paths sharing an ASN — see the Interfaces
> note above and ADR 0001. The shipped module exposes
> `resolve_path_ids(db, gateway_mac, paths, ts) -> list[int]` and resolves a
> gateway's paths as a batch. Read the code below for the *matching rule*, which
> is unchanged; take the *signature* from the Interfaces note. Step 2's checks
> must additionally cover a fresh gateway whose first two paths share an ASN —
> the case the original script missed, because by the time it reached its
> duplicate-ISP checks the gateway already had three paths.

```python
"""Assign each observed WAN Path a stable synthetic identity.

The controller's WAN key is a slot, not a service: re-cabling a link would
split its history, and swapping ISPs on a slot would silently blend two
different services into one chart. Matching on the provider instead means
history follows the internet service.

The rule fails toward splitting. A wrong split shows two series where one
was expected -- visible, and correctable via label_override. A wrong merge
blends two services irreversibly, because the rows no longer record which
was which. See docs/adr/0001-wan-path-identity.md.
"""
import sqlite3

from .wan import WanPath


def resolve_path_id(db: sqlite3.Connection, gateway_mac: str, path: WanPath, ts: str) -> int:
    """Return the wan_paths.id for this sighting, creating a row if new."""
    match_id = _find_match(db, gateway_mac, path)
    if match_id is None:
        cur = db.execute(
            "INSERT INTO wan_paths (gateway_mac, wan_key, ifname, link_type, isp_name, asn, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (gateway_mac, path.key, path.ifname, path.link_type, path.isp_name, path.asn, ts, ts),
        )
        return cur.lastrowid
    db.execute(
        "UPDATE wan_paths SET wan_key = ?, ifname = ?, link_type = ?, isp_name = ?, "
        "asn = ?, last_seen = ? WHERE id = ?",
        (path.key, path.ifname, path.link_type, path.isp_name, path.asn, ts, match_id),
    )
    return match_id


def _find_match(db: sqlite3.Connection, gateway_mac: str, path: WanPath) -> int | None:
    if path.asn is not None:
        rows = db.execute(
            "SELECT id, wan_key, ifname FROM wan_paths WHERE gateway_mac = ? AND asn = ?",
            (gateway_mac, path.asn),
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0]          # survives re-cabling
        if len(rows) > 1:
            # Duplicate ISP on several slots: disambiguate, never guess.
            for row in rows:
                if row[1] == path.key:
                    return row[0]
            for row in rows:
                if row[2] == path.ifname:
                    return row[0]
            return None                # ambiguous -> new path
        return None                    # new ASN on this gateway -> new path

    # No geo data (offline WAN, or lookup failed): the only signals left.
    row = db.execute(
        "SELECT id FROM wan_paths WHERE gateway_mac = ? AND wan_key = ? AND ifname = ?",
        (gateway_mac, path.key, path.ifname),
    ).fetchone()
    return row[0] if row else None
```

- [ ] **Step 2: Write the verification script**

Create `scratch_identity_check.py`:

```python
"""Verification for Task 3. Not committed."""
import os, tempfile
os.environ["UNIFI_DB_PATH"] = tempfile.mktemp(suffix=".db")
from unifi_lib import db
from unifi_lib.wan import WanPath
from unifi_lib.wan_identity import resolve_path_id

MAC = "aa:bb:cc:dd:ee:ff"
TS = "2026-07-27T00:00:00+00:00"

def p(key, slot, ifname, asn, isp="ISP", link="ethernet"):
    return WanPath(key=key, slot=slot, ifname=ifname, link_type=link,
                   is_cellular=False, isp_name=isp, asn=asn, ip=None,
                   status="online", label=isp)

fails = []
def check(name, cond, detail=""):
    if cond: print(f"PASS  {name}")
    else: print(f"FAIL  {name}  {detail}"); fails.append(name)

c = db.connect(); db.init_db(c)

a = resolve_path_id(c, MAC, p("WAN", "wan1", "eth9", 20115, "Spectrum"), TS)
check("first sighting creates a path", a == 1, a)

again = resolve_path_id(c, MAC, p("WAN", "wan1", "eth9", 20115, "Spectrum"), TS)
check("same sighting is stable", again == a, again)

recabled = resolve_path_id(c, MAC, p("WAN2", "wan2", "eth8", 20115, "Spectrum"), TS)
check("re-cabling keeps identity (same ASN, new slot)", recabled == a, recabled)

isp_change = resolve_path_id(c, MAC, p("WAN", "wan1", "eth9", 64500, "NewCo"), TS)
check("ISP change creates a NEW path", isp_change != a, isp_change)

dup_a = resolve_path_id(c, MAC, p("WANA", "wan1", "ethA", 999, "Dup"), TS)
dup_b = resolve_path_id(c, MAC, p("WANB", "wan2", "ethB", 999, "Dup"), TS)
check("same ISP on two slots stays two paths", dup_a != dup_b, (dup_a, dup_b))
check("duplicate ISP re-resolves by key",
      resolve_path_id(c, MAC, p("WANB", "wan2", "ethB", 999, "Dup"), TS) == dup_b)

noasn = resolve_path_id(c, MAC, p("WAN9", "wan9", "eth9x", None), TS)
check("no ASN creates a path", noasn not in (a, isp_change), noasn)
check("no ASN re-resolves by key+ifname",
      resolve_path_id(c, MAC, p("WAN9", "wan9", "eth9x", None), TS) == noasn)

other = resolve_path_id(c, "11:22:33:44:55:66", p("WAN", "wan1", "eth9", 20115, "Spectrum"), TS)
check("a different gateway is a different path", other != a, other)

print("---")
print("FAILED: " + ", ".join(fails) if fails else "all checks passed")
raise SystemExit(1 if fails else 0)
```

- [ ] **Step 3: Run it**

```bash
python3 scratch_identity_check.py
```

Expected: all checks pass. If "re-cabling keeps identity" fails, `_find_match` is
matching on the wrong signal. If "ISP change creates a NEW path" fails, it is
matching too loosely — which is the dangerous direction, since a wrong merge is
irreversible.

- [ ] **Step 4: Commit**

```bash
rm scratch_identity_check.py
git add unifi_lib/wan_identity.py
git commit -m "feat: assign WAN Paths stable synthetic identities

Matches each sighting to an existing path on gateway + ASN, preferring
the same WAN key then interface when a provider appears on several slots,
and creating a new path whenever the match is ambiguous.

History therefore follows the internet service across re-cabling, while
an ISP change on an existing slot starts a new series instead of silently
blending two services into one chart. Implements ADR 0001."
```

---

### Task 4: Persist WAN samples

**Files:**
- Modify: `unifi_lib/persist.py` — add `persist_wan_stats()`; `persist_devices_and_gateways()` at line 206 stops writing WAN columns
- Modify: `live_server.py` — `persist_loop()` calls the new function

**Interfaces:**
- Consumes: `discover_wan_paths` (Task 1), `resolve_path_id` (Task 3), `wan_stats` (Task 2).
- Produces: `persist_wan_stats(db, devices: list[dict], ts: str) -> int` returning rows written. Task 6 queries the table it fills.

- [ ] **Step 1: Add the persist function to `unifi_lib/persist.py`**

```python
def persist_wan_stats(db: sqlite3.Connection, devices: list[dict], ts: str) -> int:
    """One wan_stats row per WAN Path per cycle, across every gateway.

    Byte totals are the controller's cumulative counters, kept monotonic so
    _usage_since() can difference them; rates are instantaneous.
    """
    from .wan import discover_wan_paths
    from .wan_identity import resolve_path_ids

    written = 0
    for raw in devices:
        if device_category(raw) not in ("gateway", "cellular_gateway"):
            continue
        mac = raw.get("mac")
        if not mac:
            continue
        # Resolve the whole gateway's paths at once: two paths sharing an ISP
        # can only be distinguished by knowing which rows this cycle already
        # claimed. See Task 3's interface note and ADR 0001.
        paths = discover_wan_paths(raw)
        for path, path_id in zip(paths, resolve_path_ids(db, mac, paths, ts)):
            wan = raw.get(path.slot) or {}
            rx_r, tx_r = _f(wan.get("rx_bytes-r")), _f(wan.get("tx_bytes-r"))
            db.execute(
                "INSERT OR REPLACE INTO wan_stats (ts, wan_path_id, status, rx_rate_bps, "
                "tx_rate_bps, rx_bytes_total, tx_bytes_total, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, path_id, path.status,
                 int(rx_r * 8) if rx_r is not None else None,
                 int(tx_r * 8) if tx_r is not None else None,
                 wan.get("rx_bytes"), wan.get("tx_bytes"),
                 _wan_latency(wan)),
            )
            written += 1
    return written
```

If `_f` is not already module-level in `persist.py`, use the existing float-coercion
helper this file already uses for rate fields rather than adding another.

- [ ] **Step 2: Call it from `persist_loop()` in `live_server.py`**

In `persist_loop`, alongside the existing `persist.persist_devices_and_gateways(conn, fast["devices"], ts)` call:

```python
            wan_rows = persist.persist_wan_stats(conn, fast["devices"], ts)
```

and include `wan_rows` in that loop's existing log line so a cycle writing zero
WAN rows is visible rather than silent.

- [ ] **Step 3: Verify against the live controller**

```bash
uv run --python 3.14 --with unifi-core --with aiounifi python3 -c "
import asyncio, os, tempfile
os.environ['UNIFI_DB_PATH'] = tempfile.mktemp(suffix='.db')
from unifi_lib import db, persist
from unifi_lib.fetch import UnifiSession
async def main():
    s = UnifiSession(); await s.ensure_connected()
    devs = (await s.fetch_fast())['devices']
    c = db.connect(); db.init_db(c)
    ts = persist.now_iso()
    n = persist.persist_wan_stats(c, devs, ts); c.commit()
    print('wan_stats rows written:', n)
    for r in c.execute('SELECT p.id, p.wan_key, p.isp_name, p.link_type, s.status, s.rx_rate_bps '
                       'FROM wan_paths p JOIN wan_stats s ON s.wan_path_id = p.id'):
        print('  ', r)
    assert n == 2, f'expected 2 WAN paths on this network, got {n}'
    # Re-running must reuse the same identities, not create duplicates.
    persist.persist_wan_stats(c, devs, persist.now_iso()); c.commit()
    paths = c.execute('SELECT count(*) FROM wan_paths').fetchone()[0]
    assert paths == 2, f'identity churn: {paths} paths after two cycles'
    print('PASS: 2 paths, stable across cycles')
    await s.close()
asyncio.run(main())
"
```

- [ ] **Step 4: Commit**

```bash
git add unifi_lib/persist.py live_server.py
git commit -m "feat: persist per-WAN samples keyed on path identity

Writes one wan_stats row per WAN Path per cycle across every gateway,
resolving each sighting to its stable identity first. Verified against
the live controller: two paths, reused rather than duplicated on the
second cycle."
```

---

### Task 5: Speedtest attribution by observation

**Files:**
- Modify: `live_server.py` — `fast_loop()` observes `speedtest-status`
- Modify: `unifi_lib/persist.py` — `persist_speedtests()` at line 377; delete `CELLULAR_LATENCY_THRESHOLD_MS` at line 49-53

**Interfaces:**
- Consumes: `speedtest_observations` and `speedtests.wan_path_id` (Task 2); `wan_paths` (Task 3).
- Produces: `record_speedtest_observation(db, raw: dict, ts: str) -> bool` and an attributing `persist_speedtests`.

The controller's archive has no WAN field and `speedtest-status.timestamp` is a
*refresh* time, not a completion time — matching on timestamps attributes nothing.
Tests also run in pairs ~18s apart, so a 60s poll misses half. Hence: observe the
live status every second (already in the `fast_loop` payload at no extra API cost)
and match archives on exact throughput.

- [ ] **Step 1: Add the observer to `unifi_lib/persist.py`**

```python
def record_speedtest_observation(db: sqlite3.Connection, raw: dict, ts: str) -> bool:
    """Record a completed speedtest seen in the gateway's live status.

    speedtest-status names the interface the test ran on -- the only
    authoritative attribution the controller offers. It is a single latest
    value, so it must be sampled often enough to catch back-to-back tests.
    Returns True when this reading was not already recorded.
    """
    st = raw.get("speedtest-status") or {}
    ifname, down, up = st.get("interface_name"), st.get("xput_download"), st.get("xput_upload")
    if not ifname or down is None or up is None:
        return False
    cur = db.execute(
        "INSERT OR IGNORE INTO speedtest_observations (observed_at, ifname, xput_download, "
        "xput_upload) VALUES (?, ?, ?, ?)",
        (ts, ifname, down, up),
    )
    return cur.rowcount > 0
```

- [ ] **Step 2: Rewrite `persist_speedtests()` to attribute instead of guess**

Replace the body (line 377-393). Delete the `source =` line entirely:

```python
def persist_speedtests(db: sqlite3.Connection, speedtests: list[dict]) -> int:
    """Insert archived speedtests, attributing each to a WAN Path when possible.

    The archive carries no WAN field, so attribution comes from matching an
    observed speedtest-status reading on exact throughput. Anything that
    cannot be matched stays NULL -- the controller genuinely does not record
    which WAN ran the test, and guessing is what this replaced.
    """
    inserted = 0
    for st in speedtests:
        ts_ms = st.get("time")
        if ts_ms is None:
            continue
        down, up, lat = st.get("xput_download"), st.get("xput_upload"), st.get("latency")
        if not down and not up:
            continue  # zero-value glitch rows the controller occasionally logs
        row = db.execute(
            "SELECT p.id FROM speedtest_observations o "
            "JOIN wan_paths p ON p.ifname = o.ifname "
            "WHERE o.xput_download = ? AND o.xput_upload = ?",
            (down, up),
        ).fetchone()
        cur = db.execute(
            "INSERT OR IGNORE INTO speedtests (ts, download_mbps, upload_mbps, latency_ms, "
            "wan_path_id) VALUES (?, ?, ?, ?, ?)",
            (epoch_to_iso(ts_ms / 1000), down, up, lat, row[0] if row else None),
        )
        inserted += cur.rowcount
    return inserted
```

Then delete `CELLULAR_LATENCY_THRESHOLD_MS` and its comment block (lines 49-53).
Nothing else may reference it.

- [ ] **Step 3: Observe from `fast_loop()` in `live_server.py`**

Inside `fast_loop`'s device iteration, where `cat == "gateway"` is already
handled, collect gateway raws; after the tick is built, record observations.
Opening a database connection every second is too expensive, so only connect
when the reading actually changed — track the last seen triple in `state`:

```python
# In State.__init__:
self.last_speedtest_seen: dict[str, tuple] = {}   # gateway mac -> (ifname, down, up)
```

```python
# In fast_loop, after the tick is broadcast:
for raw in fast["devices"]:
    if persist.device_category(raw) not in ("gateway", "cellular_gateway"):
        continue
    st = raw.get("speedtest-status") or {}
    seen = (st.get("interface_name"), st.get("xput_download"), st.get("xput_upload"))
    if seen[0] is None or seen == state.last_speedtest_seen.get(raw.get("mac")):
        continue
    state.last_speedtest_seen[raw.get("mac")] = seen
    conn = db.connect()
    if persist.record_speedtest_observation(conn, raw, persist.now_iso()):
        log.info("speedtest observed on %s: %s/%s Mbps", seen[0], seen[1], seen[2])
    conn.commit()
    conn.close()
```

- [ ] **Step 4: Verify**

```bash
grep -rn "CELLULAR_LATENCY_THRESHOLD_MS" . --include=*.py && echo "STILL REFERENCED" || echo "PASS: heuristic gone"
uv run --python 3.14 --with unifi-core --with aiounifi python3 -c "
import asyncio, os, tempfile
os.environ['UNIFI_DB_PATH'] = tempfile.mktemp(suffix='.db')
from unifi_lib import db, persist
from unifi_lib.fetch import UnifiSession
async def main():
    s = UnifiSession(); await s.ensure_connected()
    devs = (await s.fetch_fast())['devices']
    c = db.connect(); db.init_db(c)
    ts = persist.now_iso()
    persist.persist_wan_stats(c, devs, ts)
    gw = next(d for d in devs if persist.device_category(d) == 'gateway')
    print('observation recorded:', persist.record_speedtest_observation(c, gw, ts))
    print('duplicate is idempotent:', persist.record_speedtest_observation(c, gw, ts) is False)
    n = persist.persist_speedtests(c, await s.fetch_speedtests(duration_hours=24*14)); c.commit()
    total, attributed = c.execute(
        'SELECT count(*), count(wan_path_id) FROM speedtests').fetchone()
    print(f'speedtests: {total} rows, {attributed} attributed to a WAN path')
    assert attributed >= 1, 'the observed test should have attributed at least one row'
    await s.close()
asyncio.run(main())
"
```

Most archived rows staying unattributed is **correct** — they predate observation.
At least one should attribute, matching the currently-live status reading.

- [ ] **Step 5: Commit**

```bash
git add unifi_lib/persist.py live_server.py
git commit -m "feat: attribute speedtests by observation, delete latency heuristic

CELLULAR_LATENCY_THRESHOLD_MS classified every speedtest by a magic
number tuned to one household's ISP latencies, silently misclassifying
on any other network.

Replaced by observing speedtest-status in fast_loop, which names the
interface authoritatively and is already in the payload at no extra API
cost. Archives match on exact throughput; unmatched rows stay NULL rather
than being guessed. Timestamps are unusable for this -- measurement
showed speedtest-status.timestamp is a refresh time, not a completion
time -- and per-second sampling is required because tests pair ~18s
apart against a 60s poll."
```

---

### Task 6: API — expose WAN paths, split the WAN history endpoint

**Files:**
- Modify: `live_server.py` — new `handle_wans`; `handle_wan_history` (line 402), `handle_rtt_history` (line 951), `handle_wan_usage` (line 1047), `handle_speedtest_history` (line 1081); route table

**Interfaces:**
- Consumes: `wan_paths`, `wan_stats` (Tasks 2-4).
- Produces: `GET /api/wans`; `?wan=<wan_path_id>` on the four handlers. Task 7 consumes both.

`handle_wan_history` currently serves WAN throughput **and** device metrics
(cpu/mem/load/temps) from one `gateway_stats` query filtered by `gateway_kind`.
Those are different scopes and must be split.

- [ ] **Step 1: Add `handle_wans`**

```python
async def handle_wans(request):
    """Discovered WAN Paths with their stable ids. May legitimately be empty."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, wan_key, ifname, link_type, isp_name, asn, label_override "
        "FROM wan_paths ORDER BY id"
    ).fetchall()
    conn.close()
    return web.json_response([
        {"id": r[0], "key": r[1], "ifname": r[2], "linkType": r[3],
         "isCellular": bool(r[3] and r[3].startswith("wireless")),
         "asn": r[5], "label": r[6] or r[4] or r[2] or r[1]}
        for r in rows
    ])
```

`label_override` wins when set, so a human correction survives every later cycle.

- [ ] **Step 2: Split `handle_wan_history` into WAN-scoped and device-scoped**

Replace the handler at line 402:

```python
async def handle_wan_history(request):
    """Per-WAN throughput and latency history for one WAN Path."""
    wan_id = request.query.get("wan")
    range_key = request.query.get("range", "7d")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket,
               AVG(rx_rate_bps), AVG(tx_rate_bps), AVG(latency_ms)
        FROM wan_stats
        WHERE wan_path_id = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (wan_id, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([
        {"t": r[0], "rxRateBps": r[1], "txRateBps": r[2], "latencyMs": r[3]} for r in rows
    ])


async def handle_gateway_history(request):
    """Device-scoped history for one Gateway Device: CPU, memory, load, temps."""
    mac = request.query.get("mac")
    range_key = request.query.get("range", "7d")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket,
               AVG(cpu_pct), AVG(mem_pct), AVG(load1), AVG(load5), AVG(load15), AVG(temp_cpu)
        FROM gateway_stats
        WHERE gateway_mac = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (mac, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([
        {"t": r[0], "cpu": r[1], "mem": r[2], "load1": r[3], "load5": r[4],
         "load15": r[5], "tempCpu": r[6]} for r in rows
    ])
```

- [ ] **Step 3: Switch the other three handlers to `?wan=`**

In `handle_rtt_history` (951), `handle_wan_usage` (1047) and
`handle_speedtest_history` (1081), replace
`gateway = request.query.get("gateway", "primary")` with
`wan_id = request.query.get("wan")` and change each query to filter on
`wan_path_id = ?` against `wan_stats` (or `speedtests.wan_path_id` for the
speedtest handler) instead of `gateway_kind`.

- [ ] **Step 4: Register the routes**

```python
    app.router.add_get("/api/wans", handle_wans)
    app.router.add_get("/api/history/gateway", handle_gateway_history)
```

- [ ] **Step 5: Verify**

```bash
python3 -c "import ast; ast.parse(open('live_server.py').read())" && echo "OK syntax"
grep -c 'query.get("gateway"' live_server.py | xargs echo "remaining ?gateway= handlers (want 0):"
```

Then run the server against the live controller on a spare port and check the
endpoints return real data:

```bash
BIND_PORT=8791 uv run --python 3.14 --with unifi-core --with aiounifi python3 live_server.py &
sleep 90    # one persist cycle must run before wan_stats has rows
curl -sS http://127.0.0.1:8791/api/wans
curl -sS "http://127.0.0.1:8791/api/history/wan?wan=1&range=24h" | head -c 300
kill %1
```

Expected: `/api/wans` returns two entries labelled `Spectrum` and `T-Mobile USA`
with distinct `id`s, and the history endpoint returns rows for `wan=1`.

- [ ] **Step 6: Commit**

```bash
git add live_server.py
git commit -m "feat: expose WAN paths and split WAN from gateway history

/api/wans returns discovered paths with stable ids and ISP labels.
handle_wan_history now serves per-WAN throughput and latency from
wan_stats keyed on wan_path_id; the device metrics it used to return
alongside them move to /api/history/gateway keyed on gateway_mac,
because a WAN Path has no CPU of its own."
```

---

### Task 7: Frontend — dynamic WAN toggle, separated device tiles

**Files:**
- Modify: `static/live_dashboard.html` — markup at lines 266-278; `renderGatewayStats`, `loadWanChart`, chart titles, `CAT_LABEL`/`DEV_ORDER`

**Interfaces:**
- Consumes: `/api/wans`, `?wan=<id>` endpoints (Task 6).

- [ ] **Step 1: Replace the hardcoded buttons with an empty container**

At lines 271-274, replace both `<button>` elements so the seg is populated at runtime:

```html
      <div class="seg" id="gatewaySeg"></div>
```

- [ ] **Step 2: Populate it from `/api/wans`**

Add near the other loaders, and call it from the init block **before**
`loadWanChart()` so a WAN is selected when the first chart request fires:

```js
let WANS = [];
let selectedWan = null;
async function loadWans() {
  WANS = await fetch("/api/wans").then(r => r.json()).catch(() => []);
  const seg = document.getElementById("gatewaySeg");
  // A network with one WAN needs no chooser; zero WANs must still render.
  seg.innerHTML = WANS.length > 1
    ? WANS.map((w, i) => `<button class="seg-btn2${i ? "" : " active"}" data-wan="${w.id}">${w.label}</button>`).join("")
    : "";
  selectedWan = WANS.length ? WANS[0].id : null;
}
function wanLabel(id) {
  const w = WANS.find(x => x.id === id);
  return w ? w.label : "WAN";
}
```

- [ ] **Step 3: Rewrite the toggle click handler**

Replace the `#gatewaySeg` listener body (it currently sets `selectedGateway` from
`btn.dataset.gw`):

```js
document.getElementById("gatewaySeg").addEventListener("click", e => {
  const btn = e.target.closest(".seg-btn2");
  if (!btn) return;
  selectedWan = Number(btn.dataset.wan);
  document.querySelectorAll("#gatewaySeg .seg-btn2").forEach(b => b.classList.toggle("active", b === btn));
  openTile = null;
  loadWanChart(); loadSpeedtest(); loadWanUsage(); loadRttMonitors();
});
```

The old handler also called `renderGatewayStats(latestTick)` and
`renderRttSection(latestTick)`; drop the `renderGatewayStats` call — device tiles
no longer depend on the selection.

- [ ] **Step 4: Point chart requests and titles at the selected WAN**

Every `?gateway=${selectedGateway}` becomes `?wan=${selectedWan}`, and the two
title ternaries at lines ~1335 and ~1643 become:

```js
`Round-trip time — ${wanLabel(selectedWan)}, last ${rangeLabel}`
`Download / upload throughput — ${wanLabel(selectedWan)}, last ${rangeLabel}`
```

- [ ] **Step 5: Verify in a browser**

Restart the server on port 8791 as in Task 6, open `http://127.0.0.1:8791`, and
confirm: the toggle shows **Spectrum** and **T-Mobile USA** (not "UDM Pro" or
"T-Mobile 5G"); switching updates the throughput chart and its title; gateway CPU
and memory tiles stay visible and unchanged across the switch; the devices list
still shows the Cellular Modem.

Then confirm nothing network-specific survives:

```bash
grep -nE 'UDM Pro|T-Mobile|data-gw=|selectedGateway' static/live_dashboard.html && echo "STILL PRESENT" || echo "PASS: clean"
```

- [ ] **Step 6: Commit**

```bash
git add static/live_dashboard.html
git commit -m "feat: build the WAN toggle from discovered paths

Buttons are rendered from /api/wans and labelled with each path's ISP
name, so the toggle reads correctly on any network instead of naming this
one's hardware. One WAN renders no chooser; zero WANs still render.

Gateway CPU and memory tiles no longer move with the toggle -- a WAN Path
has no CPU, and on this network the cellular path is a GRE tunnel while
the device that does have a CPU owns no WAN Path."
```

---

### Task 8: Remove the dead two-slot model and widen the enum maps

**Files:**
- Modify: `unifi_lib/persist.py` — delete `WAN_PATH_KINDS` (186); widen `DEVICE_CATEGORY_BY_TYPE` (44); `_persist_cellular_gateway` (273)
- Modify: `live_server.py` — `build_gateway_tick` (93), `build_rtt_tick` (164), `backfill_gateway_history` (1230)
- Modify: `static/live_dashboard.html` — `BAND_LABEL`/`BAND_SLOT` (1716)
- Modify: `CLAUDE.md`

**Interfaces:** consumes everything above; produces no new surface.

- [ ] **Step 1: Delete the two-slot remnants**

Remove `WAN_PATH_KINDS` from `persist.py:186` and every `wan3` literal lookup
(`persist.py:210`, `live_server.py:171`). `build_rtt_tick` must build its result
by iterating `discover_wan_paths(gw_raw)` and keying on `wan_key`, not on the
hardcoded `{"primary": wan1, "cellular": wan3}` pair. `build_gateway_tick`'s
`kind` parameter goes away — it now describes only the Gateway Device.

- [ ] **Step 2: Widen `DEVICE_CATEGORY_BY_TYPE`**

```python
DEVICE_CATEGORY_BY_TYPE = {
    # Gateways
    "udm": "gateway", "usg": "gateway", "ugw": "gateway",
    "uxg": "gateway", "ucg": "gateway",
    # Switches
    "usw": "switch", "usl": "switch",
    # Access points
    "uap": "ap",
    # Cellular modems -- a device category, independent of whether any WAN
    # Path is cellular.
    "umbb": "cellular_gateway", "umr": "cellular_gateway",
}
```

Unknown types still fall to `"other"`, and the frontend renders the raw value —
the documented graceful degradation. Do **not** remove `cellular_gateway`.

- [ ] **Step 3: Widen `BAND_LABEL` / `BAND_SLOT`**

```js
const BAND_LABEL = { ng: "2.4GHz", na: "5GHz", "6e": "6GHz", ax: "6GHz" };
```

Leave `REGION_NAMES` alone — it already falls back to the raw ISO code, and a
250-country table is scope creep.

- [ ] **Step 4: Update CLAUDE.md**

Add to the architecture section, after the polling-tiers table:

```markdown
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
```

Also update the "Reporting controller data honestly" section: the entry claiming
no API field distinguishes the WAN paths is now false and must be replaced with
the `interface_name` mechanism.

- [ ] **Step 5: Verify nothing network-specific remains**

```bash
for f in live_server.py unifi_lib/persist.py unifi_lib/db.py static/live_dashboard.html; do
  echo "--- $f"
  grep -nE 'WAN_PATH_KINDS|CELLULAR_LATENCY|"wan3"|UDM Pro|T-Mobile|gateway_kind *= *["'"'"']' "$f" || echo "  clean"
done
python3 -c "import ast; ast.parse(open('live_server.py').read())" && echo "OK live_server"
python3 -c "import ast; ast.parse(open('unifi_lib/persist.py').read())" && echo "OK persist"
```

Then run the full server on 8791 for 90 seconds and confirm no `tick failed` or
`loop failed` in the logs, and that the dashboard still renders every tab.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: remove the two-slot WAN model, widen device taxonomy

Deletes WAN_PATH_KINDS and the hardcoded wan3 lookups; RTT is now built
by iterating discovered WAN Paths. Widens DEVICE_CATEGORY_BY_TYPE to the
current UniFi taxonomy and BAND_LABEL for 6GHz, keeping 'other' and the
raw-value fallback as documented graceful degradation.

REGION_NAMES is deliberately untouched -- it already falls back to the
raw ISO code. Documents the WAN Path / Gateway Device distinction in
CLAUDE.md and corrects its now-false claim that no API field
distinguishes the WAN paths."
```

---

## Rollback

Every task is a separate commit on `wan-portability`, and `main` still carries the
working containerized dashboard. `git checkout main` restores it; the NAS keeps
running its published image until a new one is built from `main`.

The schema changes are additive, so a database written by this branch still opens
under `main` — the new tables are simply ignored.

## Notes on verification

There is no test framework and none is being added. Tasks 1 and 3 are pure
functions verified by scratch scripts against saved payloads, which is the only
way to exercise single-WAN, three-WAN, re-cabling and ISP-change cases that this
network cannot produce. Tasks 4-8 are verified against the live controller.

Attribution of *historical* speedtests is impossible and expected to stay NULL;
only tests observed while the dashboard is running can be attributed.

---

### Task 9: Key RTT history on WAN Path

**Added after Task 6 revealed the gap.** `rtt_monitors` still keys on the legacy
`gateway_kind` primary/cellular column, so `handle_rtt_history` bridges `?wan=`
through `link_type`. That works on a network with one wired and one cellular WAN,
but **cannot distinguish two non-cellular WAN Paths on one gateway** — the exact
two-slot limitation this plan exists to remove, surviving in one endpoint.

**Files:**
- Modify: `unifi_lib/db.py` — additive column + prune unchanged
- Modify: `unifi_lib/persist.py` — write `wan_path_id` when recording monitors
- Modify: `live_server.py` — `handle_rtt_history` keys on `wan_path_id`, bridge deleted

**Interfaces:** consumes `resolve_path_ids` (Task 3) and `wan_paths` (Task 2).

- [ ] **Step 1: Add the column**

In `init_db()`, with the other `PRAGMA table_info` migrations:

```python
    existing_rtt_cols = {row[1] for row in db.execute("PRAGMA table_info(rtt_monitors)").fetchall()}
    if "wan_path_id" not in existing_rtt_cols:
        db.execute("ALTER TABLE rtt_monitors ADD COLUMN wan_path_id INTEGER")
```

Do not change the primary key and do not drop `gateway_kind` — additive rule.
Rows predating this keep `wan_path_id IS NULL`.

- [ ] **Step 2: Populate it**

Wherever `rtt_monitors` rows are written, resolve the owning WAN Path the same
way `persist_wan_stats` does — one `resolve_path_ids` call per gateway, matched
to the monitor's WAN by the path's `key`. A monitor whose WAN cannot be resolved
writes `NULL`; it must never fall back to a `gateway_kind` guess.

- [ ] **Step 3: Key the handler on it**

Replace `handle_rtt_history`'s `link_type`→`gateway_kind` bridge with a direct
`WHERE wan_path_id = ?`. Delete the bridge and its CARRY FORWARD note. A missing
or unresolvable `?wan=` returns `[]`, never a cross-WAN aggregate.

- [ ] **Step 4: Verify**

```bash
grep -n "gateway_kind" live_server.py || echo "PASS: no gateway_kind in handlers"
```

Then, using a temp database via `UNIFI_DB_PATH` (never the production file),
assert that two `wan_paths` rows on one gateway with the **same** `link_type`
(`ethernet`) resolve to different `wan_path_id`s in `rtt_monitors`, and that
querying one returns only its own rows. That case is the whole point of the task
and is the one the bridge could not express.

- [ ] **Step 5: Commit**

```bash
git add unifi_lib/db.py unifi_lib/persist.py live_server.py
git commit -m "feat: key RTT history on WAN Path identity

rtt_monitors keyed on the legacy gateway_kind primary/cellular column, so
handle_rtt_history had to bridge ?wan= through link_type -- which cannot
distinguish two non-cellular WAN Paths on one gateway. That was the last
two-slot assumption in the codebase.

Adds an additive wan_path_id column, populates it from the same identity
resolution wan_stats uses, and keys the handler on it directly.
Unresolvable monitors write NULL rather than guessing."
```
