"""UDS / DoIP diagnostic report adapter.

Parses ReadDTCInformation (service 0x19, sub-function 0x02 reportDTCByStatusMask) responses captured as hex lines:
  2026-09-21T10:20:00 7E8 59 02 FF 0A 7F 02 2F
DTC bytes are a 3-byte DTC + 1 status byte. Codes are mapped to the project's synthetic SIM-* catalogue through
DTC_CATALOGUE; unknown DTCs are kept with their raw hex (never invented descriptions).
"""
from __future__ import annotations

from datetime import datetime

from . import Event

DTC_CATALOGUE = {
    0x0A7F02: ("SIM-BMS-T01", "battery", "Battery pack temperature high (synthetic)"),
    0x0C2A00: ("SIM-THM-P02", "thermal", "Motor coolant flow low (synthetic)"),
    0x0A2F05: ("SIM-THM-T05", "thermal", "Motor temperature high (synthetic)"),
    0xC75001: ("SIM-TPMS-L01", "tyres", "Low tyre pressure detected (synthetic)"),
}
CODE_TO_DTC = {v[0]: k for k, v in DTC_CATALOGUE.items()}


def parse(lines, vehicle_id: str) -> tuple[list[Event], int]:
    events, errors = [], 0
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        try:
            ts = datetime.fromisoformat(parts[0]).isoformat(timespec="seconds")
            ecu = parts[1]
            data = [int(x, 16) for x in parts[2:]]
        except (ValueError, IndexError):
            errors += 1
            continue
        if data[:2] != [0x59, 0x02]:
            errors += 1
            continue
        recs = data[3:]
        for i in range(0, len(recs) - 3, 4):
            dtc = (recs[i] << 16) | (recs[i + 1] << 8) | recs[i + 2]
            status = recs[i + 3]
            if not status & 0x01:  # testFailed bit not set -> not active
                continue
            code, sub, desc = DTC_CATALOGUE.get(dtc, (f"RAW-{dtc:06X}", "unknown", f"Unmapped DTC 0x{dtc:06X} (no description)"))
            events.append(Event(ts, vehicle_id, "dtc", {"code": code, "subsystem": sub, "description": desc,
                                                       "reported_level": "warning" if status & 0x08 else "info"},
                                "diag", f"UDS 0x19/0x02 ECU {ecu} status 0x{status:02X}"))
    return events, errors


def encode(ts_iso: str, code: str, ecu: str = "7E8", status: int = 0x2F) -> str:
    d = CODE_TO_DTC[code]
    return f"{ts_iso} {ecu} 59 02 FF {d >> 16:02X} {(d >> 8) & 0xFF:02X} {d & 0xFF:02X} {status:02X}"
