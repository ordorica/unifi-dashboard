"""Thin async wrapper around the UniFi controller connection, reusable by
both the one-shot cron poller and the live server's persistent session."""
import os
import json
import time
from pathlib import Path

from unifi_core.network.managers.client_manager import ClientManager
from unifi_core.network.managers.connection_manager import ConnectionManager
from unifi_core.network.managers.device_manager import DeviceManager
from unifi_core.network.managers.event_manager import EventManager
from unifi_core.network.managers.firewall_manager import FirewallManager
from unifi_core.network.managers.network_manager import NetworkManager
from unifi_core.network.managers.system_manager import SystemManager
from unifi_core.network.managers.stats_manager import StatsManager
from unifi_core.network.managers.traffic_flow_manager import TrafficFlowManager
from unifi_core.network.models.traffic_flows import TrafficFlowQuery
from aiounifi.models.api import ApiRequest, ApiRequestV2

SETTINGS_FILE = Path(__file__).resolve().parent.parent.parent / ".claude" / "settings.local.json"


def _settings_env() -> dict:
    """The `env` block of settings.local.json, or {} when it is absent.

    That file lives outside this directory and only exists on the development
    Mac. In a container every value arrives through the real environment, so a
    missing file is normal rather than an error.
    """
    try:
        return json.loads(SETTINGS_FILE.read_text()).get("env", {})
    except (OSError, ValueError):
        return {}


def load_config() -> dict:
    settings = _settings_env()

    def pick(*names, default=None):
        """Real environment first, then settings.local.json, then default."""
        for source in (os.environ, settings):
            for name in names:
                if source.get(name):
                    return source[name]
        return default

    return {
        "host": pick("UNIFI_NETWORK_HOST", "UNIFI_HOST"),
        "username": pick("UNIFI_NETWORK_USERNAME", "UNIFI_USERNAME"),
        "password": pick("UNIFI_NETWORK_PASSWORD", "UNIFI_PASSWORD"),
        "port": int(pick("UNIFI_NETWORK_PORT", "UNIFI_PORT", default=443)),
        "site": pick("UNIFI_NETWORK_SITE", "UNIFI_SITE", default="default"),
        "verify_ssl": str(pick("UNIFI_NETWORK_VERIFY_SSL", "UNIFI_VERIFY_SSL",
                               default="false")).lower() == "true",
    }


