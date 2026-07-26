#!/usr/bin/env python3
"""
Live UniFi Network Monitor.

Replaces the 5-minute cron with a persistent process that:
  - keeps one long-lived controller session open (fast_loop polls it every
    ~5s for cheap live numbers and pushes them to connected browsers over
    a WebSocket)
  - persists a durable snapshot to SQLite every ~60s (persist_loop) so the
    history charts/VLAN history have real data to render
  - refreshes the heavier calls (speedtest archive, neighbor AP scan) every
    ~10 minutes (slow_loop), since those change slowly and are expensive

Serves the dashboard + a small JSON API on http://127.0.0.1:8787 (loopback
only -- this process holds live UniFi controller credentials, so it isn't
exposed beyond this machine).

Run via: uv run --with unifi-core --with aiounifi python3 live_server.py
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web, WSMsgType

sys.path.insert(0, str(Path(__file__).resolve().parent))
from unifi_lib import db, persist
from unifi_lib.fetch import UnifiSession

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
# Loopback default preserves the macOS/launchd behaviour; the container
# overrides BIND_HOST to 0.0.0.0, since 127.0.0.1 inside a container is
# unreachable from the host.
HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("BIND_PORT", "8787"))

FAST_INTERVAL = 1
PERSIST_INTERVAL = 60
SLOW_INTERVAL = 60
# Neighbor *history* is appended on its own, slower cadence than SLOW_INTERVAL.
# It is by far the largest table (one row per sighting per write, ~340 here),
# and neighbor signal levels drift slowly enough that a 5-minute sample loses
# nothing. The current-state table still refreshes every SLOW_INTERVAL.
ROGUE_HISTORY_INTERVAL = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("live_server")


class State:
    def __init__(self):
        self.session: UnifiSession | None = None
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.latest_tick: dict | None = None
        self.last_fast: dict | None = None
        self.prev_bytes: dict[str, tuple[str, int, int]] = {}  # gateway_mac -> (ts, rx_total, tx_total)
        self.vendor_cache: dict[str, str] = {}  # mac -> vendor, refreshed each persist cycle
        self.flow_cache: dict[str, tuple[float, object]] = {}  # key -> (fetched_at, payload)
        self.last_rogue_history: float = 0.0  # monotonic clock of last history append


state = State()


# ---------------------------------------------------------------------------
# Field extraction (pure, no I/O) -- shared shape between the live tick JSON
# and what persist.py writes to SQLite, kept deliberately small/duplicated
# rather than over-abstracted across the two call sites.
# ---------------------------------------------------------------------------
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _uptime_from_assoc(assoc_time):
    """Seconds connected, derived from the absolute association epoch so the
    figure is correct at read time rather than as of the last 60s snapshot."""
    if not assoc_time:
        return None
    elapsed = int(datetime.now(timezone.utc).timestamp()) - int(assoc_time)
    return elapsed if elapsed >= 0 else None


def build_gateway_tick(raw: dict, kind: str) -> dict:
    sys_stats = raw.get("sys_stats") or {}
    system_stats = raw.get("system-stats") or {}
    mac = raw.get("mac")
    base = {
        "mac": mac,
        "name": raw.get("name"),
        "kind": kind,
        "status": persist.device_status(raw),
        "cpu": _f(system_stats.get("cpu")),
        "mem": _f(system_stats.get("mem")),
        "load1": _f(sys_stats.get("loadavg_1")),
        "load5": _f(sys_stats.get("loadavg_5")),
        "load15": _f(sys_stats.get("loadavg_15")),
        "uptimeSec": raw.get("uptime"),
    }
    if kind == "primary":
        temps = {t.get("name"): t.get("value") for t in (raw.get("temperatures") or [])}
        wan = raw.get("wan1") or raw.get("wan") or {}
        base.update({
            "tempCpu": temps.get("CPU"), "tempLocal": temps.get("Local"), "tempPhy": temps.get("PHY"),
            "rxRateBps": wan.get("rx_rate"), "txRateBps": wan.get("tx_rate"),
            "rxBytesTotal": wan.get("rx_bytes"), "txBytesTotal": wan.get("tx_bytes"),
        })
    else:
        mbb = raw.get("mbb") or {}
        sim = next((s for s in (mbb.get("sim") or []) if s.get("active")), None) or {}
        rx_total = int(sim["rxbytes"]) if sim.get("rxbytes") else None
        tx_total = int(sim["txbytes"]) if sim.get("txbytes") else None
        now_ts = persist.now_iso()
        rx_rate = tx_rate = None
        prev = state.prev_bytes.get(mac)
        if prev and rx_total is not None:
            elapsed = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(prev[0])).total_seconds()
            if elapsed > 0:
                drx, dtx = rx_total - prev[1], (tx_total or 0) - prev[2]
                if drx >= 0:
                    rx_rate = int(drx * 8 / elapsed)
                if dtx >= 0:
                    tx_rate = int(dtx * 8 / elapsed)
        if rx_total is not None:
            state.prev_bytes[mac] = (now_ts, rx_total, tx_total or 0)
        base.update({
            "carrier": sim.get("spn"), "hasCarrier": sim.get("has_carrier"),
            "rxRateBps": rx_rate, "txRateBps": tx_rate,
            "rxBytesTotal": rx_total, "txBytesTotal": tx_total,
        })
    return base


def _rtt_path(wan: dict, stats: dict) -> dict:
    """One WAN path's RTT view. `latency` reads 0 on a standby failover link
    that isn't carrying traffic, which is not a real 0ms measurement -- report
    it as None so the UI can say "standby" instead of a bogus 0."""
    latency = wan.get("latency")
    # Same 0-means-unmeasured rule per target: an external ICMP/DNS target
    # cannot genuinely average 0ms, so it's absence of data, not a reading.
    monitors = [
        {"target": m.get("target"), "type": m.get("type"),
         "latencyMs": m.get("latency_average") or None, "availability": m.get("availability")}
        for m in persist.wan_monitors(stats)
    ]
    return {
        "latencyMs": latency if latency else None,
        "up": wan.get("up", True),
        "availability": stats.get("availability"),
        "latencyAvg24h": stats.get("latency_average"),
        "monitors": monitors,
    }


def build_rtt_tick(gw_raw: dict) -> dict:
    """RTT for both WAN paths. The UDM is the authority for both -- wan1 is
    the wired primary, wan3 the cellular failover (confirmed via
    active_geo_info.WAN3 resolving to the T-Mobile ASN)."""
    uptime_stats = gw_raw.get("uptime_stats") or {}
    return {
        "primary": _rtt_path(gw_raw.get("wan1") or gw_raw.get("wan") or {}, uptime_stats.get("WAN") or {}),
        "cellular": _rtt_path(gw_raw.get("wan3") or {}, uptime_stats.get("WAN3") or {}),
    }


def build_ap_tick(raw: dict) -> list[dict]:
    out = []
    for radio in raw.get("radio_table_stats") or raw.get("radio_table") or []:
        tx_packets = radio.get("tx_packets") or 0
        tx_retries = radio.get("tx_retries") or 0
        retry_pct = round((tx_retries / tx_packets) * 100, 1) if tx_packets else 0.0
        out.append({
            "mac": raw.get("mac"), "name": raw.get("name"), "status": persist.device_status(raw),
            "band": radio.get("radio"), "channel": radio.get("channel") or radio.get("last_channel"),
            "txPower": radio.get("tx_power"), "cuTotal": radio.get("cu_total"),
            "numSta": radio.get("num_sta"), "retryPct": retry_pct,
            "uptimeSec": raw.get("uptime"),  # the AP's uptime, not the radio's
        })
    return out


def build_port_list(raw: dict) -> list[dict]:
    # Only switches and the primary gateway expose a real port_table (APs
    # and the cellular gateway have an empty one) -- caller only invokes
    # this for those two categories.
    out = []
    for p in raw.get("port_table") or []:
        rx_bytes_r, tx_bytes_r = _f(p.get("rx_bytes-r")), _f(p.get("tx_bytes-r"))
        out.append({
            "idx": p.get("port_idx"), "name": p.get("name"),
            "up": p.get("up"), "enabled": p.get("enable", p.get("enabled")),
            "speed": p.get("speed"), "poe": p.get("poe_enable"), "poePower": _f(p.get("poe_power")),
            "isUplink": p.get("is_uplink"), "clientMac": (p.get("last_connection") or {}).get("mac"),
            "uptimeSec": p.get("uptime"),  # absent on down ports
            "rxBps": rx_bytes_r * 8 if rx_bytes_r is not None else None,
            "txBps": tx_bytes_r * 8 if tx_bytes_r is not None else None,
        })
    return out


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------
async def broadcast(payload: dict):
    if not state.ws_clients:
        return
    msg = json.dumps(payload)
    dead = set()
    for ws in state.ws_clients:
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    state.ws_clients -= dead


async def fast_loop():
    while True:
        try:
            fast = await state.session.fetch_fast()
            state.last_fast = fast

            online_by_mac = {c["mac"]: c for c in fast["online"] if c.get("mac")}
            vlan_counts: dict[str, int] = {}
            online_clients = []
            for c in online_by_mac.values():
                ip = persist.as_ipv4(c.get("ip")) or persist.as_ipv4(c.get("last_ip")) or persist.as_ipv4(c.get("fixed_ip"))
                net = persist.network_for(ip) or "other"
                vlan_counts[net] = vlan_counts.get(net, 0) + 1
                tx_rate_kbps, rx_rate_kbps = _f(c.get("tx_rate")), _f(c.get("rx_rate"))
                online_clients.append({
                    "mac": c.get("mac"), "hostname": c.get("name") or c.get("hostname"), "ip": ip,
                    "network": net, "connType": "wired" if c.get("is_wired") else "wireless",
                    "essid": c.get("essid"), "signal": c.get("signal"),
                    "vendor": c.get("oui") or state.vendor_cache.get(c.get("mac")),
                    "parentMac": c.get("last_uplink_mac"), "parentName": c.get("last_uplink_name"),
                    # Wireless-only PHY link stats -- negotiated rate, not
                    # actual throughput (that's rxBps/txBps on devices).
                    # tx_rate/rx_rate arrive in Kbps; convert to bps so the
                    # frontend can reuse fmtBits() like every other rate field.
                    "channel": c.get("channel"), "band": c.get("radio"),
                    "txRate": tx_rate_kbps * 1000 if tx_rate_kbps is not None else None,
                    "rxRate": rx_rate_kbps * 1000 if rx_rate_kbps is not None else None,
                    "uptimeSec": c.get("uptime"),
                })

            gateways, aps, devices = {}, [], []
            rtt = {}
            for raw in fast["devices"]:
                cat = persist.device_category(raw)
                uplink = raw.get("uplink") or {}
                rx_bps = tx_bps = None
                if cat == "gateway":
                    gw = build_gateway_tick(raw, "primary")
                    gateways["primary"] = gw
                    rtt = build_rtt_tick(raw)
                    rx_bps, tx_bps = gw["rxRateBps"], gw["txRateBps"]
                elif cat == "cellular_gateway":
                    gw = build_gateway_tick(raw, "cellular")
                    gateways["cellular"] = gw
                    rx_bps, tx_bps = gw["rxRateBps"], gw["txRateBps"]
                else:
                    if cat == "ap":
                        aps.extend(build_ap_tick(raw))
                    # Non-gateway devices don't carry their own rx_rate/tx_rate --
                    # the uplink port's byte-rate counters are the closest proxy
                    # for "how much traffic is this device pushing/pulling".
                    rx_bytes_r, tx_bytes_r = _f(uplink.get("rx_bytes-r")), _f(uplink.get("tx_bytes-r"))
                    rx_bps = rx_bytes_r * 8 if rx_bytes_r is not None else None
                    tx_bps = tx_bytes_r * 8 if tx_bytes_r is not None else None
                # Only switches and the primary gateway expose real physical
                # ports (APs/cellular gateway have an empty port_table).
                ports = build_port_list(raw) if cat in ("switch", "gateway") else []
                devices.append({
                    "mac": raw.get("mac"), "name": raw.get("name"), "model": raw.get("model"),
                    "category": cat, "status": persist.device_status(raw), "uptimeSec": raw.get("uptime"),
                    "ip": raw.get("ip"), "parent": uplink.get("uplink_device_name"),
                    "parentMac": uplink.get("uplink_mac"),
                    "rxBps": rx_bps, "txBps": tx_bps, "ports": ports,
                })

            tick = {
                "type": "tick", "ts": persist.now_iso(),
                "gateways": gateways, "aps": aps, "devices": devices, "rtt": rtt,
                "clientsOnline": len(online_by_mac), "vlanCounts": vlan_counts,
                "onlineClients": online_clients,
            }
            state.latest_tick = tick
            await broadcast(tick)
        except Exception:
            log.exception("fast_loop tick failed")
        await asyncio.sleep(FAST_INTERVAL)


async def persist_loop():
    while True:
        await asyncio.sleep(PERSIST_INTERVAL)
        try:
            fast = state.last_fast or await state.session.fetch_fast()
            historical = await state.session.fetch_historical_clients()
            # Networks change rarely, but re-reading them here means a newly
            # added VLAN starts labelling clients within a minute.
            persist.set_networks(await state.session.fetch_networks())
            ts = persist.now_iso()
            conn = db.connect()
            persist.persist_clients(conn, fast["online"], historical, ts)
            persist.persist_vlan_history(conn, ts)
            persist.persist_devices_and_gateways(conn, fast["devices"], ts)
            db.prune_old(conn)
            conn.commit()

            offline_rows = conn.execute(
                "SELECT mac, hostname, last_ip, network, connection_type, last_seen, vendor, parent_mac, parent_name "
                "FROM clients WHERE is_online = 0 ORDER BY last_seen DESC"
            ).fetchall()
            state.vendor_cache = dict(conn.execute("SELECT mac, vendor FROM clients WHERE vendor IS NOT NULL").fetchall())
            conn.close()

            await broadcast({
                "type": "offline-update",
                "offline": [
                    {"mac": r[0], "hostname": r[1], "ip": r[2], "network": r[3], "connType": r[4], "lastSeen": r[5],
                     "vendor": r[6], "parentMac": r[7], "parentName": r[8]}
                    for r in offline_rows
                ],
            })
            log.info("persist_loop: snapshot written")
        except Exception:
            log.exception("persist_loop failed")


async def slow_loop():
    while True:
        try:
            speedtests = await state.session.fetch_speedtests(duration_hours=24)
            rogue = await state.session.fetch_rogue_aps(within_hours=2)
            ts = persist.now_iso()
            now = time.monotonic()
            write_history = (now - state.last_rogue_history) >= ROGUE_HISTORY_INTERVAL
            conn = db.connect()
            st_count = persist.persist_speedtests(conn, speedtests)
            rogue_count = persist.persist_rogue_aps(conn, rogue, ts, write_history=write_history)
            conn.commit()
            neighbors_payload = build_neighbors_payload(conn)
            conn.close()
            if write_history:
                state.last_rogue_history = now
            await broadcast({"type": "neighbors-update", "neighbors": neighbors_payload})
            log.info("slow_loop: %d new speedtests, %d neighbor readings%s",
                     st_count, rogue_count, " (history appended)" if write_history else "")
        except Exception:
            log.exception("slow_loop failed")
        await asyncio.sleep(SLOW_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP / WebSocket routes
# ---------------------------------------------------------------------------
async def handle_index(request):
    return web.FileResponse(STATIC_DIR / "live_dashboard.html")


async def handle_ws(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    state.ws_clients.add(ws)
    if state.latest_tick:
        await ws.send_str(json.dumps(state.latest_tick))
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        state.ws_clients.discard(ws)
    return ws


def _bucket_query(range_key: str) -> tuple[str, str]:
    """Returns (cutoff_iso, sqlite strftime bucket format) for a range key.
    Data is persisted once per PERSIST_INTERVAL (60s), so minute-level
    buckets are the finest resolution that actually means anything -- no
    point aggregating to hourly and throwing away real samples."""
    now = datetime.now(timezone.utc)
    if range_key == "24h":
        return (now - timedelta(hours=24)).isoformat(), "%Y-%m-%dT%H:%M:00"
    if range_key == "14d":
        return (now - timedelta(days=14)).isoformat(), "%Y-%m-%dT%H:%M:00"
    if range_key == "30d":
        return (now - timedelta(days=30)).isoformat(), "%Y-%m-%dT%H:%M:00"
    return (now - timedelta(days=7)).isoformat(), "%Y-%m-%dT%H:%M:00"  # default 7d


async def handle_wan_history(request):
    """Gateway health history: throughput, CPU/mem, load average, temps.
    Serves both the WAN traffic chart and per-tile drill-down charts."""
    gateway = request.query.get("gateway", "primary")
    range_key = request.query.get("range", "7d")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket,
               AVG(wan_rx_rate_bps), AVG(wan_tx_rate_bps), AVG(cpu_pct), AVG(mem_pct),
               AVG(load1), AVG(load5), AVG(load15), AVG(temp_cpu), AVG(wan_latency_ms)
        FROM gateway_stats
        WHERE gateway_kind = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (gateway, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([
        {"t": r[0], "rxRateBps": r[1], "txRateBps": r[2], "cpu": r[3], "mem": r[4],
         "load1": r[5], "load5": r[6], "load15": r[7], "tempCpu": r[8], "latencyMs": r[9]}
        for r in rows
    ])


async def handle_ap_history(request):
    mac = request.match_info["mac"]
    band = request.match_info["band"]
    range_key = request.query.get("range", "24h")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket, AVG(cu_total), AVG(num_sta), AVG(retry_pct)
        FROM ap_radios WHERE ap_mac = ? AND band = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (mac, band, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([{"t": r[0], "cuTotal": r[1], "numSta": r[2], "retryPct": r[3]} for r in rows])


async def handle_device_history(request):
    """Uplink-port bandwidth history for a non-gateway device (switch/AP/other).
    Gateways use /api/history/wan instead -- their rate history already lives
    in gateway_stats, no need for a second copy in device_stats."""
    mac = request.match_info["mac"]
    range_key = request.query.get("range", "24h")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket, AVG(rx_bps), AVG(tx_bps)
        FROM device_stats WHERE mac = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (mac, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([{"t": r[0], "rxRateBps": r[1], "txRateBps": r[2]} for r in rows])


async def handle_port_history(request):
    mac = request.match_info["mac"]
    try:
        port_idx = int(request.match_info["idx"])
    except ValueError:
        return web.json_response([])
    range_key = request.query.get("range", "24h")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket, AVG(rx_bps), AVG(tx_bps)
        FROM port_stats WHERE mac = ? AND port_idx = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (mac, port_idx, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([{"t": r[0], "rxRateBps": r[1], "txRateBps": r[2]} for r in rows])


# ---------------------------------------------------------------------------
# Traffic flows. These are expensive (thousands of rows per call) and the
# controller is the system of record -- we query it on demand and memoise
# briefly rather than persisting our own copy, which would balloon the DB at
# ~10k flows/hour for no added value.
# ---------------------------------------------------------------------------
FLOW_CACHE_TTL = 30.0


async def _cached_flow(key: str, producer):
    hit = state.flow_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < FLOW_CACHE_TTL:
        return hit[1]
    value = await producer()
    state.flow_cache[key] = (now, value)
    return value


def _count_by(rows, key_fn):
    counts: dict[str, int] = {}
    for r in rows:
        k = key_fn(r)
        if k:
            counts[k] = counts.get(k, 0) + 1
    return [{"key": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def _flow_row(f: dict) -> dict:
    src, dst = f.get("source") or {}, f.get("destination") or {}
    return {
        "id": f.get("id"), "time": f.get("time"),
        "action": f.get("action"), "risk": f.get("risk"),
        "service": f.get("service"), "protocol": f.get("protocol"),
        "direction": f.get("direction"), "count": f.get("count"),
        "bytesTotal": f.get("bytes_total"), "bytesRx": f.get("bytes_rx"), "bytesTx": f.get("bytes_tx"),
        "durationMs": f.get("duration_milliseconds"),
        "srcName": src.get("name"), "srcIp": src.get("ip"), "srcMac": src.get("mac"),
        "srcNetwork": src.get("network_name"), "srcZone": src.get("zone_name"),
        "dstName": dst.get("name"), "dstIp": dst.get("ip"), "dstMac": dst.get("mac"),
        "dstNetwork": dst.get("network_name"), "dstZone": dst.get("zone_name"),
        "domains": dst.get("domains") or src.get("domains") or [],
        "policies": [p.get("name") for p in (f.get("policies") or []) if p.get("name")],
    }


async def handle_flow_stats(request):
    period = request.query.get("period", "DAY").upper()
    top = max(1, min(int(request.query.get("top", 10) or 10), 50))
    stats = await _cached_flow(
        f"stats:{period}:{top}", lambda: state.session.fetch_flow_stats(period=period, top=top)
    )

    def risk_total(d):
        return sum((d or {}).values())

    allowed_by_risk = stats.get("allowed_count_by_risk") or {}
    blocked_by_risk = stats.get("blocked_count_by_risk") or {}
    all_by_region = stats.get("all_count_by_region") or {}
    blocked_by_region = stats.get("blocked_count_by_region") or {}
    regions = sorted(
        ({"region": k, "total": v, "blocked": blocked_by_region.get(k, 0)} for k, v in all_by_region.items()),
        key=lambda r: -r["total"],
    )
    return web.json_response({
        "period": period,
        "allowed": risk_total(allowed_by_risk),
        "blocked": risk_total(blocked_by_risk),
        "allowedByRisk": allowed_by_risk,
        "blockedByRisk": blocked_by_risk,
        "regions": regions,
        "topClients": stats.get("top_clients") or [],
        "topBlockedClients": stats.get("top_blocked_clients") or [],
        "topDestinations": stats.get("top_destinations") or [],
        # application_name/category_name come back null on this controller --
        # pass them through so the UI can fall back to the numeric id rather
        # than inventing a label.
        "topApplications": stats.get("top_applications") or [],
        "topBlockedPolicies": stats.get("top_blocked_policies") or [],
    })


async def handle_flows_recent(request):
    hours = max(1, min(int(request.query.get("hours", 1) or 1), 24))
    sample = await _cached_flow(
        f"recent:{hours}", lambda: state.session.fetch_flows(hours=hours, page_size=1000)
    )
    rows = [_flow_row(f) for f in sample]
    return web.json_response({
        "hours": hours,
        # Breakdowns describe THIS sample (newest N flows), not the whole
        # period -- the flow list is capped, so totals live in /api/flows/stats.
        "sampleSize": len(rows),
        "byAction": _count_by(sample, lambda f: f.get("action")),
        "byProtocol": _count_by(sample, lambda f: f.get("protocol")),
        "byService": _count_by(sample, lambda f: f.get("service")),
        "byDirection": _count_by(sample, lambda f: f.get("direction")),
        "flows": rows,
    })


# ---------------------------------------------------------------------------
# Health / WiFi diagnostics. All slow-changing, so they share the flow cache's
# TTL memoisation rather than being polled on the fast tick.
# ---------------------------------------------------------------------------
EVENT_PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+)\}")


def _render_event(e: dict) -> dict:
    """Turn the controller's templated message into readable text.

    message_raw looks like "{CLIENT} roamed from {DEVICE_FROM} to {DEVICE_TO}"
    with a parallel `parameters` map holding {"name": ...} for each token.
    Unknown tokens are left as-is rather than blanked, so a firmware change
    that adds a placeholder degrades to visible text instead of a hole.
    """
    params = e.get("parameters") or {}

    def sub(m):
        v = params.get(m.group(1))
        if isinstance(v, dict):
            return str(v.get("name") or v.get("hostname") or v.get("id") or m.group(0))
        return str(v) if v is not None else m.group(0)

    raw = e.get("message_raw") or ""
    client = params.get("CLIENT") or {}
    device = params.get("DEVICE") or params.get("DEVICE_TO") or {}
    return {
        "id": e.get("id"),
        "time": e.get("timestamp") or e.get("time"),
        "severity": e.get("severity"),
        "category": e.get("category"),
        "subcategory": e.get("subcategory"),
        "key": e.get("key"),
        "message": EVENT_PLACEHOLDER_RE.sub(sub, raw).strip(),
        "clientName": client.get("name") or client.get("hostname"),
        "clientMac": client.get("id"),
        "deviceName": device.get("name"),
    }


def _name_lookup(conn) -> dict:
    """mac -> friendly name, across both clients and infrastructure devices,
    so an anomaly keyed only by MAC can be shown as something recognisable."""
    names = {}
    for mac, hostname in conn.execute(
        "SELECT mac, hostname FROM clients WHERE hostname IS NOT NULL"
    ).fetchall():
        names[mac] = hostname
    for mac, name in conn.execute(
        "SELECT mac, name FROM devices WHERE name IS NOT NULL"
    ).fetchall():
        names[mac] = name
    return names


async def handle_health(request):
    async def produce():
        events, anomalies, subsystems, sysinfo, alarms, agg = await asyncio.gather(
            state.session.fetch_events(),
            state.session.fetch_anomalies(),
            state.session.fetch_network_health(),
            state.session.fetch_system_info(),
            state.session.fetch_alarms(),
            state.session.fetch_aggregated_dashboard(),
        )
        conn = db.connect()
        names = _name_lookup(conn)
        conn.close()

        anom = []
        for a in anomalies:
            stamps = a.get("timestamps") or []
            mac = a.get("mac")
            anom.append({
                "anomaly": a.get("anomaly"), "mac": mac,
                "name": names.get(mac), "count": len(stamps),
                "lastSeen": max(stamps) if stamps else None,
            })
        anom.sort(key=lambda x: (-(x["lastSeen"] or 0), -x["count"]))

        internet = agg.get("internet") or {}
        history = internet.get("health_history") or []
        issues = [h for h in history
                  if h.get("wan_downtime") or h.get("high_latency")
                  or h.get("packet_loss") or h.get("failover_wan_active")]
        return {
            "subsystems": [
                {"subsystem": s.get("subsystem"), "status": s.get("status"),
                 "numUser": s.get("num_user"), "numAp": s.get("num_ap"),
                 "numSw": s.get("num_sw"), "numGw": s.get("num_gw"),
                 "numDisconnected": s.get("num_disconnected"), "numPending": s.get("num_pending"),
                 "wanIp": s.get("wan_ip"), "latency": s.get("latency"), "drops": s.get("drops"),
                 "uptime": s.get("uptime"), "xputUp": s.get("xput_up"), "xputDown": s.get("xput_down"),
                 "speedtestPing": s.get("speedtest_ping"), "gwVersion": s.get("gw_version")}
                for s in subsystems
            ],
            "system": {
                "version": sysinfo.get("version"),
                "consoleVersion": sysinfo.get("console_display_version"),
                "updateAvailable": sysinfo.get("update_available"),
                "uptimeSec": sysinfo.get("uptime"),
                "name": sysinfo.get("name"),
                "retentionDays": sysinfo.get("data_retention_days"),
            },
            "ips": agg.get("cybersecure") or {},
            "internetHealth": {
                "samples": len(history),
                "issues": len(issues),
                "windowStart": history[0].get("timestamp") if history else None,
                "windowEnd": history[-1].get("timestamp") if history else None,
                "recentIssues": issues[-20:],
            },
            "anomalies": anom,
            "alarms": alarms,
            "events": [_render_event(e) for e in events],
        }

    return web.json_response(await _cached_flow("health", produce))


def _fw_endpoint(ep: dict, zones: dict, groups: dict) -> dict:
    """Flatten a policy's source/destination into readable text.

    The controller stores these as id references (zone_id, ip_group_id), so
    without resolving them the UI would show opaque hex strings.
    """
    ep = ep or {}
    group = groups.get(ep.get("ip_group_id") or "")
    if group:
        what = group["name"]
    elif ep.get("ips"):
        what = ", ".join(ep["ips"])
    elif ep.get("match_mac") and ep.get("macs"):
        what = ", ".join(ep["macs"])
    else:
        what = "Any"
    port_type = ep.get("port_matching_type")
    ports = None
    if port_type and port_type != "ANY":
        ports = ep.get("port") or ep.get("ports") or (
            f"{ep.get('port_group_id') and groups.get(ep['port_group_id'], {}).get('name') or port_type}"
        )
    return {
        "zone": zones.get(ep.get("zone_id") or "", "Any"),
        "what": what,
        "ports": str(ports) if ports else None,
    }


async def handle_firewall(request):
    async def produce():
        fw = await state.session.fetch_firewall()
        zones = {z.get("_id"): z.get("name") for z in fw["zones"] if z.get("_id")}
        groups = {
            g.get("_id"): {"name": g.get("name"), "type": g.get("group_type"),
                           "members": g.get("group_members") or []}
            for g in fw["groups"] if g.get("_id")
        }
        policies = []
        for p in fw["policies"]:
            policies.append({
                "id": p.get("_id"), "name": p.get("name"), "action": p.get("action"),
                "enabled": p.get("enabled"), "index": p.get("index"),
                "predefined": p.get("predefined"), "protocol": p.get("protocol"),
                "ipVersion": p.get("ip_version"), "logging": p.get("logging"),
                "hits": p.get("hits"), "lastHit": p.get("last_hit"),
                "source": _fw_endpoint(p.get("source"), zones, groups),
                "destination": _fw_endpoint(p.get("destination"), zones, groups),
                "connectionStates": p.get("connection_states") or [],
                "scheduleMode": (p.get("schedule") or {}).get("mode"),
            })
        # Most-active first; unfired rules (hits None) sort last so the rules
        # actually doing work are what you see immediately.
        policies.sort(key=lambda p: -(p["hits"] or 0))
        return {
            "policies": policies,
            "portForwards": [
                {"id": f.get("_id"), "name": f.get("name"), "enabled": f.get("enabled"),
                 "proto": f.get("proto"), "wanPort": f.get("dst_port"),
                 "fwdIp": f.get("fwd"), "fwdPort": f.get("fwd_port"),
                 "src": f.get("src"), "log": f.get("log"), "interface": f.get("pfwd_interface")}
                for f in fw["portForwards"]
            ],
            "zones": [{"id": z.get("_id"), "name": z.get("name"), "key": z.get("zone_key")}
                      for z in fw["zones"]],
            "groups": sorted(
                [{"id": gid, **g} for gid, g in groups.items()],
                key=lambda g: (g["type"] or "", g["name"] or ""),
            ),
        }

    return web.json_response(await _cached_flow("firewall", produce))


async def handle_routing(request):
    """QoS shaping, policy-based routes and VPN tunnels -- everything that
    decides *where* traffic goes, as opposed to whether it's allowed."""
    async def produce():
        r, nets = await asyncio.gather(
            state.session.fetch_routing(), state.session.fetch_networks()
        )
        net_names = {n.get("_id"): (n.get("name") or "").strip() for n in nets if n.get("_id")}

        def qos_target(t):
            t = t or {}
            if t.get("app_ids"):
                return f"{len(t['app_ids'])} applications"
            if t.get("app_category_ids"):
                return f"{len(t['app_category_ids'])} app categories"
            if t.get("ips"):
                return ", ".join(t["ips"])
            return (t.get("matching_target") or "Any").title()

        return {
            "qos": sorted([
                {"id": q.get("_id"), "name": q.get("name"), "enabled": q.get("enabled"),
                 "index": q.get("index"), "objective": q.get("objective"),
                 "downKbps": q.get("download_limit_kbps"), "upKbps": q.get("upload_limit_kbps"),
                 "downBurst": q.get("download_burst"), "upBurst": q.get("upload_burst"),
                 "source": qos_target(q.get("source")), "destination": qos_target(q.get("destination"))}
                for q in r.get("qos", [])
            ], key=lambda q: (not q["enabled"], q["index"] or 0)),
            "routes": [
                {"id": t.get("_id"), "description": t.get("description"), "enabled": t.get("enabled"),
                 "matchingTarget": t.get("matching_target"),
                 "domains": [d.get("domain") for d in (t.get("domains") or []) if d.get("domain")],
                 "ipAddresses": t.get("ip_addresses") or [], "ipRanges": t.get("ip_ranges") or [],
                 "regions": t.get("regions") or [],
                 "killSwitch": t.get("kill_switch_enabled"),
                 # network_id is the tunnel it routes THROUGH; target_devices
                 # are the networks/clients being steered into it.
                 "via": net_names.get(t.get("network_id")) or t.get("next_hop") or None,
                 "targets": [net_names.get(d.get("network_id")) or d.get("type")
                             for d in (t.get("target_devices") or [])]}
                for t in r.get("routes", [])
            ],
            "vpnServers": [
                {"id": v.get("_id"), "name": (v.get("name") or "").strip(), "enabled": v.get("enabled"),
                 "type": v.get("vpn_type"), "purpose": v.get("purpose"), "subnet": v.get("ip_subnet"),
                 "port": v.get("local_port")}
                for v in r.get("vpnServers", [])
            ],
            "vpnClients": [
                {"id": v.get("_id"), "name": (v.get("name") or "").strip(), "enabled": v.get("enabled"),
                 "type": v.get("vpn_type"), "purpose": v.get("purpose"), "subnet": v.get("ip_subnet"),
                 "configStatus": v.get("openvpn_configuration_status"),
                 "username": v.get("openvpn_username")}
                for v in r.get("vpnClients", [])
            ],
            "ddns": [
                {"host": d.get("host_name"), "service": d.get("service"),
                 "server": d.get("server"), "interface": d.get("interface")}
                for d in r.get("ddns", [])
            ],
        }

    return web.json_response(await _cached_flow("routing", produce))


