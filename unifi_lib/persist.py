"""Writes fetched UniFi snapshots into the SQLite tables defined in db.py."""
import ipaddress
import re
import sqlite3
from datetime import datetime, timezone

IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")

# Subnet -> network name, rebuilt from the controller's own network config
# (see set_networks). Previously this was a hardcoded /24 prefix table, which
# silently bucketed anything it didn't know -- the WireGuard/OpenVPN subnets
# and every VPN-client network -- into "other".
_NETWORK_MAP: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str]] = []


def set_networks(networks: list[dict]) -> int:
    """Rebuild the subnet->name map from controller network config.

    A failed/empty fetch leaves the previous map in place rather than
    relabelling every client as "other" on a transient controller hiccup.
    """
    entries = []
    for n in networks:
        subnet, name = n.get("ip_subnet"), n.get("name")
        # Controller names can carry stray whitespace ("01-default-01 ").
        # Used raw, that forks one VLAN into two keys across the client table
        # and every chart, so normalise before it becomes an identifier.
        name = name.strip() if isinstance(name, str) else name
        if not subnet or not name:
            continue
        try:
            entries.append((ipaddress.ip_network(subnet, strict=False), name))
        except ValueError:
            continue
    if not entries:
        return len(_NETWORK_MAP)
    # Most-specific first: the T-Mobile WAN transit net (192.168.1.19/30)
    # sits inside the default LAN (192.168.1.1/24) and has to win for the
    # addresses it actually covers.
    entries.sort(key=lambda e: e[0].prefixlen, reverse=True)
    _NETWORK_MAP[:] = entries
    return len(entries)

DEVICE_CATEGORY_BY_TYPE = {
    "udm": "gateway", "usg": "gateway", "ugw": "gateway",
    "usw": "switch", "uap": "ap", "umbb": "cellular_gateway",
}


def as_ipv4(value) -> str | None:
    return value if isinstance(value, str) and IPV4_RE.match(value) else None


def network_for(ip: str | None) -> str | None:
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for net, name in _NETWORK_MAP:
        if addr in net:
            return name
    return "other"


def known_networks() -> list[str]:
    """Network names currently mapped, ordered for stable UI colouring."""
    return sorted({name for _, name in _NETWORK_MAP})


def device_category(raw: dict) -> str:
    return DEVICE_CATEGORY_BY_TYPE.get(raw.get("type"), "other")


def device_status(raw: dict) -> str:
    return "online" if raw.get("state") == 1 else "offline"


def epoch_to_iso(value) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def persist_clients(db: sqlite3.Connection, online: list[dict], historical: list[dict], ts: str) -> tuple[int, int]:
    online_by_mac = {c["mac"]: c for c in online if c.get("mac")}
    historical_by_mac = {c["mac"]: c for c in historical if c.get("mac")}
    all_macs = set(online_by_mac) | set(historical_by_mac)

    online_count = offline_count = 0
    for mac in all_macs:
        existing = db.execute(
            "SELECT hostname, first_seen, last_seen, vendor, parent_mac, parent_name FROM clients WHERE mac = ?", (mac,)
        ).fetchone()
        existing_hostname, existing_first_seen, existing_last_seen, existing_vendor, existing_parent_mac, existing_parent_name = (
            existing or (None, None, None, None, None, None)
        )

        is_online = mac in online_by_mac
        c = online_by_mac.get(mac) or historical_by_mac.get(mac) or {}

        hostname = (c.get("name") or c.get("hostname")) or existing_hostname
        vendor = c.get("oui") or existing_vendor
        parent_mac = c.get("last_uplink_mac") or existing_parent_mac
        parent_name = c.get("last_uplink_name") or existing_parent_name
        ip = as_ipv4(c.get("ip")) or as_ipv4(c.get("last_ip")) or as_ipv4(c.get("fixed_ip"))
        network = network_for(ip)
        connection_type = "wired" if c.get("is_wired") else "wireless" if "is_wired" in c else None
        essid = c.get("essid") if is_online else None
        signal_dbm = c.get("signal") if is_online else None
        # Only meaningful while connected -- cleared on disconnect so a stale
        # association can't be read back as a still-running uptime.
        assoc_time = c.get("assoc_time") if is_online else None

        if is_online:
            last_seen = ts
            online_count += 1
        else:
            hist_last_seen = epoch_to_iso(c.get("last_seen"))
            last_seen = max(filter(None, [existing_last_seen, hist_last_seen]), default=None)
            offline_count += 1

        first_seen = existing_first_seen or last_seen or ts

        db.execute(
            """
            INSERT INTO clients (mac, hostname, last_ip, network, connection_type, essid, signal_dbm,
                                  is_online, first_seen, last_seen, updated_at, vendor, parent_mac, parent_name,
                                  assoc_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                hostname = excluded.hostname, last_ip = excluded.last_ip, network = excluded.network,
                connection_type = COALESCE(excluded.connection_type, clients.connection_type),
                essid = excluded.essid, signal_dbm = excluded.signal_dbm, is_online = excluded.is_online,
                last_seen = COALESCE(excluded.last_seen, clients.last_seen), updated_at = excluded.updated_at,
                vendor = excluded.vendor, parent_mac = excluded.parent_mac, parent_name = excluded.parent_name,
                assoc_time = excluded.assoc_time
            """,
            (mac, hostname, ip, network, connection_type, essid, signal_dbm,
             1 if is_online else 0, first_seen, last_seen, ts, vendor, parent_mac, parent_name,
             assoc_time),
        )
    return online_count, offline_count


