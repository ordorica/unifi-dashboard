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

    # rtt_monitors: per-target WAN latency history. monitor_type is part of
    # the key because the same target can be probed two ways (the controller
    # watches 1.1.1.1 over both ICMP and DNS).
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
    # deliberately NOT a time series and is never pruned: history is keyed
    # on wan_paths.id via wan_stats, so pruning identities would orphan that
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

    db.commit()


def prune_old(db: sqlite3.Connection, retention_days: int = RETENTION_DAYS) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    db.execute("DELETE FROM gateway_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM ap_radios WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM device_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM port_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM rtt_monitors WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM speedtests WHERE ts < ?", (cutoff,))
    # wan_paths is deliberately NOT pruned here -- it is identity, not a time
    # series, and wan_stats rows above are keyed on wan_paths.id. Pruning
    # wan_paths would orphan wan_stats history and break the whole point of
    # this design: continuity of a WAN Path's history across restarts.
    db.execute("DELETE FROM wan_stats WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM speedtest_observations WHERE observed_at < ?", (cutoff,))
    db.execute("DELETE FROM vlan_client_history WHERE ts < ?", (cutoff,))
    # rogue_aps isn't a time series (one row per bssid+ap_mac, upserted in
    # place), but drop entries nobody has seen in a while so stale/moved
    # neighbors fall out of the list.
    db.execute("DELETE FROM rogue_aps WHERE updated_at < ?", (cutoff,))
    db.execute("DELETE FROM rogue_aps_history WHERE ts < ?", (cutoff,))
