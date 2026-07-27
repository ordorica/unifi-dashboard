# Making the dashboard portable across networks

**Date:** 2026-07-27
**Status:** Approved, ready for implementation planning

## Goal

Remove every network-specific assumption so the dashboard runs correctly on any
UniFi network, not just this one. Everything displayed or persisted must come
from the controller API rather than from values hardcoded to one household.

## What the audit found

A full scan of `live_server.py`, `unifi_lib/*.py` and `static/live_dashboard.html`.

### Already portable — no work needed

These were checked specifically and are clean:

- **VLAN / network names** — `persist.set_networks()` builds the subnet→name map
  from the controller's real network config each persist cycle. The frontend's
  `NETWORKS` is `{}` at load and populated at runtime from `/api/networks`.
- **SSIDs / WiFi names** — every reference is dynamic (`n.ssid`, `d.ssid`,
  `w.ssid`). No SSID string appears in the source.
- **Firewall rules, zones, groups, port forwards** — rendered entirely from
  `/api/firewall`. The only quoted strings near that code are CSS class names.
- **Client names, MACs, IPs** — all API-sourced.
- **No personal identifiers anywhere** — a grep for the owner's domain, family
  names, city names and ISP names across all source files returns nothing.

### Network-specific — must fix

| # | Location | Problem |
| --- | --- | --- |
| 1 | `static/live_dashboard.html:272-273` | Button labels `Primary (UDM Pro)` and `Cellular failover (T-Mobile 5G)` |
| 2 | `unifi_lib/persist.py:53` | `CELLULAR_LATENCY_THRESHOLD_MS = 24`, tuned to this ISP pair |
| 3 | `unifi_lib/persist.py:186` | `WAN_PATH_KINDS = {"WAN": "primary", "WAN3": "cellular"}`, plus `wan3` lookups in `live_server.py:171` and `persist.py:210` |
| 4 | schema + tick + UI | Two-slot `primary`/`cellular` WAN model |

Item 2 is the most damaging. It classifies every speedtest by a magic number
derived from this network's latencies — the comment admits "no API field
distinguishes the two". On another network it misclassifies silently, producing
plausible-looking wrong data rather than an error.

### Incomplete but graceful — widen, don't rewrite

Every enum map falls back to the raw API value (`CAT_LABEL[d.category] || d.category`,
`BAND_LABEL[r.band] || r.band`, `regionLabel()` returns the bare code). Unknown
values render as raw codes, never as `undefined`. These are quality gaps, not
correctness bugs:

- `DEVICE_CATEGORY_BY_TYPE` covers `udm/usg/ugw/usw/uap/umbb`. All four types on
  this network are covered. A `uxg`, `ucg`, `usl` or `umr` elsewhere falls to
  `"other"` — displayed, but grouped wrongly.
- `BAND_LABEL` / `BAND_SLOT` cover `ng/na/6e`.
- `REGION_NAMES` covers ~28 countries.

## API findings that make this possible

Probed live against the controller, not assumed. Every hardcoded value has an
API equivalent:

| Hardcoded today | API source | Value here |
| --- | --- | --- |
| `"Primary (UDM Pro)"` | `active_geo_info.WAN.isp_name` | `Spectrum` |
| `"Cellular failover (T-Mobile 5G)"` | `active_geo_info.WAN3.isp_name` | `T-Mobile USA` |
| `WAN3 == cellular` | `wan3.type` | `wireless_5g` (vs `ethernet` on wan1) |
| WAN inventory | `last_wan_interfaces` keys | `WAN`, `WAN3` |
| `CELLULAR_LATENCY_THRESHOLD_MS` | `speedtest-status.interface_name` | `gre1` → matches `wan3.ifname` |

Two further facts that shape the design:

- **The speedtest archive has no WAN field.** `stat/speedtest` records carry only
  `_id`, `latency`, `o`, `oid`, `time`, `xput_download`, `xput_upload`. `oid` is
  identical across all 24 records, so it cannot distinguish paths. Only the
  gateway's *live* `speedtest-status` names the interface.