def persist_vlan_history(db: sqlite3.Connection, ts: str) -> None:
    rows = db.execute(
        "SELECT COALESCE(network, 'other') AS network, COUNT(*) FROM clients WHERE is_online = 1 "
        "GROUP BY COALESCE(network, 'other')"
    ).fetchall()
    for network, count in rows:
        db.execute(
            "INSERT OR REPLACE INTO vlan_client_history (ts, network, online_count) VALUES (?, ?, ?)",
            (ts, network, count),
        )


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _wan_latency(wan: dict) -> float | None:
    """0 means "standby link, not measured" rather than a real 0ms RTT."""
    return _f(wan.get("latency")) or None


# WAN path -> gateway_kind. The UDM reports both paths; wan3/WAN3 is the
# cellular failover (confirmed via active_geo_info.WAN3 -> T-Mobile ASN).
WAN_PATH_KINDS = {"WAN": "primary", "WAN3": "cellular"}


def wan_monitors(stats: dict) -> list[dict]:
    """All latency monitors for one WAN path, merging the controller's two
    separate lists. `monitors` and `alerting_monitors` are different sets --
    reading only one drops real targets (our primary WAN has 3 in each, and
    both DNS probes live in alerting_monitors). Deduped on (target, type)
    since the same host is probed over both ICMP and DNS."""
    seen, out = set(), []
    for group in ("monitors", "alerting_monitors"):
        for m in (stats.get(group) or []):
            target, mtype = m.get("target"), m.get("type")
            if not target or not mtype or (target, mtype) in seen:
                continue
            seen.add((target, mtype))
            out.append(m)
    return out


def persist_devices_and_gateways(db: sqlite3.Connection, devices: list[dict], ts: str) -> None:
    # The cellular path's latency is reported by the UDM as wan3, not by the
    # U5G device itself, so grab it up front and hand it to the cellular row.
    gw_raw = next((d for d in devices if device_category(d) == "gateway"), None)
    cellular_latency = _wan_latency((gw_raw or {}).get("wan3") or {})

    for raw in devices:
        mac = raw.get("mac")
        if not mac:
            continue
        category = device_category(raw)
        parent_name = (raw.get("uplink") or {}).get("uplink_device_name")
        db.execute(
            """
            INSERT INTO devices (mac, name, model, category, status, uptime_sec, ip, parent_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                name = excluded.name, model = excluded.model, category = excluded.category,
                status = excluded.status, uptime_sec = excluded.uptime_sec, ip = excluded.ip,
                parent_name = excluded.parent_name, updated_at = excluded.updated_at
            """,
            (mac, raw.get("name"), raw.get("model"), category, device_status(raw),
             raw.get("uptime"), raw.get("ip"), parent_name, ts),
        )

        if category == "gateway":
            _persist_primary_gateway(db, raw, mac, ts)
            _persist_port_stats(db, raw, mac, ts)
            # The UDM is the authority for BOTH WAN paths' monitors.
            _persist_rtt_monitors(db, raw, ts)
        elif category == "cellular_gateway":
            _persist_cellular_gateway(db, raw, mac, ts, cellular_latency)
        else:
            # Gateways already get bandwidth history via gateway_stats
            # (wan_rx_rate_bps/wan_tx_rate_bps) -- everything else (switch,
            # ap, other) uses its uplink port's byte-rate counters instead.
            _persist_device_bandwidth(db, raw, mac, ts)
            if category == "ap":
                _persist_ap_radios(db, raw, mac, ts)
            elif category == "switch":
                # Only switches and the gateway expose a real port_table
                # (APs/cellular gateway have an empty one).
                _persist_port_stats(db, raw, mac, ts)


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


