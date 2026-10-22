"""Android Automotive Vehicle HAL adapter.

Parses property updates in the JSON-lines form produced by a VHAL client / `dumpsys android.hardware.automotive.vehicle`
export: {"prop": <int id>, "areaId": 0, "timestamp": <ns>, "value": <float|int>}. Property ids are the standard AOSP
VehicleProperty constants. On a real head unit the same mapping is used with a CarPropertyManager subscriber.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import Signal, from_epoch

# AOSP VehicleProperty ids (hardware/interfaces/automotive/vehicle)
VHAL_PROPS = {
    0x11600207: ("speed_kmh", "km/h", 3.6),            # PERF_VEHICLE_SPEED (m/s) -> km/h
    0x11600204: ("odometer_km", "km", 1.0),            # PERF_ODOMETER
    0x11600703: ("ambient_temp_c", "°C", 1.0),         # ENV_OUTSIDE_TEMPERATURE
    0x11600309: ("ev_battery_level_wh", "Wh", 1.0),    # EV_BATTERY_LEVEL
    0x11600308: ("ev_battery_capacity_wh", "Wh", 1.0), # INFO_EV_BATTERY_CAPACITY
    0x1160030C: ("power_kw", "kW", -0.001),            # EV_BATTERY_INSTANTANEOUS_CHARGE_RATE (mW, + = charging)
    0x15600309: ("tire_pressure_kpa", "kPa", 1.0),     # TIRE_PRESSURE (per area)
}


def parse_lines(lines, vehicle_id: str) -> tuple[list[Signal], int]:
    out, errors = [], 0
    cap = None
    pending_level = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            pid = int(rec["prop"])
            ts = from_epoch(int(rec["timestamp"]) / 1e9)
            val = float(rec["value"])
        except (ValueError, KeyError, TypeError):
            errors += 1
            continue
        if pid not in VHAL_PROPS:
            continue
        name, unit, k = VHAL_PROPS[pid]
        ref = f"VHAL 0x{pid:08X} area {rec.get('areaId', 0)}"
        if name == "ev_battery_capacity_wh":
            cap = val
            continue
        if name == "ev_battery_level_wh":
            pending_level.append((ts, val, ref))
            continue
        out.append(Signal(ts, vehicle_id, name, round(val * k, 3), unit, "vhal", ref))
    for ts, val, ref in pending_level:
        if cap:
            out.append(Signal(ts, vehicle_id, "soc_pct", round(val / cap * 100, 2), "%", "vhal", ref))
        else:
            errors += 1
    return out, errors


def to_line(pid: int, value: float, epoch: float, area: int = 0) -> str:
    return json.dumps({"prop": pid, "areaId": area, "timestamp": int(epoch * 1e9), "value": value})
