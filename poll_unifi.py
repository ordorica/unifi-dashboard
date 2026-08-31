#!/usr/bin/env python3
"""
One-shot poll of the UniFi controller into the local SQLite database.
Kept for manual runs/backfills; the live server (live_server.py) now owns
the recurring polling via its own internal loops instead of cron.

Run standalone via `uv run --with unifi-core --with aiounifi poll_unifi.py`.
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unifi_lib import db, persist
from unifi_lib.fetch import UnifiSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("poll_unifi")


async def poll() -> tuple[int, int]:
    session = UnifiSession()
    try:
        fast = await session.fetch_fast()
        historical = await session.fetch_historical_clients()
        speedtests = await session.fetch_speedtests(duration_hours=24)
        rogue = await session.fetch_rogue_aps(within_hours=2)
    finally:
        await session.close()

    ts = persist.now_iso()
    conn = db.connect()
    db.init_db(conn)

    online_count, offline_count = persist.persist_clients(conn, fast["online"], historical, ts)
    persist.persist_vlan_history(conn, ts)
    device_rows = persist.persist_devices_and_gateways(conn, fast["devices"], ts)
    st_count = persist.persist_speedtests(conn, speedtests)
    rogue_count = persist.persist_rogue_aps(conn, rogue, ts)
    # The guard is the number of `devices` rows the upsert actually
    # refreshed, not the number of entries fetched -- N entries that all
    # upsert to zero rows (an API change reshaping the payload) must read as
    # zero here, or the sweep would delete every device a week later.
    removed = db.sweep_absent_devices(conn, device_rows)
    db.prune_old(conn)

    conn.commit()
    conn.close()
    log.info(
        "Poll complete: %d online, %d offline, %d new speedtests, %d neighbor readings",
        online_count, offline_count, st_count, rogue_count,
    )
    if removed:
        log.info("Removed %d device(s) absent for %d+ days: %s",
                 len(removed), db.DEVICE_ABSENCE_DAYS, ", ".join(removed))
    return online_count, offline_count


def main() -> None:
    try:
        asyncio.run(poll())
    except Exception:
        log.exception("Poll failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