def _persist_primary_gateway(db, raw, mac, ts):
    sys_stats = raw.get("sys_stats") or {}
    system_stats = raw.get("system-stats") or {}
    temps = {t.get("name"): t.get("value") for t in (raw.get("temperatures") or [])}
    wan = raw.get("wan1") or raw.get("wan") or {}
    db.execute(
        """
        INSERT OR REPLACE INTO gateway_stats
            (ts, gateway_mac, gateway_kind, gateway_name, cpu_pct, mem_pct, load1, load5, load15,
             uptime_sec, temp_cpu, temp_local, temp_phy, wan_rx_rate_bps, wan_tx_rate_bps,
             wan_rx_bytes_total, wan_tx_bytes_total, carrier, wan_latency_ms)
        VALUES (?, ?, 'primary', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (ts, mac, raw.get("name"),
         _f(system_stats.get("cpu")), _f(system_stats.get("mem")),
         _f(sys_stats.get("loadavg_1")), _f(sys_stats.get("loadavg_5")), _f(sys_stats.get("loadavg_15")),
         raw.get("uptime"), temps.get("CPU"), temps.get("Local"), temps.get("PHY"),
         wan.get("rx_rate"), wan.get("tx_rate"), wan.get("rx_bytes"), wan.get("tx_bytes"),
         _wan_latency(wan)),
    )


def _persist_cellular_gateway(db, raw, mac, ts, latency_ms=None):
    sys_stats = raw.get("sys_stats") or {}
    system_stats = raw.get("system-stats") or {}
    mbb = raw.get("mbb") or {}
    sim = next((s for s in (mbb.get("sim") or []) if s.get("active")), None) or {}
    carrier = sim.get("spn")
    rx_total = int(sim["rxbytes"]) if sim.get("rxbytes") else None
    tx_total = int(sim["txbytes"]) if sim.get("txbytes") else None

    prev = db.execute(
        "SELECT ts, wan_rx_bytes_total, wan_tx_bytes_total FROM gateway_stats "
        "WHERE gateway_mac = ? ORDER BY ts DESC LIMIT 1",
        (mac,),
    ).fetchone()
    rx_rate = tx_rate = None
    if prev and prev[1] is not None and rx_total is not None:
        elapsed = (datetime.fromisoformat(ts) - datetime.fromisoformat(prev[0])).total_seconds()
        if elapsed > 0:
            drx, dtx = rx_total - prev[1], (tx_total or 0) - (prev[2] or 0)
            if drx >= 0:
                rx_rate = int(drx * 8 / elapsed)
            if dtx >= 0:
                tx_rate = int(dtx * 8 / elapsed)

    db.execute(
        """
        INSERT OR REPLACE INTO gateway_stats
            (ts, gateway_mac, gateway_kind, gateway_name, cpu_pct, mem_pct, load1, load5, load15,
             uptime_sec, temp_cpu, temp_local, temp_phy, wan_rx_rate_bps, wan_tx_rate_bps,
             wan_rx_bytes_total, wan_tx_bytes_total, carrier, wan_latency_ms)
        VALUES (?, ?, 'cellular', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (ts, mac, raw.get("name"),
         _f(system_stats.get("cpu")), _f(system_stats.get("mem")),
         _f(sys_stats.get("loadavg_1")), _f(sys_stats.get("loadavg_5")), _f(sys_stats.get("loadavg_15")),
         raw.get("uptime"), rx_rate, tx_rate, rx_total, tx_total, carrier, latency_ms),
    )


def _persist_rtt_monitors(db, gw_raw, ts):
    uptime_stats = gw_raw.get("uptime_stats") or {}
    for path, kind in WAN_PATH_KINDS.items():
        for m in wan_monitors(uptime_stats.get(path) or {}):
            db.execute(
                """
                INSERT OR REPLACE INTO rtt_monitors
                    (ts, gateway_kind, target, monitor_type, latency_ms, availability)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                # 0 = unmeasured, not a real 0ms RTT to an external host.
                (ts, kind, m.get("target"), m.get("type"),
                 _f(m.get("latency_average")) or None, _f(m.get("availability"))),
            )


def _persist_device_bandwidth(db, raw, mac, ts):
    uplink = raw.get("uplink") or {}
    rx_bytes_r, tx_bytes_r = _f(uplink.get("rx_bytes-r")), _f(uplink.get("tx_bytes-r"))
    db.execute(
        """
        INSERT OR REPLACE INTO device_stats (ts, mac, rx_bps, tx_bps, rx_bytes_total, tx_bytes_total)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ts, mac, rx_bytes_r * 8 if rx_bytes_r is not None else None,
         tx_bytes_r * 8 if tx_bytes_r is not None else None,
         uplink.get("rx_bytes"), uplink.get("tx_bytes")),
    )


