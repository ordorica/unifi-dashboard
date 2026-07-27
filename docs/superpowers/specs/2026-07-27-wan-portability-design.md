# Making the dashboard portable across networks

**Date:** 2026-07-27
**Status:** Approved after design review, ready for implementation planning
**Related:** [ADR 0001 — WAN Path identity](../../adr/0001-wan-path-identity.md), [CONTEXT.md](../../../CONTEXT.md)

## Goal

Remove every network-specific assumption so the dashboard runs correctly on any
UniFi network, not just this one. Everything displayed or persisted must come
from the controller API rather than from values hardcoded to one household.

Terms used here — **WAN Path**, **Gateway Device**, **Cellular Modem**,
**Link Type**, **Speedtest Attribution** — are defined in `CONTEXT.md`.

## What the audit found

A full scan of `live_server.py`, `unifi_lib/*.py` and `static/live_dashboard.html`.

### Already portable — no work needed

Checked specifically and clean:

- **VLAN / network names** — `persist.set_networks()` builds the subnet→name map
  from the controller's real config each persist cycle; the frontend's `NETWORKS`
  is `{}` at load and populated at runtime from `/api/networks`.
- **SSIDs / WiFi names** — every reference is dynamic (`n.ssid`, `d.ssid`,
  `w.ssid`). No SSID string appears in the source.
- **Firewall rules, zones, groups, port forwards** — rendered entirely from
  `/api/firewall`.
- **Client names, MACs, IPs** — all API-sourced.
- **No personal identifiers anywhere** — grepping all source for the owner's
  domain, family names, city names and ISP names returns nothing.

### Network-specific — must fix

| # | Location | Problem |
| --- | --- | --- |
| 1 | `static/live_dashboard.html:272-273` | Button labels `Primary (UDM Pro)` and `Cellular failover (T-Mobile 5G)` |
| 2 | `unifi_lib/persist.py:53` | `CELLULAR_LATENCY_THRESHOLD_MS = 24`, tuned to this ISP pair |
| 3 | `unifi_lib/persist.py:186` | `WAN_PATH_KINDS = {"WAN": "primary", "WAN3": "cellular"}`, plus `wan3` lookups in `live_server.py:171` and `persist.py:210` |
| 4 | schema + tick + UI | Two-slot `primary`/`cellular` model conflating Gateway Device with WAN Path |

Item 2 is the most damaging: it classifies every speedtest by a magic number
derived from this network's latencies, and the comment admits "no API field
distinguishes the two". Elsewhere it misclassifies silently, producing
plausible-looking wrong data rather than an error.

### Incomplete but graceful — widen, don't rewrite

Every enum map falls back to the raw API value (`CAT_LABEL[d.category] || d.category`,
`BAND_LABEL[r.band] || r.band`, `regionLabel()` returns the bare code), so unknown
values render as raw codes rather than `undefined`. Quality gaps, not correctness
bugs: `DEVICE_CATEGORY_BY_TYPE` (covers `udm/usg/ugw/usw/uap/umbb`),
`BAND_LABEL`/`BAND_SLOT` (`ng/na/6e`), `REGION_NAMES` (~28 countries).

## API findings

Probed live against the controller, not assumed.

| Hardcoded today | API source | Value here |
| --- | --- | --- |
| `"Primary (UDM Pro)"` | `active_geo_info.WAN.isp_name` | `Spectrum` |
| `"Cellular failover (T-Mobile 5G)"` | `active_geo_info.WAN3.isp_name` | `T-Mobile USA` |
| `WAN3 == cellular` | `wan3.type` | `wireless_5g` (vs `ethernet` on wan1) |
| WAN inventory | `last_wan_interfaces` keys | `WAN`, `WAN3` |
| speedtest attribution | `speedtest-status.interface_name` | `gre1` → matches `wan3.ifname` |

Four measured facts that shape the design:

- **The speedtest archive has no WAN field.** Records carry only `_id`,
  `latency`, `o`, `oid`, `time`, `xput_download`, `xput_upload`. `oid` is
  identical across all 24 records and cannot distinguish paths.
- **`speedtest-status.timestamp` is a refresh time, not a completion time.**
  Live status carried `xput 450/65` with a timestamp 0.0 hours old, while the
  archive record with exactly `450/65` was 10.2 hours old. Any design matching
  on timestamp proximity would attribute nothing.
- **Speedtests run in pairs, ~18–109 seconds apart** (one per WAN). A 60-second
  poll catches at most one of each pair.
- **`wan3` here is a GRE tunnel** (`ifname: gre1`, `type: wireless_5g`), not a
  modem interface, and the `umbb` Cellular Modem owns no WAN Path at all.
  Interface naming cannot infer link type; the `type` field must be used. This
  is also why a WAN Path cannot carry device metrics.

## Section 1 — WAN discovery

