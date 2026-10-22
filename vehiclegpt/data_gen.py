"""Reproducible SYNTHETIC fleet generator (3 battery-electric vehicles).

All records are labelled synthetic=1. Diagnostic codes use a `SIM-` prefix and are NOT real
OEM or OBD-II codes. Injected scenarios are written to data/ground_truth.json.

SYN-EV-001 "demo" (seed 42, identical to VehicleGPT 1.0)
  GT-ENERGY-HWY   2026-09-11 highway trip -> elevated daily consumption
  GT-BATT-HOT     2026-09-11 DC fast charge on a hot day -> battery temperature warning
  GT-GAP          2026-09-08 03:00-06:00 logger outage
  GT-IMPLAUSIBLE  2026-09-13 10:20 battery temp spike 151 C
  GT-SOC-DROPOUT  2026-09-14 09:00-10:00 SOC NULL
  GT-COLD-WEEK    2026-09-15..21 cold snap + low tyre pressure
  GT-TPMS-REPEAT  SIM-TPMS-L01 x4
  GT-MOTOR-RISE   2026-09-19 10:00-10:40 coolant pump fault
SYN-EV-002 "fastcharger" — repeated DC fast-charge battery temperature episodes (09-05, 09-10, 09-17)
SYN-EV-003 "fleet" — data-quality problems: duplicate timestamps (09-06), °F ambient unit error (09-09),
  flat-lined motor temperature sensor (09-12), and logging stops on 09-19 (stale data)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import config, db

UNITS = [
    ("ts", "ISO-8601 local time", "Sample timestamp (10-minute logging interval)"),
    ("speed_kmh", "km/h", "Average vehicle speed over interval"),
    ("soc_pct", "%", "Battery state of charge"),
    ("battery_temp_c", "°C", "Battery pack temperature"),
    ("motor_temp_c", "°C", "Traction motor temperature"),
    ("ambient_temp_c", "°C", "Outside air temperature"),
    ("power_kw", "kW", "Battery power (+ discharge, - charge)"),
    ("distance_km", "km", "Distance travelled in interval"),
    ("energy_kwh", "kWh", "Energy consumed in interval (traction + HVAC)"),
    ("odometer_km", "km", "Odometer"),
    ("tire_pressure_kpa", "kPa", "Lowest tyre pressure"),
]

FLEET = [
    {"vehicle_id": "SYN-EV-001", "seed": 42, "profile": "demo", "name": "VehicleGPT Demo EV (synthetic)",
     "nickname": "Blue Commuter", "vin": "SYNVIN00000000001", "pairing_code": "PAIR-001-DEMO",
     "type": "Battery electric compact SUV", "odo": 12_480.0},
    {"vehicle_id": "SYN-EV-002", "seed": 7, "profile": "fastcharger", "name": "Fast-charge Hatchback (synthetic)",
     "nickname": "Road Tripper", "vin": "SYNVIN00000000002", "pairing_code": "PAIR-002-DEMO",
     "type": "Battery electric hatchback", "odo": 33_910.0},
    {"vehicle_id": "SYN-EV-003", "seed": 11, "profile": "fleet", "name": "Fleet Van (synthetic)",
     "nickname": "Depot Van 3", "vin": "SYNVIN00000000003", "pairing_code": "PAIR-003-DEMO",
     "type": "Battery electric light van", "odo": 58_200.0},
]


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _ambient(day: int, hour: float, rng, profile: str) -> float:
    base = 21.0
    if profile == "demo":
        if day == 10:
            base = 31.0
        if day >= 14:
            base = 7.0
    elif profile == "fastcharger" and day in (4, 9, 16):
        base = 30.0
    diurnal = 5.0 * np.sin((hour - 9) / 24 * 2 * np.pi)
    return float(base + diurnal + rng.normal(0, 0.6))


def _trips_for_day(day: int, weekday: int, profile: str) -> list[tuple[float, float, float]]:
    if weekday < 5:
        trips = [(7.5, 8.33, 48.0), (17.5, 18.5, 45.0)]
    else:
        trips = [(10.0, 11.0, 42.0)]
    if profile == "demo":
        if day == 10:
            trips = [(7.5, 8.33, 48.0), (11.5, 12.83, 112.0), (17.5, 18.5, 45.0)]
        if day >= 14 and weekday < 5:
            trips = [(7.5, 8.33, 62.0), (17.5, 18.5, 60.0)]
    elif profile == "fastcharger" and day in (4, 9, 16):
        trips = [(9.0, 11.0, 105.0)]
    elif profile == "fleet":
        trips = [(8.0, 10.0, 38.0), (13.0, 15.0, 36.0)] if weekday < 6 else []
    return trips


def _gen_vehicle(conn, v: dict) -> dict:
    rng = np.random.default_rng(v["seed"])
    vid, profile = v["vehicle_id"], v["profile"]
    conn.execute(
        "INSERT INTO vehicles (vehicle_id, name, vehicle_type, usable_battery_kwh, nominal_battery_kwh, synthetic, notes,"
        " nickname, vin, pairing_code_hash, data_mode) VALUES (?,?,?,?,?,1,?,?,?,?, 'historical-synthetic')",
        (vid, v["name"], v["type"], config.USABLE_BATTERY_KWH, config.NOMINAL_BATTERY_KWH,
         f"Synthetic dataset, seed={v['seed']}, profile={profile}. Not a real vehicle.",
         v["nickname"], v["vin"], hash_code(v["pairing_code"])))
    start = datetime.fromisoformat(config.DATASET_START)
    gen_start = start - timedelta(days=config.PREHISTORY_DAYS)
    n = (config.DATASET_DAYS + config.PREHISTORY_DAYS) * 24 * 60 // config.SAMPLE_MINUTES
    arng = np.random.default_rng(v["seed"] + 1000)  # separate stream: ADAS/camera events
    adas = []
    has_adas = profile in ("demo", "fastcharger")
    dt_h = config.SAMPLE_MINUTES / 60
    soc, batt_t, motor_t, odo = 82.0, 21.0, 21.0, v["odo"]
    rows, dtcs = [], []
    trip_counter, current_trip, last_tpms_day, frozen = 0, None, None, None
    demo = profile == "demo"
    prefix = vid[-3:]

    def dtc(ts, code, sub, desc, level):
        dtcs.append((vid, ts.isoformat(), code, sub, desc, level, f"SYN-DTC-LOG/{prefix}/{ts:%Y%m%d}", 1))

    if demo:
        dtc(start + timedelta(days=5, hours=19, minutes=12), "SIM-INF-U03", "infotainment",
            "Infotainment module communication timeout (synthetic)", "info")

    for i in range(n):
        ts = gen_start + timedelta(minutes=config.SAMPLE_MINUTES * i)
        day = (ts - start).days  # negative = pre-history (normal operation)
        hour = ts.hour + ts.minute / 60
        amb = _ambient(day, hour, rng, profile)
        if demo and day == 7 and 3 <= hour < 6:
            continue
        if profile == "fleet" and day >= 18:
            break  # logger stopped -> stale data
        tire = 240.0 + rng.normal(0, 1.0)
        low_tire = demo and day >= 14
        if low_tire:
            tire = 205.0 + rng.normal(0, 2.0)

        state, speed, power, dist, energy, trip_id = "parked", 0.0, 0.0, 0.0, 0.0, None
        active = [t for t in _trips_for_day(day, ts.weekday(), profile) if t[0] <= hour < t[1]]
        if active:
            s, e, cruise = active[0]
            if current_trip != (day, s):
                trip_counter += 1
                current_trip = (day, s)
            trip_id = f"TRIP-{prefix}-{trip_counter:04d}" if not demo else f"TRIP-{trip_counter:04d}"
            state = "driving"
            speed = max(5.0, cruise + rng.normal(0, 4))
            cons = 13.0 + 0.0007 * speed ** 2 + 0.12 * abs(amb - 21)
            if profile == "fleet":
                cons *= 1.25
            if low_tire:
                cons *= 1.06
            cons *= 1 + rng.normal(0, 0.02)
            dist = speed * dt_h
            energy = dist * cons / 100
            power = energy / dt_h
            soc -= energy / config.USABLE_BATTERY_KWH * 100
            if has_adas and speed > 40:
                _adas_sample(adas, arng, vid, prefix, ts, day, speed, amb, profile)
            if low_tire and last_tpms_day != day and day in (14, 15, 17, 19) and hour < 12:
                dtc(ts, "SIM-TPMS-L01", "tyres", "Low tyre pressure detected (synthetic)", "warning")
                last_tpms_day = day
        else:
            current_trip = None
            fast = (demo and day == 10 and 13.0 <= hour < 13.67) or \
                   (profile == "fastcharger" and day in (4, 9, 16) and 12.5 <= hour < 13.17)
            ac = (hour >= 22 or hour < 4) and soc < 90
            if fast:
                state, power = "charging", -110.0
            elif ac:
                state, power = "charging", -7.2
            if state == "charging":
                soc = min(100.0, soc + (-power) * dt_h * 0.92 / config.USABLE_BATTERY_KWH * 100)
                if soc >= 100:
                    power = 0.0

        target_b = amb + 3 + 0.04 * max(power, 0) + 0.17 * max(-power, 0)
        batt_t += (target_b - batt_t) * 0.25 + rng.normal(0, 0.15)
        target_m = amb + (22 + 0.22 * speed if state == "driving" else 0)
        k = 0.12
        if demo and day == 18 and 10.0 <= hour < 10.67:
            target_m, k = 120.0, 0.18
        motor_t += (target_m - motor_t) * k + rng.normal(0, 0.2)
        odo += dist

        b_out, soc_out, m_out, a_out = round(batt_t, 2), round(soc, 2), round(motor_t, 2), round(amb, 2)
        if demo and day == 12 and ts.hour == 10 and ts.minute == 20:
            b_out = 151.0
        if demo and day == 13 and 9 <= hour < 10:
            soc_out = None
        if profile == "fleet" and day == 8 and 8 <= hour < 16:
            a_out = round(amb * 9 / 5 + 32, 2)  # unit error: Fahrenheit logged as °C
        if profile == "fleet" and day == 11 and 8.5 <= hour < 15:
            frozen = frozen if frozen is not None else m_out
            m_out = frozen  # sensor flat-line (stuck at last good value)
        row = (vid, ts.isoformat(), trip_id, state, round(speed, 2), soc_out, b_out, m_out, a_out, round(power, 3),
               round(dist, 4), round(energy, 5), round(odo, 2), round(tire, 1), 1, None,
               f"SYN-TEL/{prefix}/{ts:%Y%m%d}/{i:05d}", 1)
        rows.append(row)
        if profile == "fleet" and day == 5 and ts.hour == 9 and ts.minute <= 40:
            rows.append(row[:16] + (row[16] + "-DUP",) + row[17:])  # duplicate timestamp

        if demo and day == 10 and ts.hour == 13 and ts.minute == 30:
            dtc(ts, "SIM-BMS-T01", "battery", "Battery pack temperature high (synthetic)", "warning")
        if profile == "fastcharger" and day in (4, 9, 16) and ts.hour == 13 and ts.minute == 0:
            dtc(ts, "SIM-BMS-T01", "battery", "Battery pack temperature high (synthetic)", "warning")
        if demo and day == 18 and ts.hour == 10 and ts.minute == 20:
            dtc(ts, "SIM-THM-P02", "thermal", "Motor coolant flow low (synthetic)", "warning")
            dtc(ts + timedelta(minutes=10), "SIM-THM-T05", "thermal", "Motor temperature high (synthetic)", "warning")
        if profile == "fleet" and day == 11 and ts.hour == 9 and ts.minute == 0:
            dtc(ts, "SIM-SNS-M09", "thermal", "Motor temperature sensor signal not changing (synthetic)", "info")

    conn.executemany(
        "INSERT INTO telemetry (vehicle_id, ts, trip_id, state, speed_kmh, soc_pct, battery_temp_c, motor_temp_c,"
        " ambient_temp_c, power_kw, distance_km, energy_kwh, odometer_km, tire_pressure_kpa, valid, validity_note,"
        " source_ref, synthetic) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO diagnostic_events (vehicle_id, ts, code, subsystem, description, reported_level, source_ref,"
        " synthetic) VALUES (?,?,?,?,?,?,?,?)", dtcs)
    conn.executemany(
        "INSERT INTO adas_events (vehicle_id, ts, feature, event, reason, speed_kmh, duration_s, source_ref, synthetic)"
        " VALUES (?,?,?,?,?,?,?,?,1)", adas)
    return {"vehicle_id": vid, "telemetry_rows": len(rows), "dtc_rows": len(dtcs), "adas_rows": len(adas)}


def _adas_sample(adas, rng, vid, prefix, ts, day, speed, amb, profile):
    """Synthetic ADAS (ACC / lane keeping) disengagements and front-camera status events.
    Injected: SYN-EV-001 on 2026-09-20 (yesterday) — 3 disengagements incl. camera blockage in cold/wet weather;
    cold week (09-15..21) has elevated lane-keeping disengagements."""
    src = f"SYN-ADAS/{prefix}/{ts:%Y%m%d%H%M}"
    feature = "ACC" if speed > 70 else "LKA"
    p = 0.02
    if profile == "demo" and day >= 14:
        p = 0.06
    injected = profile == "demo" and day == 19 and ts.hour == 10 and ts.minute in (10, 20, 30)
    if injected or rng.random() < p:
        if injected and ts.minute == 10:
            adas.append((vid, (ts + timedelta(minutes=2)).isoformat(), "front_camera", "camera_blocked", "lens obstruction (road spray)",
                                         round(speed, 1), 240, src))
            reason = "sensor_blocked"
        else:
            reason = rng.choice(["driver_override", "lane_markings_lost", "driver_override", "system_limit"],
                                p=[0.4, 0.3, 0.2, 0.1] if amb > 10 else [0.3, 0.45, 0.2, 0.05]) if not injected else \
                "lane_markings_lost"
        adas.append((vid, (ts + timedelta(minutes=int(rng.integers(0, 9)))).isoformat(), feature, "disengagement", str(reason),
                     round(speed, 1), int(rng.integers(5, 120)), src))


def generate(db_path: Path | str | None = None, seed: int = config.SEED, fleet: list[dict] | None = None) -> dict:
    """Create a fresh database. `seed` overrides the demo vehicle's seed (others keep theirs)."""
    p = Path(db_path or config.DB_PATH)
    if p.exists():
        p.unlink()
    conn = db.connect(p)
    db.init_schema(conn)
    conn.executemany("INSERT INTO units VALUES (?,?,?)", UNITS)
    per = []
    for v in fleet or FLEET:
        v = dict(v, seed=seed) if v["profile"] == "demo" else v
        per.append(_gen_vehicle(conn, v))
    conn.commit()
    from .validation import validate_telemetry
    vstats = validate_telemetry(conn)
    conn.close()
    config.GROUND_TRUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GROUND_TRUTH_PATH.write_text(json.dumps(ground_truth(), indent=2))
    demo = per[0]
    return {"telemetry_rows": demo["telemetry_rows"], "dtc_rows": demo["dtc_rows"], "vehicles": per,
            "validation": vstats, "seed": seed}