def _persist_port_stats(db, raw, mac, ts):
    for p in raw.get("port_table") or []:
        port_idx = p.get("port_idx")
        if port_idx is None:
            continue
        rx_bytes_r, tx_bytes_r = _f(p.get("rx_bytes-r")), _f(p.get("tx_bytes-r"))
        db.execute(
            """
            INSERT OR REPLACE INTO port_stats (ts, mac, port_idx, rx_bps, tx_bps, rx_bytes_total, tx_bytes_total)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, mac, port_idx, rx_bytes_r * 8 if rx_bytes_r is not None else None,
             tx_bytes_r * 8 if tx_bytes_r is not None else None,
             p.get("rx_bytes"), p.get("tx_bytes")),
        )


def _persist_ap_radios(db, raw, mac, ts):
    for radio in raw.get("radio_table_stats") or raw.get("radio_table") or []:
        band = radio.get("radio")
        tx_packets = radio.get("tx_packets") or 0
        tx_retries = radio.get("tx_retries") or 0
        retry_pct = round((tx_retries / tx_packets) * 100, 1) if tx_packets else 0.0
        db.execute(
            """
            INSERT OR REPLACE INTO ap_radios
                (ts, ap_mac, ap_name, band, channel, tx_power, cu_total, num_sta, tx_retries, tx_packets, retry_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, mac, raw.get("name"), band, radio.get("channel") or radio.get("last_channel"),
             radio.get("tx_power"), radio.get("cu_total"), radio.get("num_sta"),
             tx_retries, tx_packets, retry_pct),
        )


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


def persist_rogue_aps(db: sqlite3.Connection, rogue: list[dict], ts: str,
                      write_history: bool = True) -> int:
    """Upsert the latest neighbor readings, optionally appending to history.

    `rogue_aps` is latest-state and must stay fresh -- the neighbor list reads
    it, and its updated_at drives stale-neighbor pruning. `rogue_aps_history`
    is append-only and dominates database growth (one row per sighting per
    write, ~340 sightings here), so callers can write it less often than they
    refresh the current state. Signal readings change slowly enough that a
    coarser history interval loses nothing useful.
    """
    # The controller returns repeat sightings across the lookback window;
    # collapse to the single latest reading per (bssid, observing ap_mac).
    latest: dict[tuple, dict] = {}
    for r in rogue:
        bssid, ap_mac = r.get("bssid"), r.get("ap_mac")
        if not bssid or not ap_mac:
            continue
        key = (bssid, ap_mac)
        if key not in latest or (r.get("report_time") or 0) > (latest[key].get("report_time") or 0):
            latest[key] = r

    for (bssid, ap_mac), r in latest.items():
        db.execute(
            """
            INSERT INTO rogue_aps (bssid, ap_mac, ssid, channel, band, signal, vendor, ap_name, last_seen, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bssid, ap_mac) DO UPDATE SET
                ssid = excluded.ssid, channel = excluded.channel, band = excluded.band,
                signal = excluded.signal, vendor = excluded.vendor, ap_name = excluded.ap_name,
                last_seen = excluded.last_seen, updated_at = excluded.updated_at
            """,
            (bssid, ap_mac, r.get("essid") or r.get("ssid"), r.get("channel"), r.get("band"),
             r.get("signal"), r.get("oui"), r.get("ap_name"), epoch_to_iso(r.get("last_seen")), ts),
        )
        if write_history:
            db.execute(
                "INSERT OR IGNORE INTO rogue_aps_history (ts, bssid, ap_mac, ssid, channel, band, signal, vendor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, bssid, ap_mac, r.get("essid") or r.get("ssid"), r.get("channel"), r.get("band"),
                 r.get("signal"), r.get("oui")),
            )
    return len(latest)