New module `unifi_lib/wan.py`: one pure function turning a Gateway Device's raw
dict into the WAN Paths it actually has. No controller or database access, so it
is verified by feeding it saved payloads.

```python
@dataclass(frozen=True)
class WanPath:
    key: str           # "WAN", "WAN3"   -- controller's inventory key
    slot: str          # "wan1", "wan3"  -- where raw stats live
    ifname: str        # "eth9", "gre1"
    link_type: str     # "ethernet", "wireless_5g"
    is_cellular: bool  # derived from link_type, never from the key name
    isp_name: str
    asn: int | None
    ip: str | None
    status: str        # "online" / "offline"
    label: str
```

Resolution order, each step API-sourced:

1. **Inventory** — keys of `last_wan_interfaces`. Authoritative: one WAN yields
   one entry, three yield three.
2. **Slot mapping** — `WAN`→`wan1`, `WAN<n>`→`wan<n>`, derived from the
   controller's naming rather than enumerated.
3. **Link facts** — `raw[slot]`: `ifname`, `type`, `up`, byte counters.
4. **Identity** — `active_geo_info[key]`: `isp_name`, `asn`.
5. **Status** — `last_wan_status[key]`.

**Label precedence:** `isp_name` → `ifname` → `key`.

**`is_cellular`** derives from `link_type` (`wireless_5g`, `wireless_lte`, any
`wireless_*`). This is what removes the WAN3 assumption: a cellular WAN on WAN2
classifies correctly, and a wired backup on WAN3 correctly does not.

A gateway exposing no recognisable WAN keys yields an empty list. Callers must
handle that rather than assuming at least one WAN exists.

## Section 2 — Identity and data model

Per ADR 0001, a WAN Path's identity is a synthetic id, not its controller key.

```sql
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
);

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
);
CREATE INDEX IF NOT EXISTS idx_wan_stats_path_ts ON wan_stats(wan_path_id, ts);
```

Identity attributes live on `wan_paths` and change rarely; `wan_stats` holds only
what varies per sample.

### Matching rule

Applied each persist cycle, for each discovered WAN Path:

1. Candidates = rows in `wan_paths` with the same `gateway_mac` and the same `asn`.
2. Exactly one candidate → **same path**; update `last_seen` and any changed
   attributes. This is what survives re-cabling.
3. Several candidates → prefer same `wan_key`, then same `ifname`. Still
   ambiguous → **new path**.
4. No ASN available → match on (`wan_key`, `ifname`) only. No match → **new path**.
5. **Never merge under doubt.**

The bias is deliberate. A wrong split shows two series where one was expected —
visible and correctable via `label_override`. A wrong merge blends two services
irreversibly, because the rows no longer record which was which.

### Other schema notes

`gateway_stats` keeps Gateway Device metrics (CPU, memory, load, temps, uptime)
and stops receiving WAN columns. Per this codebase's additive-migration rule the
old WAN columns stay in place — dead but harmless — so a restart against an older
database remains safe.

`rx_bytes_total`/`tx_bytes_total` keep the monotonic cumulative-counter
convention CLAUDE.md documents, so `_usage_since()` still works as a delta
between two samples, now per WAN Path.

`prune_old()` gains `wan_stats` and `speedtest_observations` deletes.
`wan_paths` is **not** pruned — it is identity, not time series.

**Clean break, no backfill.** Existing `gateway_stats` WAN columns are not
migrated: those rows recorded whatever `wan1` reported and never knew which WAN
they measured.

### Disappearing WAN Paths

A path absent from `last_wan_interfaces` keeps its `wan_paths` row and all its
`wan_stats` history — deleting it would destroy data the user may still want to
chart. It simply stops receiving samples, and `last_seen` stops advancing. The
WAN toggle lists paths seen within the retention window, so a removed WAN fades
out naturally rather than vanishing mid-session.

### Multiple Gateway Devices

`wan_paths` keys on `gateway_mac`, so a network with several gateways gets
correct per-gateway paths with no additional work. This supersedes the earlier
draft's "multi-gateway out of scope" note, which contradicted the schema.

## Section 3 — Speedtest attribution

`CELLULAR_LATENCY_THRESHOLD_MS` is deleted.

Attribution is **observation, not reconciliation**. `fast_loop` already fetches
the Gateway Device every second, so `speedtest-status` is already in the payload
at no additional API cost. Each time `(interface_name, xput_download,
xput_upload)` differs from the previously seen triple, that is a newly completed
test — record it:

```sql
CREATE TABLE IF NOT EXISTS speedtest_observations (
    observed_at TEXT NOT NULL,
    ifname TEXT NOT NULL,
    xput_download REAL,
    xput_upload REAL,
    PRIMARY KEY (ifname, xput_download, xput_upload)
);
```

Persisting rather than holding in memory makes attribution survive a container
restart between observing a test and its archive record appearing — a real gap
given tests occur only ~1.7×/day. The primary key makes re-observation
idempotent.