async def handle_config(request):
    async def produce():
        c = await state.session.fetch_config()
        nets = c.get("networks", [])
        net_names = {n.get("_id"): (n.get("name") or "").strip() for n in nets if n.get("_id")}
        ug_names = {g.get("_id"): g.get("name") for g in c.get("userGroups", []) if g.get("_id")}
        return {
            "wlans": [
                {"id": w.get("_id"), "ssid": w.get("name"), "enabled": w.get("enabled"),
                 "security": w.get("security"), "wpaMode": w.get("wpa_mode"),
                 "band": w.get("wlan_band"), "hidden": w.get("hide_ssid"),
                 "isGuest": w.get("is_guest"),
                 "network": net_names.get(w.get("networkconf_id")),
                 "userGroup": ug_names.get(w.get("usergroup_id")),
                 "apGroups": len(w.get("ap_group_ids") or [])}
                for w in c.get("wlans", [])
            ],
            "networks": sorted([
                {"id": n.get("_id"), "name": (n.get("name") or "").strip(),
                 "purpose": n.get("purpose"), "enabled": n.get("enabled"),
                 "vlan": n.get("vlan"), "subnet": n.get("ip_subnet"),
                 "dhcpEnabled": n.get("dhcpd_enabled"),
                 "dhcpRange": (f"{n.get('dhcpd_start')}–{n.get('dhcpd_stop')}"
                               if n.get("dhcpd_start") else None),
                 "dns": [d for d in (n.get("dhcpd_dns_1"), n.get("dhcpd_dns_2")) if d]}
                for n in nets
            ], key=lambda n: (n["purpose"] or "", n["name"] or "")),
            "portProfiles": [
                {"id": p.get("_id"), "name": p.get("name"), "forward": p.get("forward"),
                 "nativeNetwork": net_names.get(p.get("native_networkconf_id")),
                 "poeMode": p.get("poe_mode"), "isolation": p.get("isolation"),
                 "opMode": p.get("op_mode")}
                for p in c.get("portProfiles", [])
            ],
            "userGroups": [
                {"id": g.get("_id"), "name": g.get("name"),
                 # -1 means unlimited in the controller's encoding
                 "downKbps": g.get("qos_rate_max_down"), "upKbps": g.get("qos_rate_max_up")}
                for g in c.get("userGroups", [])
            ],
        }

    return web.json_response(await _cached_flow("config", produce))


