# Removing devices that no longer exist on the controller

A device forgotten in the UniFi console currently lives in this dashboard
forever. Its row in `devices` is never deleted, and neither is any of the
history keyed to its MAC. This adds a grace-period sweep that removes such a
device and its history a week after the controller stops reporting it, an
`absent` status so the countdown is visible rather than silent, and a manual
delete for when a week is too long to wait.

## Goal

Three outcomes, in order of importance:

1. A device removed from the controller eventually disappears from the
   dashboard, taking its historical data with it.
2. Nothing is deleted because of a transient failure. The cost of a wrong
   deletion is unrecoverable data loss; the cost of a wrong retention is a
   stale row for a few more days.
3. A device on its way out is visibly on its way out, and can be dismissed
   immediately by hand.

## What the audit found

### `devices` has no delete path at all

`persist.py:212` is the **only** writer to the table, and it is an upsert.
Nothing in `unifi_lib/`, `live_server.py` or `poll_unifi.py` ever deletes a
device row. The local database currently holds 18 devices; `clients` holds
265, but clients are out of scope here (see Out of scope).

### `updated_at` already means the right thing

Because that upsert is the sole writer, `devices.updated_at` is exactly "the
last time the controller's inventory reported this device". Every present
device is refreshed in lockstep each cycle — all 18 rows share an identical
timestamp. No new column is needed to detect absence.

The distinction that makes this safe: `device_status()` (`persist.py:84`)
returns `"offline"` for `state != 1`, so a device that is **powered off but
still adopted** continues to appear in `get_devices()` and continues to have
its `updated_at` refreshed. It is never a deletion candidate. Only a device
*forgotten in the controller* stops being refreshed.

### The UI never reads `devices` from the database

`tick.devices` is built entirely from the live controller fetch
(`live_server.py:293`, from `fast["devices"]`), and the devices page renders
from the WebSocket tick (`live_dashboard.html:1160`). The `/api/devices`
endpoint exists but does not feed this table.

The consequence drives Section 4: a forgotten device **already vanishes from
the UI instantly**. Without a merge it could never render as `absent`, and the
trashcan would be unreachable.

### Every route is read-only

All 28 routes are `add_get` (`live_server.py:1475-1504`). The delete endpoint
in Section 5 is the first write route in the application.

## Section 1 — Detection

Two constants in `db.py`, beside the existing `RETENTION_DAYS = 30`:

```python
DEVICE_ABSENT_AFTER_MINUTES = 10
DEVICE_ABSENCE_DAYS = 7
```

Ten minutes rather than a single 60-second cycle so a brief blip does not flap
rows in and out of the table.

One new function, called from both persistence paths where `db.prune_old(conn)`
already sits — `live_server.py:380` and `poll_unifi.py:42`:

```python
def sweep_absent_devices(db, seen_device_count: int) -> list[str]
```

It marks, then deletes, then returns the MACs it deleted so callers can log
them. It **must** run after `persist_devices_and_gateways` in the same
transaction, so devices present in this cycle already carry a fresh
`updated_at` and cannot be caught by either threshold.

### The guard

**If `seen_device_count == 0` the function returns immediately and changes
nothing.**

Without it, a controller returning an empty inventory — expired credentials, an
API change, a permissions change — would mark every device absent within ten
minutes and delete every device and all its history a week later. The grace
period alone does not cover this, because the empty response repeats on every
cycle. This guard is the difference between "the dashboard looks broken until
you fix the credentials" and "the dashboard destroyed your history while you
were on holiday".

## Section 2 — The `absent` status

The sweep sets `status = 'absent'` on devices whose `updated_at` is older than
`DEVICE_ABSENT_AFTER_MINUTES`.

This overloads the existing `status` column rather than adding an `is_absent`
flag, for two reasons. `status` already means "what the controller last told us
about this device", and "the controller no longer lists this device" is
genuinely one of those things. And it **self-heals**: when the device returns,
the normal upsert overwrites `absent` with `online`/`offline` with no special
handling.

Absence is stored in the database rather than computed in memory from the live
set. That is what keeps it correct across restarts — `state.last_fast` is
`None` immediately after a container start, so a set-difference approach would
briefly consider every device absent.

## Section 3 — The cascade

One function, `delete_device(db, mac)` in `db.py` beside `prune_old`, used by
both the sweep and the manual endpoint so there is one deletion path rather
than two. Ordered children first:

| Step | Table | Key |
|------|-------|-----|
| 1 | collect `wan_path_ids` | `wan_paths.gateway_mac` |
| 2 | `UPDATE speedtests SET wan_path_id = NULL` | those path ids |
| 3 | `DELETE FROM wan_stats` | `wan_path_id` |
| 4 | `DELETE FROM rtt_path_monitors` | `wan_path_id` |
| 5 | `DELETE FROM rtt_monitors` (legacy rows) | `wan_path_id` |
| 6 | `DELETE FROM wan_paths` | `gateway_mac` |
| 7 | `DELETE FROM speedtest_observations` | `gateway_mac` |
| 8 | `DELETE FROM gateway_stats` | `gateway_mac` |
| 9 | `DELETE FROM device_stats` | `mac` |
| 10 | `DELETE FROM port_stats` | `mac` |
| 11 | `DELETE FROM ap_radios` | `ap_mac` |
| 12 | `DELETE FROM rogue_aps`, `rogue_aps_history` | `ap_mac` |
| 13 | `DELETE FROM devices` | `mac` |

