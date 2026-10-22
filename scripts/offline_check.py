"""Offline verification: runs every major feature with outbound network blocked in-process.

Usage:
  python scripts/offline_check.py            # in-process network block (non-loopback sockets refused)
For a full test, ALSO disable Wi-Fi/Ethernet at the OS level and run it again, then launch ./run.sh.
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_real = socket.socket.connect


def _guard(self, addr):
    if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "localhost", "::1"):
        raise OSError(f"BLOCKED outbound connection to {addr}")
    return _real(self, addr)


socket.socket.connect = _guard

from vehiclegpt import analytics, anomaly, db, detective, runtime, simulator  # noqa: E402
from vehiclegpt import query_engine as qe  # noqa: E402

checks = []


def check(name, fn):
    t = time.perf_counter()
    try:
        detail = fn()
        checks.append((name, True, (time.perf_counter() - t) * 1000, detail))
    except Exception as e:  # noqa: BLE001
        checks.append((name, False, (time.perf_counter() - t) * 1000, repr(e)))


try:
    socket.create_connection(("1.1.1.1", 53), timeout=1)
    print("WARNING: network block not effective")
except OSError as e:
    print(f"Network block active: {e}")

runtime.ensure_database()
conn = db.connect()
for q in ["Why did my driving range decrease over the last seven days?",
          "What happened before the battery temperature warning?",
          "Show unusual events from yesterday.",
          "Compare this week's energy consumption with last week.",
          "Which diagnostic warnings occurred repeatedly?"]:
    check(f"query: {q}", lambda q=q: (lambda a: a.supported and a.driver[:70] or (_ for _ in ()).throw(AssertionError(a.limitations)))(qe.answer(conn, q)))
check("early warnings", lambda: f"{len(anomaly.run_detection(conn, persist=False))} alerts")
check("event detective", lambda: f"{len(detective.investigate(conn, detective.list_events(conn)['key'].iloc[0])['timeline'])} timeline items")
check("what-if simulation", lambda: f"range {simulator.simulate(simulator.baseline(conn), simulator.Params(consumption_change_pct=10))['range']}")
check("dataset stats", lambda: analytics.period_stats(conn, analytics.last_n_days(conn, 7)).distance_km)

ok = all(c[1] for c in checks)
for name, passed, ms, d in checks:
    print(f"[{'PASS' if passed else 'FAIL'}] {ms:7.1f} ms  {name}  -> {d}")
print("\nOFFLINE VERIFICATION:", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