USAGE_SCOPES = {
    ("user", "24h"): ("hourly.user", 24), ("user", "7d"): ("hourly.user", 168),
    ("user", "30d"): ("daily.user", 720),
    ("ap", "24h"): ("hourly.ap", 24), ("ap", "7d"): ("hourly.ap", 168),
    ("ap", "30d"): ("daily.ap", 720),
}


async def handle_usage_top(request):
    """Top talkers by bytes over a window, from the controller's per-client /
    per-AP rollups. This is history the dashboard never recorded itself."""
    kind = request.query.get("scope", "user")
    range_key = request.query.get("range", "7d")
    scope = USAGE_SCOPES.get((kind, range_key))
    if not scope:
        return web.json_response({"error": "bad scope/range"}, status=400)
    report, hours = scope

    async def produce():
        rows = await state.session.fetch_usage_report(report, hours)
        key = "user" if kind == "user" else "ap"
        totals: dict[str, dict] = {}
        for r in rows:
            mac = r.get(key) or r.get("oid")
            if not mac:
                continue
            t = totals.setdefault(mac, {"mac": mac, "rx": 0.0, "tx": 0.0, "samples": 0})
            t["rx"] += r.get("rx_bytes") or 0
            t["tx"] += r.get("tx_bytes") or 0
            t["samples"] += 1
        conn = db.connect()
        names = _name_lookup(conn)
        conn.close()
        out = []
        for mac, t in totals.items():
            out.append({"mac": mac, "name": names.get(mac),
                        "rxBytes": int(t["rx"]), "txBytes": int(t["tx"]),
                        "totalBytes": int(t["rx"] + t["tx"]), "samples": t["samples"]})
        out.sort(key=lambda x: -x["totalBytes"])
        return {"scope": kind, "range": range_key, "report": report,
                "count": len(out), "rows": out[:40]}

    return web.json_response(await _cached_flow(f"usage:{kind}:{range_key}", produce))


