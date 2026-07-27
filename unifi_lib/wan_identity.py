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
            row = rows[0]
            if row[1] == path.key or row[2] == path.ifname:
                return row[0]           # survives re-cabling
            # Neither key nor ifname carries over. On a gateway that has
            # never had any other path, a bare ASN match is unambiguous --
            # there is nothing else it could be, so this is still a
            # re-cabling. On a gateway with other known paths, a bare ASN
            # match with no corroboration is equally consistent with "the
            # same path, re-cabled" and "a second, brand-new path that
            # happens to share an ISP" -- and resolving that guess toward
            # a merge is exactly the irreversible mistake this design
            # exists to avoid (see the ADR's "same ISP on two slots" case:
            # without this check, two distinct paths with the same ASN
            # never reach the disambiguation branch below, because the
            # first one resolved each tick always looks like the sole
            # candidate and silently absorbs the other).
            (total,) = db.execute(
                "SELECT COUNT(*) FROM wan_paths WHERE gateway_mac = ?", (gateway_mac,)
            ).fetchone()
            return row[0] if total <= 1 else None
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