Steps 1–8 match no rows for a switch or an AP, so a single code path handles
every category without branching on `category`.

### `speedtests` rows survive

Step 2 nulls the link instead of deleting the row. A speedtest measures the
internet service, not the box that ran it — replace a gateway and the ISP
performance history before the swap is still meaningful, and it is usually the
most valuable long-run series the dashboard has.

`speedtests.wan_path_id` is already nullable by design, and the existing
comment in `db.py` states that "unattributed speedtests stay unattributed
rather than being inferred". A nulled link is therefore a state the schema, the
attribution code and the UI already handle correctly. Nothing new is invented.

### `wan_paths` rows do not survive

This is a deliberate narrowing of an existing invariant. `wan_paths` is
commented in four places as never pruned, but that rule is about **time-based**
pruning: an identity row must not age out merely because it is old, or history
would fragment. Deleting because the owning gateway no longer exists is a
different justification, and it strictly helps the concern documented at
`persist.py:437` and `:460`, where never-pruned rows of a replaced gateway
persist alongside its replacement and can mislead attribution's `ifname` join.

The comments in `db.py` and `live_server.py` must be updated to say "not pruned
by age" rather than "never pruned", so the invariant and the code continue to
agree.

### What is deliberately left alone

`clients.parent_mac` and `clients.parent_name` are not cleared. They are
denormalised labels recording where a client *was* attached; a client that was
on a since-removed AP genuinely was on that AP. Nulling them destroys accurate
history and gains nothing.

## Section 4 — Merging absent devices into the tick

After the tick's `devices` list is built from `fast["devices"]`, the server
appends database rows whose MAC is not in the live set and whose status is
`absent`, shaped identically to a live entry but with null live metrics
(`rxBps`, `txBps`, `uptimeSec`, `ports`).

This keeps the devices table fed by a single source. The UI does not learn a
second data path; it simply receives rows whose `status` is `"absent"`.

Because the merge reads the same durable column the sweep writes, an absent
device survives a container restart in the UI as well as in the database.

### The ten-minute gap is accepted

Gating the merge on `status = 'absent'` means a freshly forgotten device drops
out of the table for up to ten minutes before reappearing as absent. The
alternative -- merging any database device missing from the live set -- is
worse: for those ten minutes the row would still carry its last known status
and claim to be *online* while the controller no longer lists it. A brief
absence from the table is a smaller lie than a stale one.

## Section 5 — `DELETE /api/devices/{mac}`

The first write route in the application.

- **404** if the MAC is not in `devices`.
- **409** if the stored status is not `absent`.
- **200** and the full cascade otherwise.

The `absent` precondition is the safety property. It bounds what the endpoint
can destroy to devices the controller has already stopped reporting for at
least ten minutes, so a stray or malicious request cannot remove a live
device's history. It reads its authority from the same durable column the UI
renders from, so the button and the endpoint cannot disagree.

### Security note

The dashboard is published to the LAN without authentication, a risk accepted
when it was containerised — but accepted for a **read-only** service. This
route means anyone on the LAN can permanently destroy an absent device's
history with one request. The `absent` gate is what keeps that bounded. If
authentication is ever added, this endpoint is the reason.

## Section 6 — Frontend

The devices table gains a narrow trailing actions column. The trashcan renders
**only** when `status === "absent"`; every other row gets an empty cell.

Clicking it opens a confirm dialog naming the device and stating that the
deletion is permanent, then issues the `DELETE` and re-renders on success.

Three specifics that are easy to get wrong:

- The drill-down row's `colspan="9"` becomes `10`. Missing this breaks the
  port-stats layout silently.
- The button handler calls `stopPropagation()`, or clicking it also toggles the
  row's port-stats drill (`tr.clickable` at `live_dashboard.html:1490`).
- `.status-dot.absent` needs its own colour rule beside the existing `.online`
  and `.offline` at `live_dashboard.html:105`. The label needs no work —
  `${d.status[0].toUpperCase()+d.status.slice(1)}` already renders "Absent".

## Verification

Every check runs against a temporary database via `UNIFI_DB_PATH`, never
against `unifi_clients.db`.

1. A device present in the poll keeps `updated_at` fresh and is never marked.
2. A device with `state != 1` (powered off, still adopted) is **not** marked
   absent — the powered-off vs forgotten distinction holds.
3. A device absent for 11 minutes is marked `absent`; its row still exists.
4. A device absent for 8 days is deleted, and every table in the cascade is
   confirmed empty of its rows (steps 3-13; step 2 is asserted separately by
   check 5).
5. A deleted gateway's `speedtests` rows still exist with `wan_path_id IS NULL`.
6. `sweep_absent_devices(db, 0)` on a database full of stale devices deletes
   and marks **nothing** — the guard.
7. A device marked absent, then seen again, returns to `online`/`offline`
   without manual intervention.
8. `DELETE /api/devices/{mac}` returns 409 for an online device and 404 for an
   unknown MAC.
9. The tick contains an absent device with null metrics, and the browser
   renders its trashcan while online rows have none.

## Out of scope

- **Clients.** The 265 rows in `clients` have the same unbounded-growth problem
  and no time bound on the offline queries, but client churn is a different
  failure mode from infrastructure churn and deserves its own decision.
- **Undo.** Deletion is permanent by request. The seven-day grace period is the
  recovery window.
- **Authentication.** Noted in Section 5 as a consequence, not addressed here.