async def handle_wifi_health(request):
    async def produce():
        agg = await state.session.fetch_aggregated_dashboard()
        return {
            "connectivity": (agg.get("wifi_connectivity") or {}).get("radio_connectivity") or [],
            "channels": (agg.get("wifi_channels") or {}).get("radio_channels") or [],
            "density": (agg.get("ap_radio_density") or {}).get("density_details") or [],
            "experience": (agg.get("wifi_client_experience") or {}).get("categories") or [],
            "doctor": agg.get("wifi_doctor") or {},
        }

    return web.json_response(await _cached_flow("wifi_health", produce))


async def handle_rtt_history(request):
    """Per-target latency series for one WAN path, one entry per
    (target, monitor_type) -- the same host can be probed over both ICMP
    and DNS, and those are separate measurements."""
    gateway = request.query.get("gateway", "primary")
    range_key = request.query.get("range", "24h")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT target, monitor_type, strftime('{bucket_fmt}', ts) AS bucket,
               AVG(latency_ms), AVG(availability)
        FROM rtt_monitors WHERE gateway_kind = ? AND ts >= ?
        GROUP BY target, monitor_type, bucket
        ORDER BY target, monitor_type, bucket
        """,
        (gateway, cutoff),
    ).fetchall()
    conn.close()

    grouped: dict[tuple, dict] = {}
    for target, mtype, bucket, latency, availability in rows:
        g = grouped.setdefault((target, mtype), {
            "target": target, "type": mtype, "points": [], "availability": None,
        })
        g["points"].append({"t": bucket, "latencyMs": latency})
        if availability is not None:
            g["availability"] = availability

    result = []
    for g in grouped.values():
        vals = [p["latencyMs"] for p in g["points"] if p["latencyMs"] is not None]
        g["min"] = min(vals) if vals else None
        g["max"] = max(vals) if vals else None
        g["avg"] = sum(vals) / len(vals) if vals else None
        g["latest"] = vals[-1] if vals else None
        result.append(g)
    # ICMP before DNS, then by target, so card order is stable across reloads.
    result.sort(key=lambda g: (g["type"] != "icmp", g["type"], g["target"]))
    return web.json_response(result)


def _usage_since(conn, table, key_cols, key_vals, cutoff, rx_col="rx_bytes_total", tx_col="tx_bytes_total"):
    """Bandwidth used since `cutoff`, computed from a monotonic cumulative
    byte counter (delta = latest - nearest-sample-at-or-before-cutoff) rather
    than integrating rate samples -- accurate regardless of sampling gaps.
    Falls back to the earliest available sample if history doesn't reach
    back to cutoff (a live server that's only been running a few hours can't
    answer "used in the last 30 days", so this reports a partial-period
    figure instead of nothing). Returns (None, None) if there's no data at
    all; a negative delta (counter reset by a device reboot mid-period) is
    clamped to 0 since the true pre-reset baseline is unrecoverable.
    """
    where = " AND ".join(f"{c} = ?" for c in key_cols)
    latest = conn.execute(
        f"SELECT {rx_col}, {tx_col} FROM {table} WHERE {where} ORDER BY ts DESC LIMIT 1", key_vals
    ).fetchone()
    if not latest or latest[0] is None:
        return None, None
    # Skip rows that predate this column existing (older DBs migrated it in
    # after already having rows) -- an old NULL-total row would otherwise
    # win the "earliest available" fallback and poison the whole delta.
    ref = conn.execute(
        f"SELECT {rx_col}, {tx_col} FROM {table} WHERE {where} AND ts <= ? AND {rx_col} IS NOT NULL "
        f"ORDER BY ts DESC LIMIT 1",
        (*key_vals, cutoff),
    ).fetchone()
    if not ref:
        ref = conn.execute(
            f"SELECT {rx_col}, {tx_col} FROM {table} WHERE {where} AND {rx_col} IS NOT NULL "
            f"ORDER BY ts ASC LIMIT 1",
            key_vals,
        ).fetchone()
    if not ref:
        return None, None
    return max(0, latest[0] - ref[0]), max(0, latest[1] - ref[1])


async def handle_port_usage(request):
    """24h (or ?range=) bandwidth used per port on a switch/gateway, via
    byte-counter delta -- separate from /api/history/port's rate series."""
    mac = request.match_info["mac"]
    range_key = request.query.get("range", "24h")
    cutoff, _ = _bucket_query(range_key)
    conn = db.connect()
    idxs = [r[0] for r in conn.execute(
        "SELECT DISTINCT port_idx FROM port_stats WHERE mac = ?", (mac,)
    ).fetchall()]
    result = []
    for idx in idxs:
        rx, tx = _usage_since(conn, "port_stats", ["mac", "port_idx"], (mac, idx), cutoff)
        result.append({"idx": idx, "rxBytes": rx, "txBytes": tx})
    conn.close()
    return web.json_response(result)


