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
