"""Assign each observed WAN Path a stable synthetic identity.

The controller's WAN key is a slot, not a service: re-cabling a link would
split its history, and swapping ISPs on a slot would silently blend two
different services into one chart. Matching on the provider instead means
history follows the internet service.

The rule fails toward splitting. A wrong split shows two series where one
was expected -- visible, and correctable via label_override. A wrong merge
blends two services irreversibly, because the rows no longer record which
was which. See docs/adr/0001-wan-path-identity.md.

Resolution is batch, not per-path, and that is load-bearing: two WAN Paths
that share an ASN can only be told apart by knowing which candidate rows
this same cycle has already claimed. A per-path signature cannot know that
-- resolving the second path would see the first's brand-new row as a lone,
uncorroborated ASN match and merge into it, and every later cycle would
repeat the mistake, permanently blending the two services' history. Within
one call, an id claimed by an earlier path is removed from the candidate
pool for every later one.
"""
import sqlite3

from .wan import WanPath


def resolve_path_ids(db: sqlite3.Connection, gateway_mac: str, paths: list[WanPath], ts: str) -> list[int]:
    """Resolve every WAN Path on one gateway in a single pass.

    Batch rather than per-path because two paths sharing an ASN can only be
    told apart by knowing which candidate rows this cycle already claimed.
    Returns ids positionally aligned with `paths`.
    """
    claimed: set[int] = set()
    ids: list[int] = []
    for path in paths:
        match_id = _find_match(db, gateway_mac, path, claimed)
        if match_id is None:
            cur = db.execute(
                "INSERT INTO wan_paths (gateway_mac, wan_key, ifname, link_type, isp_name, asn, "
                "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (gateway_mac, path.key, path.ifname, path.link_type, path.isp_name, path.asn, ts, ts),
            )
            match_id = cur.lastrowid
        else:
            db.execute(
                "UPDATE wan_paths SET wan_key = ?, ifname = ?, link_type = ?, isp_name = ?, "
                "asn = ?, last_seen = ? WHERE id = ?",
                (path.key, path.ifname, path.link_type, path.isp_name, path.asn, ts, match_id),
            )
        claimed.add(match_id)              # unavailable to every later path this cycle
        ids.append(match_id)
    return ids


def _find_match(db: sqlite3.Connection, gateway_mac: str, path: WanPath, claimed: set[int]) -> int | None:
    if path.asn is not None:
        rows = [
            row for row in db.execute(
                "SELECT id, wan_key, ifname FROM wan_paths WHERE gateway_mac = ? AND asn = ?",
                (gateway_mac, path.asn),
            ).fetchall()
            if row[0] not in claimed        # already matched to an earlier path this cycle
        ]
        if len(rows) == 1:
            row = rows[0]
            if row[1] == path.key or row[2] == path.ifname:
                return row[0]           # survives re-cabling
            # Neither key nor ifname carries over. Two paths sharing an ASN
            # and appearing together are already handled above -- the
            # earlier one's row is excluded from `rows` by `claimed`, so
            # this branch is never reached for that case. What remains here
            # is a gateway with *other, unrelated* history: a bare ASN
            # match with no corroboration is equally consistent with "the
            # same path, re-cabled" and "a second, brand-new path that
            # happens to share an ISP", and guessing merge is exactly the
            # irreversible mistake this design exists to avoid. On a
            # gateway that has never had any other path at all, though, an
            # ASN match is unambiguous -- there is nothing else it could
            # be -- so re-cabling still works there.
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
    if row is None or row[0] in claimed:
        return None
    return row[0]