async def handle_wan_usage(request):
    """24h/7d/30d WAN bandwidth used, via gateway_stats' existing cumulative
    wan_rx_bytes_total/wan_tx_bytes_total counters -- no new storage needed."""
    gateway = request.query.get("gateway", "primary")
    conn = db.connect()
    out = {}
    for range_key in ("24h", "7d", "30d"):
        cutoff, _ = _bucket_query(range_key)
        rx, tx = _usage_since(
            conn, "gateway_stats", ["gateway_kind"], (gateway,), cutoff,
            rx_col="wan_rx_bytes_total", tx_col="wan_tx_bytes_total",
        )
        out[range_key] = {"rxBytes": rx, "txBytes": tx}
    conn.close()
    return web.json_response(out)


async def handle_neighbor_history(request):
    bssid = request.match_info["bssid"]
    range_key = request.query.get("range", "7d")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket, MAX(signal)
        FROM rogue_aps_history WHERE bssid = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (bssid, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([{"t": r[0], "signal": r[1]} for r in rows])


async def handle_speedtest_history(request):
    gateway = request.query.get("gateway", "primary")
    conn = db.connect()
    rows = conn.execute(
        "SELECT ts, download_mbps, upload_mbps, latency_ms FROM speedtests WHERE source = ? ORDER BY ts",
        (gateway,),
    ).fetchall()
    conn.close()
    return web.json_response([{"ts": r[0], "down": r[1], "up": r[2], "lat": r[3]} for r in rows])


async def handle_vlan_history(request):
    network = request.match_info["network"]
    range_key = request.query.get("range", "7d")
    cutoff, bucket_fmt = _bucket_query(range_key)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT strftime('{bucket_fmt}', ts) AS bucket, AVG(online_count)
        FROM vlan_client_history WHERE network = ? AND ts >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        (network, cutoff),
    ).fetchall()
    conn.close()
    return web.json_response([{"t": r[0], "count": r[1]} for r in rows])


