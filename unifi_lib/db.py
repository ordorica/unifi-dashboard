"""SQLite schema and persistence helpers shared by the cron poller and the
live server. One writer at a time is assumed per process; WAL mode lets the
live server's REST API read concurrently with its own background writer."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Container deployments point this at a mounted volume so the database
# survives image rebuilds. Default keeps the file beside the code, as before.
DB_FILE = Path(os.environ.get("UNIFI_DB_PATH")
               or Path(__file__).resolve().parent.parent / "unifi_clients.db")
RETENTION_DAYS = 30

# How long the controller must stop reporting a device before the dashboard
# says so, and before it removes the device entirely. Ten minutes rather than
# a single 60s cycle so a brief blip does not flap rows in and out of the
# table; seven days so a device unplugged for a long weekend, or a controller
# outage over a holiday, still has room to come back before anything is lost.
DEVICE_ABSENT_AFTER_MINUTES = 10
DEVICE_ABSENCE_DAYS = 7


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_FILE, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            mac TEXT PRIMARY KEY,
            hostname TEXT,
            last_ip TEXT,
            network TEXT,
            connection_type TEXT,
            essid TEXT,
            signal_dbm INTEGER,
            is_online INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_clients_online ON clients(is_online)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_clients_network ON clients(network)")

    # Migration: older DBs predate the vendor column.
    existing_cols = {row[1] for row in db.execute("PRAGMA table_info(clients)").fetchall()}
    if "vendor" not in existing_cols:
        db.execute("ALTER TABLE clients ADD COLUMN vendor TEXT")

    # Migration: older DBs predate the parent_mac/parent_name columns (which
    # device -- switch port or AP -- this client is/was attached to).
    if "parent_mac" not in existing_cols:
        db.execute("ALTER TABLE clients ADD COLUMN parent_mac TEXT")
    if "parent_name" not in existing_cols:
        db.execute("ALTER TABLE clients ADD COLUMN parent_name TEXT")

    # Migration: association time (absolute epoch seconds). Stored rather
    # than the controller's relative `uptime` counter so "connected for X"
    # can be computed fresh at query time -- a stored relative uptime would
    # be up to PERSIST_INTERVAL (60s) stale by the time it's read.
    if "assoc_time" not in existing_cols:
        db.execute("ALTER TABLE clients ADD COLUMN assoc_time INTEGER")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            mac TEXT PRIMARY KEY,
            name TEXT,
            model TEXT,
            category TEXT,
            status TEXT,
            uptime_sec INTEGER,
            ip TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    # Migration: older DBs predate the parent_name column (uplink device).
    existing_device_cols = {row[1] for row in db.execute("PRAGMA table_info(devices)").fetchall()}
    if "parent_name" not in existing_device_cols:
        db.execute("ALTER TABLE devices ADD COLUMN parent_name TEXT")

    # gateway_stats: one row per (poll, gateway). gateway_kind distinguishes
    # the primary wired WAN (UDMPRO) from the cellular failover (U5G/T-Mobile).
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_stats (
            ts TEXT NOT NULL,
            gateway_mac TEXT NOT NULL,
            gateway_kind TEXT NOT NULL,
            gateway_name TEXT,
            cpu_pct REAL,
            mem_pct REAL,
            load1 REAL,
            load5 REAL,
            load15 REAL,
            uptime_sec INTEGER,
            temp_cpu REAL,
            temp_local REAL,
            temp_phy REAL,
            wan_rx_rate_bps INTEGER,
            wan_tx_rate_bps INTEGER,
            wan_rx_bytes_total INTEGER,
            wan_tx_bytes_total INTEGER,
            carrier TEXT,
            PRIMARY KEY (ts, gateway_mac)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_gateway_stats_mac_ts ON gateway_stats(gateway_mac, ts)")

    # Migration: WAN round-trip latency. Sourced from the UDM for BOTH paths
    # (wan1 = primary, wan3 = cellular) -- the U5G device itself reports no
    # latency of its own.
    existing_gw_cols = {row[1] for row in db.execute("PRAGMA table_info(gateway_stats)").fetchall()}
    if "wan_latency_ms" not in existing_gw_cols:
        db.execute("ALTER TABLE gateway_stats ADD COLUMN wan_latency_ms REAL")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ap_radios (
            ts TEXT NOT NULL,
            ap_mac TEXT NOT NULL,
            ap_name TEXT,
            band TEXT,
            channel INTEGER,
            tx_power INTEGER,
            cu_total INTEGER,
            num_sta INTEGER,
            tx_retries INTEGER,
            tx_packets INTEGER,
            retry_pct REAL,
            PRIMARY KEY (ts, ap_mac, band)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_ap_radios_ts ON ap_radios(ts)")

    # device_stats: uplink-port bandwidth history for non-gateway devices
    # (switch/AP/other). Gateways already have this in gateway_stats
    # (wan_rx_rate_bps/wan_tx_rate_bps), so they don't write here.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS device_stats (
            ts TEXT NOT NULL,
            mac TEXT NOT NULL,
            rx_bps INTEGER,
            tx_bps INTEGER,
            rx_bytes_total INTEGER,
            tx_bytes_total INTEGER,
            PRIMARY KEY (ts, mac)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_device_stats_mac_ts ON device_stats(mac, ts)")

    # Migration: older DBs predate the cumulative byte-total columns (needed
    # to compute "usage over the last N hours" via delta rather than
    # integrating the rate samples, which is what rx_bps/tx_bps are for).
    existing_device_stats_cols = {row[1] for row in db.execute("PRAGMA table_info(device_stats)").fetchall()}
    if "rx_bytes_total" not in existing_device_stats_cols:
        db.execute("ALTER TABLE device_stats ADD COLUMN rx_bytes_total INTEGER")
    if "tx_bytes_total" not in existing_device_stats_cols:
        db.execute("ALTER TABLE device_stats ADD COLUMN tx_bytes_total INTEGER")

    # rtt_monitors: per-target WAN latency history, LEGACY and no longer
    # written (see rtt_path_monitors below) -- kept in place, rows and all,
    # same treatment as gateway_stats's old wan_* columns (see Task 8/9).
    # monitor_type is part of the key because the same target can be probed
    # two ways (the controller watches 1.1.1.1 over both ICMP and DNS).
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rtt_monitors (
            ts TEXT NOT NULL,
            gateway_kind TEXT NOT NULL,
            target TEXT NOT NULL,
            monitor_type TEXT NOT NULL,
            latency_ms REAL,
            availability REAL,
            PRIMARY KEY (ts, gateway_kind, target, monitor_type)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rtt_monitors_lookup "
        "ON rtt_monitors(gateway_kind, target, monitor_type, ts)"
    )

    # Migration (now historical): this added wan_path_id to rtt_monitors so
    # handle_rtt_history could key on it directly instead of bridging ?wan=
    # through link_type. That still left a write-time collision: gateway_kind
    # is part of rtt_monitors's primary key and only ever collapses to
    # "primary"/"cellular", so two non-cellular WAN Paths probing the same
    # target+type at the same ts would still overwrite each other via
    # INSERT OR REPLACE -- wan_path_id fixed *querying* the surviving row,
    # not the collision itself. Fixing that without widening the primary key
    # (disallowed -- additive-migration rule) meant a new table instead: see
    # rtt_path_monitors below, which is what's actually written and read now.
    # This column is consequently redundant, but dropping it would not be
    # additive, so it stays.
    existing_rtt_cols = {row[1] for row in db.execute("PRAGMA table_info(rtt_monitors)").fetchall()}
    if "wan_path_id" not in existing_rtt_cols:
        db.execute("ALTER TABLE rtt_monitors ADD COLUMN wan_path_id INTEGER")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rtt_monitors_wan_path ON rtt_monitors(wan_path_id, ts)"
    )

    # rtt_path_monitors: per-target WAN latency history keyed on the real
    # WAN Path identity, not the legacy primary/cellular label -- the
    # replacement for rtt_monitors (see the migration comment above and
    # persist._persist_rtt_monitors). wan_path_id is part of the primary key
    # here (unlike rtt_monitors's gateway_kind), which is what actually
    # separates two non-cellular WAN Paths probing the same target+type at
    # the same ts into two rows instead of one clobbering the other.
    # wan_path_id is NOT NULL: a monitor whose owning path can't be resolved
    # is skipped rather than written with a placeholder, since an
    # unattributable RTT sample has no meaning.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rtt_path_monitors (
            ts TEXT NOT NULL,
            wan_path_id INTEGER NOT NULL,
            target TEXT NOT NULL,
            monitor_type TEXT NOT NULL,
            latency_ms REAL,
            availability REAL,
            PRIMARY KEY (ts, wan_path_id, target, monitor_type)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rtt_path_monitors_path_ts ON rtt_path_monitors(wan_path_id, ts)"
    )

    # port_stats: per-physical-port bandwidth history for switches and the
    # primary gateway (the only categories with a non-empty port_table).
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS port_stats (
            ts TEXT NOT NULL,
            mac TEXT NOT NULL,
            port_idx INTEGER NOT NULL,
            rx_bps INTEGER,
            tx_bps INTEGER,
            rx_bytes_total INTEGER,
            tx_bytes_total INTEGER,
            PRIMARY KEY (ts, mac, port_idx)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_port_stats_mac_port_ts ON port_stats(mac, port_idx, ts)")

    existing_port_stats_cols = {row[1] for row in db.execute("PRAGMA table_info(port_stats)").fetchall()}
    if "rx_bytes_total" not in existing_port_stats_cols:
        db.execute("ALTER TABLE port_stats ADD COLUMN rx_bytes_total INTEGER")
    if "tx_bytes_total" not in existing_port_stats_cols:
        db.execute("ALTER TABLE port_stats ADD COLUMN tx_bytes_total INTEGER")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS speedtests (
            ts TEXT PRIMARY KEY,
            download_mbps REAL,
            upload_mbps REAL,
            latency_ms REAL,
            source TEXT NOT NULL DEFAULT 'unknown'
        )
        """
    )

    # Migration: attribute a speedtest to the WAN Path it ran over. Nullable
    # -- attribution is only possible when the controller's live status can
    # be matched to an archived record (see wan_paths below); unattributed
    # speedtests stay unattributed rather than being inferred.
    existing_st_cols = {row[1] for row in db.execute("PRAGMA table_info(speedtests)").fetchall()}
    if "wan_path_id" not in existing_st_cols:
        db.execute("ALTER TABLE speedtests ADD COLUMN wan_path_id INTEGER")

    # Migration: distinguish the two meanings of `wan_path_id IS NULL`.
    # "Not yet attributed" is healable -- _heal_unattributed_speedtests
    # re-runs the observation join on every persist_speedtests call to catch
    # rows archived before their WAN Path existed. "Deliberately detached" is
    # not: delete_device NULLs the link when a gateway is removed, keeping the
    # ISP history while cutting it loose from a path that no longer exists.
    # Without this marker the two are indistinguishable, and the healer would
    # re-credit a dead gateway's speedtests to whichever surviving gateway
    # happens to have an observation with identical throughput -- silently and
    # irreversibly. Set to 1 by delete_device; NULL means "never detached".
    if "wan_path_detached" not in existing_st_cols:
        db.execute("ALTER TABLE speedtests ADD COLUMN wan_path_detached INTEGER")

    # rogue_aps: deduped per (bssid, ap_mac) -- ap_mac is the OUR AP that
    # observed this neighbor, so multiple rows per bssid = multiple of our
    # APs can see the same neighbor, each with its own signal reading.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rogue_aps (
            bssid TEXT NOT NULL,
            ap_mac TEXT NOT NULL,
            ssid TEXT,
            channel INTEGER,
            band TEXT,
            signal INTEGER,
            vendor TEXT,
            ap_name TEXT,
            last_seen TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bssid, ap_mac)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_rogue_aps_updated ON rogue_aps(updated_at)")

    # rogue_aps_history: append-only time series (one row per sighting per
    # poll) so neighbor signal/presence can be viewed over time, up to
    # RETENTION_DAYS back. rogue_aps above stays as the fast "latest state"
    # table for the main list.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rogue_aps_history (
            ts TEXT NOT NULL,
            bssid TEXT NOT NULL,
            ap_mac TEXT NOT NULL,
            ssid TEXT,
            channel INTEGER,
            band TEXT,
            signal INTEGER,
            vendor TEXT,
            PRIMARY KEY (ts, bssid, ap_mac)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_rogue_history_bssid_ts ON rogue_aps_history(bssid, ts)")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS vlan_client_history (
            ts TEXT NOT NULL,
            network TEXT NOT NULL,
            online_count INTEGER NOT NULL,
            PRIMARY KEY (ts, network)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_vlan_history_network_ts ON vlan_client_history(network, ts)")

    # wan_paths: identity table for WAN Paths (one internet connection, as
    # the controller reports it via its own inventory key such as `WAN` or
    # `WAN3`) -- distinct from the Gateway Device that owns it. This is
    # deliberately NOT a time series and is never pruned by age
    # (`delete_device` still removes a gateway's paths when the gateway
    # itself is removed from the controller): history is keyed on
    # wan_paths.id via wan_stats, so pruning identities would orphan that
    # history. See CONTEXT.md for the WAN Path / Gateway Device distinction.
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

    # speedtest_observations: raw speedtest-status readings as observed,
    # persisted so attribution (see CONTEXT.md) survives a restart.
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

    # Migration: scope observations to the gateway that produced them.
    # Multi-gateway networks can have two gateways report the same ifname
    # (e.g. both a eth9), and wan_paths is deliberately never pruned by age, so a
    # replaced gateway's rows persist alongside its replacement's -- without
    # this, attribution's join on ifname alone can match the wrong gateway's
    # WAN Path. Not part of the primary key: widening the PK would require
    # rebuilding the table, breaking the additive-migration rule, and the
    # PK's job is re-observation idempotence, not join scoping.
    existing_obs_cols = {row[1] for row in db.execute("PRAGMA table_info(speedtest_observations)").fetchall()}
    if "gateway_mac" not in existing_obs_cols:
        db.execute("ALTER TABLE speedtest_observations ADD COLUMN gateway_mac TEXT")

    db.commit()


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
        # wan_path_detached marks these rows as deliberately cut loose, so
        # _heal_unattributed_speedtests leaves them alone. Without it the
        # healer cannot tell them from rows that were never attributed, and
        # would re-credit this dead gateway's history to a surviving gateway
        # whose observation happens to match the same throughput.
        db.execute(
            f"UPDATE speedtests SET wan_path_id = NULL, wan_path_detached = 1 "
            f"WHERE wan_path_id IN ({marks})",
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


def prune_old(db: sqlite3.Connection, retention_days: int = RETENTION_DAYS) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    db.execute("DELETE FROM gateway_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM ap_radios WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM device_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM port_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM rtt_monitors WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM rtt_path_monitors WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM speedtests WHERE ts < ?", (cutoff,))
    # wan_paths is deliberately NOT pruned by age here (see `delete_device`
    # for the one case that does remove a path row) -- it is identity, not a
    # time series, and wan_stats rows above are keyed on wan_paths.id.
    # Pruning wan_paths would orphan wan_stats history and break the whole
    # point of this design: continuity of a WAN Path's history across
    # restarts.
    db.execute("DELETE FROM wan_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM speedtest_observations WHERE observed_at < ?", (cutoff,))
    db.execute("DELETE FROM vlan_client_history WHERE ts < ?", (cutoff,))
    # rogue_aps isn't a time series (one row per bssid+ap_mac, upserted in
    # place), but drop entries nobody has seen in a while so stale/moved
    # neighbors fall out of the list.
    db.execute("DELETE FROM rogue_aps WHERE updated_at < ?", (cutoff,))
    db.execute("DELETE FROM rogue_aps_history WHERE ts < ?", (cutoff,))