def add_missing_vehicles(conn) -> list[str]:
    """Non-destructive upgrade: generate fleet vehicles that don't exist yet and backfill v2 metadata
    (nickname, VIN, pairing code) on existing vehicles where those columns are NULL. Existing rows untouched."""
    have = {r[0] for r in conn.execute("SELECT vehicle_id FROM vehicles")}
    added = []
    if not conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]:
        conn.executemany("INSERT INTO units VALUES (?,?,?)", UNITS)
    for v in FLEET:
        if v["vehicle_id"] in have:
            conn.execute("UPDATE vehicles SET nickname = COALESCE(nickname, ?), vin = COALESCE(vin, ?),"
                         " pairing_code_hash = COALESCE(pairing_code_hash, ?) WHERE vehicle_id = ?",
                         (v["nickname"], v["vin"], hash_code(v["pairing_code"]), v["vehicle_id"]))
        else:
            _gen_vehicle(conn, v)
            added.append(v["vehicle_id"])
    conn.commit()
    if added:
        from .validation import validate_telemetry
        validate_telemetry(conn)
    return added


def ground_truth() -> dict:
    return {
        "note": "Labelled injected scenarios in the synthetic dataset. Events without vehicle_id refer to SYN-EV-001.",
        "events": [
            {"id": "GT-ENERGY-HWY", "category": "energy", "start": "2026-09-11T00:00:00", "end": "2026-09-12T00:00:00"},
            {"id": "GT-BATT-HOT", "category": "battery_temp", "start": "2026-09-11T13:00:00", "end": "2026-09-11T14:30:00"},
            {"id": "GT-GAP", "category": "missing_data", "start": "2026-09-08T02:50:00", "end": "2026-09-08T06:10:00"},
            {"id": "GT-IMPLAUSIBLE", "category": "implausible", "start": "2026-09-13T10:20:00", "end": "2026-09-13T10:21:00"},
            {"id": "GT-SOC-DROPOUT", "category": "missing_data", "start": "2026-09-14T09:00:00", "end": "2026-09-14T10:00:00"},
            {"id": "GT-COLD-WEEK", "category": "energy", "start": "2026-09-15T00:00:00", "end": "2026-09-22T00:00:00"},
            {"id": "GT-TPMS-REPEAT", "category": "repeated_dtc", "start": "2026-09-15T00:00:00", "end": "2026-09-21T00:00:00"},
            {"id": "GT-MOTOR-RISE", "category": "motor_temp", "start": "2026-09-19T09:50:00", "end": "2026-09-19T11:00:00"},
        ],
        "fleet_events": [
            {"vehicle_id": "SYN-EV-002", "id": "GT2-BATT-1", "category": "battery_temp", "start": "2026-09-05T12:30:00", "end": "2026-09-05T14:00:00"},
            {"vehicle_id": "SYN-EV-002", "id": "GT2-BATT-2", "category": "battery_temp", "start": "2026-09-10T12:30:00", "end": "2026-09-10T14:00:00"},
            {"vehicle_id": "SYN-EV-002", "id": "GT2-BATT-3", "category": "battery_temp", "start": "2026-09-17T12:30:00", "end": "2026-09-17T14:00:00"},
            {"vehicle_id": "SYN-EV-002", "id": "GT2-HWY-1", "category": "energy", "start": "2026-09-05T00:00:00", "end": "2026-09-06T00:00:00"},
            {"vehicle_id": "SYN-EV-002", "id": "GT2-HWY-2", "category": "energy", "start": "2026-09-10T00:00:00", "end": "2026-09-11T00:00:00"},
            {"vehicle_id": "SYN-EV-002", "id": "GT2-HWY-3", "category": "energy", "start": "2026-09-17T00:00:00", "end": "2026-09-18T00:00:00"},
            {"vehicle_id": "SYN-EV-003", "id": "GT3-UNIT-IMPL", "category": "implausible", "start": "2026-09-09T08:00:00", "end": "2026-09-09T16:00:00"},
            {"vehicle_id": "SYN-EV-003", "id": "GT3-DUP", "category": "duplicate", "start": "2026-09-06T09:00:00", "end": "2026-09-06T09:50:00"},
            {"vehicle_id": "SYN-EV-003", "id": "GT3-UNIT", "category": "unit", "start": "2026-09-09T08:00:00", "end": "2026-09-09T16:00:00"},
            {"vehicle_id": "SYN-EV-003", "id": "GT3-FLAT", "category": "flatline", "start": "2026-09-12T08:30:00", "end": "2026-09-12T15:00:00"},
            {"vehicle_id": "SYN-EV-003", "id": "GT3-STALE", "category": "stale", "start": "2026-09-19T00:00:00", "end": "2026-09-22T00:00:00"},
        ],
        "expected_answers": {
            "Why did my driving range decrease over the last seven days?":
                "Range estimate fell because average consumption rose in 2026-09-15..21 vs 2026-09-08..14; "
                "colder ambient, higher average speed and repeated low-tyre-pressure codes are possible contributors.",
            "What happened before the battery temperature warning?":
                "SIM-BMS-T01 at 2026-09-11 13:30 was preceded by a ~1.3 h highway trip (~112 km/h) and a "
                "~110 kW DC fast-charge session starting 13:00 at ~31-36 C ambient.",
            "Which diagnostic warnings occurred repeatedly?": "SIM-TPMS-L01 (4 occurrences, 2026-09-15..20).",
            "Show unusual events from yesterday.": "2026-09-20: low tyre pressure code SIM-TPMS-L01 and elevated consumption.",
            "What is the brake pad wear?": "Not available — dataset has no brake wear data.",
            "Which vehicles experienced repeated temperature anomalies?": "SYN-EV-002 (3 battery temperature episodes).",
        },
    }


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2))