- **`wan3` here is a GRE tunnel** (`ifname: gre1`, `type: wireless_5g`), not a
  modem interface. Interface naming cannot be used to infer link type; the
  `type` field must be.

## Section 1 — WAN discovery

New module `unifi_lib/wan.py`, one pure function turning the gateway's raw dict
into the WAN paths that actually exist. Pure and independently testable: it takes
a dict and returns a list, with no controller or database access.

```python
@dataclass(frozen=True)
class WanPath:
    key: str           # "WAN", "WAN3"   -- controller's inventory key
    slot: str          # "wan1", "wan3"  -- where raw stats live
    ifname: str        # "eth9", "gre1"
    link_type: str     # "ethernet", "wireless_5g"
    is_cellular: bool  # derived from link_type, never from the key name
    isp_name: str      # "Spectrum", "T-Mobile USA"
    asn: int | None
    ip: str | None
    status: str        # "online" / "offline"
    label: str         # display string
```

Resolution order, each step API-sourced:

1. **Inventory** — keys of `last_wan_interfaces`. Authoritative: one WAN yields
   one entry, three yield three.
2. **Slot mapping** — `WAN`→`wan1`, `WAN<n>`→`wan<n>`, derived from the
   controller's own naming rather than enumerated.
3. **Link facts** — `raw[slot]`: `ifname`, `type`, `up`, byte counters.
4. **Identity** — `active_geo_info[key]`: `isp_name`, `asn`.
5. **Status** — `last_wan_status[key]`.

**Label precedence:** `isp_name` → `ifname` → `key`. A network whose geo lookup
fails shows `eth9` rather than a blank.

**`is_cellular`** derives from `link_type` (`wireless_5g`, `wireless_lte`, and
any future `wireless_*`), which is what removes the WAN3 assumption: a cellular
WAN on WAN2 classifies correctly, and a wired backup on WAN3 correctly does not.

A gateway exposing no recognisable WAN keys yields an empty list. Callers must
handle that rather than assuming at least one WAN exists.

## Section 2 — Data model

```sql
CREATE TABLE IF NOT EXISTS wan_stats (
    ts TEXT NOT NULL,
    gateway_mac TEXT NOT NULL,
    wan_key TEXT NOT NULL,
    ifname TEXT,
    link_type TEXT,
    isp_name TEXT,
    status TEXT,
    rx_rate_bps INTEGER,
    tx_rate_bps INTEGER,
    rx_bytes_total INTEGER,
    tx_bytes_total INTEGER,
    latency_ms REAL,
    PRIMARY KEY (ts, gateway_mac, wan_key)
);
CREATE INDEX IF NOT EXISTS idx_wan_stats_key_ts ON wan_stats(gateway_mac, wan_key, ts);
```

`gateway_stats` keeps device-level metrics (CPU, memory, load, temps, uptime) and
stops receiving WAN columns. Per this codebase's additive-migration rule the old
WAN columns stay in place — dead but harmless — so a restart against an older
database remains safe.

`rx_bytes_total` / `tx_bytes_total` keep the monotonic cumulative-counter
convention CLAUDE.md documents, so `_usage_since()` continues to work as a delta
between two samples, now per WAN.

`prune_old()` gains a `wan_stats` delete — mandatory for every time-series table.

**Clean break, no backfill.** Existing `gateway_stats` WAN columns are not
migrated: those rows recorded whatever `wan1` reported and never knew which WAN
they measured. The NAS database was two hours old when this was decided, so the
loss is negligible and the model ends up correct rather than patched.

## Section 3 — Speedtest attribution

`CELLULAR_LATENCY_THRESHOLD_MS` is deleted.

`slow_loop` already fetches the gateway. It reads `speedtest-status`
(`interface_name`, `timestamp`, `xput_download`, `xput_upload`), matches it
against archive records, resolves `interface_name` → `ifname` → `wan_key`, and
stores that.

**Match rule, stated explicitly so it cannot be interpreted two ways:** an
archive record matches the live status when their timestamps differ by **≤ 5000
ms** *and* both `xput_download` and `xput_upload` are exactly equal. Both
conditions are required. If more than one archive record satisfies both, none is
attributed — an ambiguous match is left `NULL` rather than guessed, which is the
same principle that makes the latency heuristic unacceptable.

