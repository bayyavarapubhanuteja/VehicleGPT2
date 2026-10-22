"""Runtime helpers: DB bootstrap/upgrade, network status, process diagnostics."""
from __future__ import annotations

import os
import socket
import time

from . import anomaly, auth, config, data_gen, db, migrations


def ensure_database(path=None) -> dict:
    """Create the database if missing; otherwise migrate it in place (never deletes data)."""
    info: dict = {"created": False, "migrated_to": None, "vehicles_added": [], "demo_users": False}
    if not db.is_initialized(path):
        info["generated"] = data_gen.generate(path)
        info["created"] = True
    with db.session(path) as c:
        info["migrated_to"] = migrations.migrate(c)
        info["vehicles_added"] = data_gen.add_missing_vehicles(c)
        missing = [v for v in db.vehicle_ids(c)
                   if not c.execute("SELECT 1 FROM alerts WHERE vehicle_id = ? AND rule_version IS NOT NULL LIMIT 1", (v,)).fetchone()]
        for v in missing:
            anomaly.run_detection(c, vehicle_id=v)
        info["demo_users"] = auth.seed_demo_users(c)
    return info


def network_available(timeout: float = 0.3) -> bool | None:
    """TCP reachability probe only (no data sent). Disable with VEHICLEGPT_NET_PROBE=0."""
    if os.environ.get("VEHICLEGPT_NET_PROBE", "1") == "0":
        return None
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=timeout):
            return True
    except OSError:
        return False


def process_rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        return None


def timed(fn, *a, **k):
    t = time.perf_counter()
    r = fn(*a, **k)
    return r, (time.perf_counter() - t) * 1000