async def handle_vlan_clients(request):
    network = request.match_info["network"]
    status = request.query.get("status", "online")
    conn = db.connect()
    if status == "offline":
        rows = conn.execute(
            "SELECT mac, hostname, last_ip, connection_type, last_seen, vendor, parent_mac, parent_name "
            "FROM clients WHERE network = ? AND is_online = 0 ORDER BY last_seen DESC",
            (network,),
        ).fetchall()
        conn.close()
        return web.json_response([
            {"mac": r[0], "hostname": r[1], "ip": r[2], "connType": r[3], "lastSeen": r[4], "vendor": r[5],
             "parentMac": r[6], "parentName": r[7]}
            for r in rows
        ])
    rows = conn.execute(
        "SELECT mac, hostname, last_ip, connection_type, essid, signal_dbm, vendor, parent_mac, parent_name, assoc_time "
        "FROM clients WHERE network = ? AND is_online = 1 ORDER BY hostname",
        (network,),
    ).fetchall()
    conn.close()
    return web.json_response([
        {"mac": r[0], "hostname": r[1], "ip": r[2], "connType": r[3], "essid": r[4], "signal": r[5], "vendor": r[6],
         "parentMac": r[7], "parentName": r[8], "uptimeSec": _uptime_from_assoc(r[9])}
        for r in rows
    ])