`speedtests` gains a nullable `wan_key` column. The existing `source` column is
left in place per the additive rule but stops being written.

Unmatched records — including every row already in the database — keep
`wan_key = NULL` and render as "unknown WAN". This is the honest outcome: the
controller's archive does not record which WAN ran the test, and guessing is
precisely what the current code does wrong.

## Section 4 — Frontend

A new `GET /api/wans` returns the discovered paths, one object per WAN, in
`last_wan_interfaces` key order:

```json
[{"key": "WAN",  "label": "Spectrum",      "ifname": "eth9",
  "linkType": "ethernet",    "isCellular": false, "status": "online", "asn": 20115},
 {"key": "WAN3", "label": "T-Mobile USA",  "ifname": "gre1",
  "linkType": "wireless_5g", "isCellular": true,  "status": "online", "asn": 21928}]
```

An empty array is a valid response — the frontend must render the page without a
WAN toggle rather than erroring.

The two hardcoded buttons become a loop over that endpoint:

```js
wans.map(w => `<button class="seg-btn2" data-wan="${w.key}">${w.label}</button>`)
```

One WAN renders a single button; three render three. Labels come from `isp_name`.

Chart titles stop using `selectedGateway === "primary" ? … : …` ternaries and use
the selected WAN's label.

**`cellular_gateway` stays as a device category.** A `umbb` modem is a real
physical device with its own CPU, memory, uptime and carrier, and it belongs in
the devices list — so `DEVICE_CATEGORY_BY_TYPE`, `CAT_LABEL` and `DEV_ORDER`
keep it. What changes is the *conflation*: the tick currently derives
`gateways.cellular` from the presence of a `cellular_gateway` device, which
wrongly ties a WAN path to a device type. After this work the two are
independent — WAN paths come from `last_wan_interfaces` on the gateway, and a
cellular modem is just another device. A network can have a cellular WAN with no
`umbb` device (as here, where WAN3 is a GRE tunnel), or a `umbb` device that is
not currently a WAN path.

## Section 5 — Enum maps and the API break

Widen `DEVICE_CATEGORY_BY_TYPE` to the current UniFi taxonomy (`uxg`, `ucg`,
`usl`, `umr` and similar), keeping `"other"` as the documented graceful fallback.
Widen `BAND_LABEL` / `BAND_SLOT` likewise.

**`REGION_NAMES` is deliberately left as-is.** It already falls back to the raw
ISO code, and shipping a 250-country table to fix a cosmetic gap is scope creep.

API parameters change from `?gateway=primary|cellular` to `?wan=<key>` across
`/api/history/wan`, `/api/usage/wan`, `/api/history/rtt` and
`/api/history/speedtest`. Clean break with no compatibility shim: single
deployment, single user, and a shim would preserve exactly the two-slot thinking
being removed.

## Verification

- `unifi_lib/wan.py` is pure, so it is verified by feeding it saved gateway
  payloads: this network's two-WAN dict, a synthesised single-WAN dict, a
  synthesised three-WAN dict, and a dict with no WAN keys at all.
- Against the live controller, the discovered WAN list must equal exactly
  `WAN` (Spectrum, ethernet, not cellular) and `WAN3` (T-Mobile USA,
  wireless_5g, cellular).
- After one `persist_loop` cycle, `wan_stats` must hold one row per WAN per
  timestamp, with `isp_name` populated.
- No source file may contain the strings `UDM Pro`, `T-Mobile`, `primary`/`cellular`
  as WAN identifiers, or `CELLULAR_LATENCY_THRESHOLD_MS`.
- The dashboard must render correctly against the live controller with the
  gateway toggle built dynamically and labelled from ISP names.

## Out of scope

- Backfilling historical WAN attribution — impossible, the data does not exist.
- Expanding `REGION_NAMES`.
- Any change to VLAN, SSID, firewall, or client handling — already portable.
- Multi-gateway support beyond what `gateway_stats` already does per MAC.
