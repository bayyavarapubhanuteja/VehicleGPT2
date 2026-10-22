"""QNX slog2 adapter: parses `slog2info` text output into diagnostic events.

Line format: "<Mon DD HH:MM:SS.mmm> <buffer>.<pid> <severity> <code> <message>", e.g.
  Sep 21 10:20:00.123 thermal_mgr.4021 2 0 SIM-THM-P02 coolant flow low (synthetic)
Severity follows slog2 levels (0 SHUTDOWN … 2 CRITICAL, 3 ERROR, 4 WARNING, 5 NOTICE, 6 INFO, 7 DEBUG1).
Only lines carrying a diagnostic code (SIM-XXX-YYY) become events; everything else is counted and skipped.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import Event

LINE = re.compile(r"^(\w{3} \d{1,2} \d{2}:\d{2}:\d{2}\.\d{3})\s+([\w.-]+)\.(\d+)\s+(\d)\s+\d+\s+(SIM-[A-Z]{3}-[A-Z0-9]{3})\s+(.*)$")
SUBSYSTEM = {"BMS": "battery", "THM": "thermal", "TPM": "tyres", "INF": "infotainment", "SNS": "thermal", "ADS": "adas"}


def parse(lines, vehicle_id: str, year: int) -> tuple[list[Event], int, int]:
    events, skipped, errors = [], 0, 0
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        m = LINE.match(line)
        if not m:
            skipped += 1
            continue
        stamp, buf, pid, sev, code, msg = m.groups()
        try:
            ts = datetime.strptime(f"{year} {stamp}", "%Y %b %d %H:%M:%S.%f")
        except ValueError:
            errors += 1
            continue
        level = "warning" if int(sev) <= 4 else "info"
        events.append(Event(ts.isoformat(timespec="seconds"), vehicle_id, "dtc",
                            {"code": code, "subsystem": SUBSYSTEM.get(code.split("-")[1][:3], "other"),
                             "description": msg.strip()[:200], "reported_level": level},
                            "qnx_slog", f"QNX slog2 {buf}.{pid}"))
    return events, skipped, errors