`speedtests` gains a nullable `wan_path_id`. Attribution joins `speedtests` to
`speedtest_observations` on **exact equality of both throughput values**, then
resolves `ifname` → `wan_paths.wan_path_id` via the owning gateway.

Why not timestamps: measurement showed `speedtest-status.timestamp` is a refresh
time, so timestamp proximity matches nothing. Why per-second observation: tests
pair 18 seconds apart and a 60-second poll would miss half of them.

Unmatched records — including every row already in the database, and any test run
while the dashboard was down — keep `wan_path_id = NULL` and render as "unknown
WAN". The existing `source` column stays per the additive rule but stops being
written.

## Section 4 — Frontend

### The toggle is WAN-scoped

Device metrics and path metrics are separated, because they are different things
and this network proves they can't be merged — WAN3 is a GRE tunnel with no CPU,
while the Cellular Modem that *has* a CPU owns no WAN Path.

- **Under the toggle** (WAN-scoped): throughput chart, RTT, speedtest, usage,
  ISP, status.
- **Not under the toggle** (device-scoped): CPU, memory, load, temperature,
  uptime, carrier — rendered for each Gateway Device, always visible.

### `/api/wans`

Returns discovered paths in `last_wan_interfaces` key order:

```json
[{"id": 1, "key": "WAN",  "label": "Spectrum",     "ifname": "eth9",
  "linkType": "ethernet",    "isCellular": false, "status": "online", "asn": 20115},
 {"id": 2, "key": "WAN3", "label": "T-Mobile USA", "ifname": "gre1",
  "linkType": "wireless_5g", "isCellular": true,  "status": "online", "asn": 21928}]
```

An empty array is valid — the page must render without a WAN toggle rather than
erroring.

The buttons become a loop over that endpoint, keyed on `id`:

```js
wans.map(w => `<button class="seg-btn2" data-wan="${w.id}">${w.label}</button>`)
```

Chart titles use the selected path's label instead of
`selectedGateway === "primary" ? … : …` ternaries.

### `cellular_gateway` stays a device category

A `umbb` modem is a real device with its own CPU, memory, uptime and carrier, so
`DEVICE_CATEGORY_BY_TYPE`, `CAT_LABEL` and `DEV_ORDER` keep it. What changes is
the conflation: the tick currently derives `gateways.cellular` from the presence
of a `cellular_gateway` device, wrongly tying a WAN Path to a device type. After
this work the two are independent.

## Section 5 — Enum maps and the API break

Widen `DEVICE_CATEGORY_BY_TYPE` to the current UniFi taxonomy (`uxg`, `ucg`,
`usl`, `umr` and similar) and `BAND_LABEL`/`BAND_SLOT` likewise, keeping
`"other"` and the raw-value fallback as the documented graceful degradation.

**`REGION_NAMES` is deliberately left as-is** — it already falls back to the raw
ISO code, and shipping a 250-country table to fix a cosmetic gap is scope creep.

API parameters change from `?gateway=primary|cellular` to `?wan=<wan_path_id>`
across `/api/history/wan`, `/api/usage/wan`, `/api/history/rtt` and
`/api/history/speedtest`. Clean break, no compatibility shim: single deployment,
single user, and a shim would preserve exactly the two-slot thinking being
removed. Accepted cost: `?wan=2` is less readable than `?wan=WAN3` when testing
endpoints by hand.

## Verification

- `unifi_lib/wan.py` is pure: verified against this network's two-WAN payload, a
  synthesised single-WAN payload, a synthesised three-WAN payload, and a payload
  with no WAN keys.
- Against the live controller, discovery must yield exactly `WAN` (Spectrum,
  `ethernet`, not cellular) and `WAN3` (T-Mobile USA, `wireless_5g`, cellular).
- Matching is verified against saved payloads for: a re-cabled path (same ASN,
  different key → same id), an ISP change (same key, different ASN → new id),
  two same-ASN paths (→ two ids, disambiguated by key), and a path with no ASN.
- After one persist cycle, `wan_stats` holds one row per path per timestamp and
  `wan_paths` holds one row per discovered path.
- After an observed speedtest, `speedtest_observations` gains a row and the
  corresponding `speedtests` row resolves to a `wan_path_id`.
- No source file contains `UDM Pro`, `T-Mobile`, `CELLULAR_LATENCY_THRESHOLD_MS`,
  or `primary`/`cellular` used as a WAN identifier.
- The dashboard renders against the live controller with a dynamically built,
  ISP-labelled toggle, and device tiles visible independently of the selection.

## Out of scope

- Backfilling historical WAN attribution — the data does not exist.
- Attributing speedtests that ran while the dashboard was down.
- Expanding `REGION_NAMES`.
- Any change to VLAN, SSID, firewall or client handling — already portable.