async def handle_clients_online(request):
    conn = db.connect()
    rows = conn.execute(
        "SELECT mac, hostname, last_ip, network, connection_type, essid, signal_dbm, vendor, parent_mac, parent_name, "
        "assoc_time FROM clients WHERE is_online = 1 ORDER BY network, hostname"
    ).fetchall()
    conn.close()
    return web.json_response([
        {"mac": r[0], "hostname": r[1], "ip": r[2], "network": r[3], "connType": r[4], "essid": r[5],
         "signal": r[6], "vendor": r[7], "parentMac": r[8], "parentName": r[9],
         "uptimeSec": _uptime_from_assoc(r[10])}
        for r in rows
    ])


async def handle_clients_offline(request):
    conn = db.connect()
    rows = conn.execute(
        "SELECT mac, hostname, last_ip, network, connection_type, last_seen, vendor, parent_mac, parent_name "
        "FROM clients WHERE is_online = 0 ORDER BY last_seen DESC"
    ).fetchall()
    conn.close()
    return web.json_response([
        {"mac": r[0], "hostname": r[1], "ip": r[2], "network": r[3], "connType": r[4], "lastSeen": r[5],
         "vendor": r[6], "parentMac": r[7], "parentName": r[8]}
        for r in rows
    ])


def build_neighbors_payload(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT bssid, ap_mac, ssid, channel, band, signal, vendor, last_seen "
        "FROM rogue_aps ORDER BY bssid, signal DESC"
    ).fetchall()
    ap_names = dict(conn.execute("SELECT mac, name FROM devices WHERE category = 'ap'").fetchall())
    grouped: dict[str, dict] = {}
    for bssid, ap_mac, ssid, channel, band, signal, vendor, last_seen in rows:
        g = grouped.setdefault(bssid, {
            "bssid": bssid, "ssid": ssid, "channel": channel, "band": band, "vendor": vendor,
            "sightings": [],
        })
        g["sightings"].append({
            "apMac": ap_mac, "apName": ap_names.get(ap_mac) or ap_mac, "signal": signal, "lastSeen": last_seen,
        })
    result = []
    for g in grouped.values():
        g["sightings"].sort(key=lambda s: -(s["signal"] or -999))
        g["bestSignal"] = g["sightings"][0]["signal"]
        g["closestAp"] = g["sightings"][0]["apName"] or g["sightings"][0]["apMac"]
        result.append(g)
    result.sort(key=lambda g: -(g["bestSignal"] or -999))
    return result