def raw_of(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    raw = getattr(obj, "raw", None)
    return raw if isinstance(raw, dict) else {}


class UnifiSession:
    """A persistent, reconnect-on-demand controller session."""

    def __init__(self):
        cfg = load_config()
        if not cfg["host"] or not cfg["username"] or not cfg["password"]:
            raise RuntimeError(
                "Missing UniFi credentials: set UNIFI_NETWORK_HOST, "
                "UNIFI_NETWORK_USERNAME and UNIFI_NETWORK_PASSWORD in the "
                f"environment, or provide them in {SETTINGS_FILE}"
            )
        self.conn = ConnectionManager(
            host=cfg["host"], username=cfg["username"], password=cfg["password"],
            port=cfg["port"], site=cfg["site"], verify_ssl=cfg["verify_ssl"],
        )
        self.clients = ClientManager(self.conn)
        self.devices = DeviceManager(self.conn)
        self.stats = StatsManager(self.conn, self.clients)
        self.flows = TrafficFlowManager(self.conn)
        self.networks = NetworkManager(self.conn)
        self.events = EventManager(self.conn)
        self.system = SystemManager(self.conn)
        self.firewall = FirewallManager(self.conn)

    async def ensure_connected(self):
        if not await self.conn.ensure_connected():
            raise RuntimeError("Failed to connect to UniFi controller")

    async def close(self):
        await self.conn.cleanup()

    # ---- Fast tier: cheap, safe to call every few seconds ----
    async def fetch_fast(self) -> dict:
        await self.ensure_connected()
        online = [raw_of(c) for c in await self.clients.get_clients()]
        devices = [raw_of(d) for d in await self.devices.get_devices()]
        return {"online": online, "devices": devices}

    # ---- Slow tier: heavier calls, poll infrequently ----
    async def fetch_historical_clients(self) -> list[dict]:
        await self.ensure_connected()
        return [raw_of(c) for c in await self.clients.get_all_clients()]

    async def fetch_speedtests(self, duration_hours: int = 24) -> list[dict]:
        await self.ensure_connected()
        try:
            return await self.stats.get_speedtest_results(duration_hours=duration_hours)
        except Exception:
            return []

    async def fetch_rogue_aps(self, within_hours: int = 2) -> list[dict]:
        await self.ensure_connected()
        try:
            return await self.devices.list_rogue_aps(within_hours=within_hours)
        except Exception:
            return []

    async def fetch_networks(self) -> list[dict]:
        """Configured networks/VLANs, used to map client IPs to network names."""
        await self.ensure_connected()
        try:
            return [raw_of(n) for n in await self.networks.get_networks()]
        except Exception:
            return []

    async def fetch_site_report(self, scope: str, start_ms: int, end_ms: int) -> list[dict]:
        """Controller-side historical rollup (stat/report). The controller keeps
        5-minute data for 24h, hourly for 7d and daily for 30d -- far more than
        this dashboard accumulates on its own after a restart."""
        await self.ensure_connected()
        body = {"start": start_ms, "end": end_ms,
                "attrs": ["time", "wan-rx_bytes", "wan-tx_bytes", "num_sta"]}
        try:
            result = await self.conn.request(
                ApiRequest(method="post", path=f"/stat/report/{scope}", data=body)
            )
            return result if isinstance(result, list) else []
        except Exception:
            return []

    # ---- Health tier: slow-changing, poll about once a minute ----
    async def fetch_events(self) -> list[dict]:
        await self.ensure_connected()
        try:
            return [raw_of(e) for e in await self.events.get_events()]
        except Exception:
            return []

    async def fetch_alarms(self) -> list[dict]:
        """Usually empty; populated when the controller raises an alarm."""
        await self.ensure_connected()
        try:
            return [raw_of(a) for a in await self.events.get_alarms()]
        except Exception:
            return []

    async def fetch_anomalies(self) -> list[dict]:
        await self.ensure_connected()
        try:
            return await self.stats.get_anomalies() or []
        except Exception:
            return []

    async def fetch_network_health(self) -> list[dict]:
        await self.ensure_connected()
        try:
            return await self.system.get_network_health() or []
        except Exception:
            return []

    async def fetch_system_info(self) -> dict:
        await self.ensure_connected()
        try:
            return await self.system.get_system_info() or {}
        except Exception:
            return {}

    async def fetch_aggregated_dashboard(self) -> dict:
        """The v2 dashboard rollup: WiFi experience/connectivity/channels,
        AP radio density, IPS counters and the internet health timeline."""
        await self.ensure_connected()
        try:
            result = await self.conn.request(
                ApiRequestV2(method="get", path="/aggregated-dashboard")
            )
            if isinstance(result, list):
                return result[0] if result else {}
            return result or {}
        except Exception:
            return {}

    async def _gather(self, spec: dict) -> dict:
        """Run several read-only manager calls, tolerating individual failures
        so one unsupported endpoint can't blank an entire tab."""
        out = {}
        for key, call in spec.items():
            try:
                result = await call()
                out[key] = [raw_of(x) for x in result] if isinstance(result, list) else (result or {})
            except Exception:
                out[key] = []
        return out

    async def fetch_routing(self) -> dict:
        from unifi_core.network.managers.qos_manager import QosManager
        from unifi_core.network.managers.traffic_route_manager import TrafficRouteManager
        from unifi_core.network.managers.vpn_manager import VpnManager
        from unifi_core.network.managers.dynamic_dns_manager import DynamicDnsManager
        return await self._gather({
            "qos": QosManager(self.conn).get_qos_rules,
            "routes": TrafficRouteManager(self.conn).get_traffic_routes,
            "vpnServers": VpnManager(self.conn).get_vpn_servers,
            "vpnClients": VpnManager(self.conn).get_vpn_clients,
            "ddns": DynamicDnsManager(self.conn).list_dynamic_dns,
        })

    async def fetch_config(self) -> dict:
        from unifi_core.network.managers.switch_manager import SwitchManager
        from unifi_core.network.managers.usergroup_manager import UsergroupManager
        return await self._gather({
            "wlans": self.networks.get_wlans,
            "networks": self.networks.get_networks,
            "portProfiles": SwitchManager(self.conn).get_port_profiles,
            "userGroups": UsergroupManager(self.conn).get_usergroups,
        })

    async def fetch_usage_report(self, scope: str, hours: int) -> list[dict]:
        """Per-client (.user) or per-AP (.ap) byte totals from the controller's
        own rollups -- history we never collected ourselves."""
        await self.ensure_connected()
        now_ms = int(time.time() * 1000)
        body = {"start": now_ms - hours * 3600 * 1000, "end": now_ms,
                "attrs": ["time", "rx_bytes", "tx_bytes", "bytes"]}
        try:
            result = await self.conn.request(
                ApiRequest(method="post", path=f"/stat/report/{scope}", data=body)
            )
            return result if isinstance(result, list) else []
        except Exception:
            return []

    async def fetch_firewall(self) -> dict:
        """Firewall config: policies (with hit counters), port forwards, zones
        and address groups. Zones/groups are needed to turn the id references
        inside each policy into something readable."""
        await self.ensure_connected()
        out = {"policies": [], "portForwards": [], "zones": [], "groups": []}
        for key, call in (
            ("policies", self.firewall.get_firewall_policies),
            ("portForwards", self.firewall.get_port_forwards),
            ("zones", self.firewall.get_firewall_zones),
            ("groups", self.firewall.get_firewall_groups),
        ):
            try:
                out[key] = [raw_of(x) for x in (await call() or [])]
            except Exception:
                pass
        return out

    # ---- Traffic flows (heavy: thousands of rows; never poll these fast) ----
    async def fetch_flow_stats(self, period: str = "DAY", top: int = 10) -> dict:
        """Controller-side rollup: counts by risk/region plus top-talker
        rankings. Authoritative for totals -- the flow list caps out."""
        await self.ensure_connected()
        try:
            return await self.flows.get_traffic_flow_statistics(period=period, top=top) or {}
        except Exception:
            return {}

    async def fetch_flows(self, hours: int = 1, page_size: int = 1000) -> list[dict]:
        await self.ensure_connected()
        now_ms = int(time.time() * 1000)
        query = TrafficFlowQuery(
            time_from=now_ms - hours * 3600 * 1000, time_to=now_ms,
            page_size=min(page_size, 1000), page_number=0,
        )
        try:
            result = await self.flows.get_traffic_flows(query)
            return (result or {}).get("flows") or []
        except Exception:
            return []