async def handle_neighbors(request):
    conn = db.connect()
    result = build_neighbors_payload(conn)
    conn.close()
    return web.json_response(result)


async def handle_devices(request):
    conn = db.connect()
    rows = conn.execute(
        "SELECT mac, name, model, category, status, uptime_sec, ip, parent_name FROM devices"
    ).fetchall()
    conn.close()
    return web.json_response([
        {"mac": r[0], "name": r[1], "model": r[2], "category": r[3], "status": r[4], "uptimeSec": r[5],
         "ip": r[6], "parent": r[7]}
        for r in rows
    ])


async def handle_networks(request):
    """Network names known to the controller, so the UI can colour and filter
    by the real VLAN set instead of a hardcoded copy."""
    return web.json_response(persist.known_networks())


async def handle_vlans(request):
    conn = db.connect()
    rows = conn.execute(
        "SELECT COALESCE(network,'other'), COUNT(*), SUM(is_online) FROM clients GROUP BY COALESCE(network,'other')"
    ).fetchall()
    conn.close()
    return web.json_response([{"network": r[0], "total": r[1], "online": r[2] or 0} for r in rows])


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
async def backfill_gateway_history(conn) -> int:
    """Seed gateway_stats from the controller's own rollups.

    Without this, every chart starts empty after a restart and the 7d/14d/30d
    ranges stay useless until the process has been up that long -- even though
    the controller already holds the history.

    Resolution degrades with age (the controller keeps 5-minute data for 24h,
    hourly for 7d, daily for 30d), so each row is taken from the finest scope
    that still covers it and the regions are kept disjoint to avoid mixing
    granularities over the same span.

    Only the rate columns are written. The report returns per-interval byte
    deltas, not the monotonic counters wan_rx_bytes_total holds, and feeding
    deltas into those would corrupt the usage math in _usage_since().
    """
    gw = next((d for d in (state.last_fast or {}).get("devices", [])
               if persist.device_category(d) == "gateway"), None)
    if not gw or not gw.get("mac"):
        return 0
    mac, name = gw["mac"], gw.get("name")

    now_ms = int(time.time() * 1000)
    hour_ms, day_ms = 3600 * 1000, 86400 * 1000
    # (scope, seconds_per_bucket, region_start_ms, region_end_ms)
    scopes = [
        ("5minutes.gw", 300, now_ms - 24 * hour_ms, now_ms),
        ("hourly.gw", 3600, now_ms - 7 * day_ms, now_ms - 24 * hour_ms),
        ("daily.gw", 86400, now_ms - 30 * day_ms, now_ms - 7 * day_ms),
    ]
    inserted = 0
    for scope, bucket_secs, start_ms, end_ms in scopes:
        rows = await state.session.fetch_site_report(scope, start_ms, end_ms)
        for r in rows:
            t = r.get("time")
            if not t or not (start_ms <= t < end_ms):
                continue
            rx, tx = r.get("wan-rx_bytes"), r.get("wan-tx_bytes")
            ts = persist.epoch_to_iso(t / 1000)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO gateway_stats
                    (ts, gateway_mac, gateway_kind, gateway_name, wan_rx_rate_bps, wan_tx_rate_bps)
                VALUES (?, ?, 'primary', ?, ?, ?)
                """,
                (ts, mac, name,
                 int(rx * 8 / bucket_secs) if rx else None,
                 int(tx * 8 / bucket_secs) if tx else None),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


async def on_startup(app):
    state.session = UnifiSession()
    await state.session.ensure_connected()
    conn = db.connect()
    db.init_db(conn)
    n_nets = persist.set_networks(await state.session.fetch_networks())
    log.info("networks: %d subnets mapped", n_nets)
    try:
        state.last_fast = await state.session.fetch_fast()
        n_rows = await backfill_gateway_history(conn)
        log.info("backfill: %d historical gateway rows imported", n_rows)
    except Exception:
        log.exception("gateway history backfill failed (continuing without it)")
    conn.close()
    app["fast_task"] = asyncio.create_task(fast_loop())
    app["persist_task"] = asyncio.create_task(persist_loop())
    app["slow_task"] = asyncio.create_task(slow_loop())
    log.info("Live server ready on http://%s:%s", HOST, PORT)


async def on_cleanup(app):
    tasks = [app[key] for key in ("fast_task", "persist_task", "slow_task")]
    for t in tasks:
        t.cancel()
    # Wait for the loops to actually unwind before closing the shared
    # session -- cancelling and closing concurrently can yank a connection
    # out from under an in-flight request and surface as a spurious
    # "invalid state" error during shutdown.
    await asyncio.gather(*tasks, return_exceptions=True)
    if state.session:
        await state.session.close()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/devices", handle_devices)
    app.router.add_get("/api/vlans", handle_vlans)
    app.router.add_get("/api/networks", handle_networks)
    app.router.add_get("/api/history/wan", handle_wan_history)
    app.router.add_get("/api/history/speedtest", handle_speedtest_history)
    app.router.add_get("/api/history/vlan/{network}", handle_vlan_history)
    app.router.add_get("/api/history/ap/{mac}/{band}", handle_ap_history)
    app.router.add_get("/api/history/device/{mac}", handle_device_history)
    app.router.add_get("/api/history/port/{mac}/{idx}", handle_port_history)
    app.router.add_get("/api/usage/ports/{mac}", handle_port_usage)
    app.router.add_get("/api/usage/wan", handle_wan_usage)
    app.router.add_get("/api/history/rtt", handle_rtt_history)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/firewall", handle_firewall)
    app.router.add_get("/api/routing", handle_routing)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/usage/top", handle_usage_top)
    app.router.add_get("/api/wifi/health", handle_wifi_health)
    app.router.add_get("/api/flows/stats", handle_flow_stats)
    app.router.add_get("/api/flows/recent", handle_flows_recent)
    app.router.add_get("/api/history/neighbor/{bssid}", handle_neighbor_history)
    app.router.add_get("/api/clients/online", handle_clients_online)
    app.router.add_get("/api/clients/offline", handle_clients_offline)
    app.router.add_get("/api/clients/{network}", handle_vlan_clients)
    app.router.add_get("/api/neighbors", handle_neighbors)
    app.router.add_static("/static/", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host=HOST, port=PORT)
