"""What-If simulator. All outputs are HYPOTHETICAL ESTIMATES from simplified models.

Historical records are only read, never modified; runs are stored in `simulation_runs`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime

from . import analytics, config, db

KM_PER_MILE = 1.609344

PARAM_LIMITS = {
    "consumption_change_pct": (-50.0, 100.0),
    "speed_change_pct": (-50.0, 50.0),
    "usable_energy_change_pct": (-40.0, 10.0),
    "cooling_improvement_pct": (-50.0, 60.0),
    "ambient_c": (-30.0, 50.0),
}

AERO_SHARE = 0.40  # assumed share of consumption that scales with speed² at the baseline speed


@dataclass
class Params:
    consumption_change_pct: float = 0.0
    speed_change_pct: float = 0.0
    usable_energy_change_pct: float = 0.0
    cooling_improvement_pct: float = 0.0
    ambient_c: float | None = None
    unit: str = "km"  # km | mi

    def validate(self) -> list[str]:
        errs = []
        for k, (lo, hi) in PARAM_LIMITS.items():
            v = getattr(self, k)
            if v is None:
                continue
            if not isinstance(v, (int, float)):
                errs.append(f"{k} must be numeric")
            elif not lo <= v <= hi:
                errs.append(f"{k}={v} outside supported range [{lo}, {hi}]")
        if self.unit not in ("km", "mi"):
            errs.append("unit must be 'km' or 'mi'")
        return errs


def baseline(conn, period: analytics.Period | None = None, vehicle_id: str = config.VEHICLE_ID) -> dict:
    period = period or analytics.last_n_days(conn, 7)
    st = analytics.period_stats(conn, period, vehicle_id=vehicle_id)
    v = db.get_vehicle(conn, vehicle_id)
    tel = db.read_telemetry(conn, period.s, period.e, vehicle_id, include_invalid=False)
    chg = tel[(tel["state"] == "charging") & (tel["power_kw"] < -50)]
    hot = tel.loc[tel["battery_temp_c"].idxmax()] if len(tel) else None
    return {
        "vehicle_id": vehicle_id,
        "period": period.to_dict(),
        "usable_kwh": v["usable_battery_kwh"],
        "consumption_kwh_per_100km": st.consumption_kwh_per_100km,
        "avg_speed_kmh": st.avg_driving_speed_kmh,
        "ambient_c": st.avg_driving_ambient_c,
        "range_km": st.est_range_km,
        "peak_battery_temp_c": round(float(hot["battery_temp_c"]), 2) if hot is not None else None,
        "peak_ambient_c": round(float(hot["ambient_temp_c"]), 2) if hot is not None else None,
        "fast_charge_samples": int(len(chg)),
    }


def _to_unit(km: float, unit: str) -> float:
    return km / KM_PER_MILE if unit == "mi" else km


def simulate(base: dict, p: Params) -> dict:
    errs = p.validate()
    if errs:
        raise ValueError("; ".join(errs))
    if not base.get("consumption_kwh_per_100km"):
        raise ValueError("Baseline has no driving data; cannot simulate.")
    c0 = base["consumption_kwh_per_100km"]
    # 1) speed effect: aero portion scales with (v1/v0)^2
    sf = 1 + p.speed_change_pct / 100
    speed_factor = (1 - AERO_SHARE) + AERO_SHARE * sf ** 2
    # 2) ambient effect (HVAC): +0.12 kWh/100km per °C away from 21 °C (matches dataset generator assumption)
    hvac_delta = 0.0
    if p.ambient_c is not None and base.get("ambient_c") is not None:
        hvac_delta = 0.12 * (abs(p.ambient_c - 21) - abs(base["ambient_c"] - 21))
    c1 = (c0 * speed_factor + hvac_delta) * (1 + p.consumption_change_pct / 100)
    e1 = base["usable_kwh"] * (1 + p.usable_energy_change_pct / 100)
    r0 = analytics.estimated_range_km(base["usable_kwh"], c0)
    r1 = analytics.estimated_range_km(e1, c1)

    # 3) simplified steady-state thermal model: T_peak = T_amb + ΔT0 * (1 - cooling_improvement)
    thermal = None
    if base.get("peak_battery_temp_c") is not None and base.get("peak_ambient_c") is not None:
        dT0 = base["peak_battery_temp_c"] - base["peak_ambient_c"]
        amb = p.ambient_c if p.ambient_c is not None else base["peak_ambient_c"]
        t1 = amb + dT0 * (1 - p.cooling_improvement_pct / 100)
        thermal = {"baseline_peak_c": base["peak_battery_temp_c"], "sim_peak_c": round(t1, 1),
                   "abs": round(t1 - base["peak_battery_temp_c"], 1),
                   "formula": "T_peak = T_ambient + (T_peak0 − T_ambient0) × (1 − cooling_improvement)"}

    u = p.unit
    res = {
        "label": "HYPOTHETICAL ESTIMATE — not a measurement or guaranteed prediction",
        "unit": u,
        "consumption": {"baseline": round(c0, 2), "simulated": round(c1, 2), "abs": round(c1 - c0, 2),
                        "pct": round((c1 - c0) / c0 * 100, 1),
                        "unit": "kWh/100 km" if u == "km" else "kWh/100 mi",
                        "baseline_u": round(c0 if u == "km" else c0 * KM_PER_MILE, 2),
                        "simulated_u": round(c1 if u == "km" else c1 * KM_PER_MILE, 2)},
        "usable_energy_kwh": {"baseline": base["usable_kwh"], "simulated": round(e1, 2)},
        "range": {"baseline": round(_to_unit(r0, u), 1), "simulated": round(_to_unit(r1, u), 1),
                  "abs": round(_to_unit(r1 - r0, u), 1), "pct": round((r1 - r0) / r0 * 100, 1), "unit": u},
        "thermal": thermal,
        "formulas": [
            "range_km = usable_energy_kWh ÷ (consumption_kWh_per_100km ÷ 100)",
            f"speed_factor = (1 − {AERO_SHARE}) + {AERO_SHARE} × (v_new / v_base)²",
            "hvac_delta = 0.12 × (|T_new − 21| − |T_base − 21|)  [kWh/100 km]",
            "consumption_new = (consumption_base × speed_factor + hvac_delta) × (1 + consumption_change)",
            "miles = km ÷ 1.609344",
        ],
        "assumptions": [
            f"{int(AERO_SHARE * 100)}% of baseline consumption is aerodynamic and scales with speed squared.",
            "HVAC sensitivity of 0.12 kWh/100 km per °C away from 21 °C (demonstration constant).",
            "Usable energy is fully available (no reserve buffer, no temperature derating).",
            "Thermal model is steady-state and first-order; it ignores thermal mass and charge-rate limits.",
        ],
        "limitations": [
            "Simplified models calibrated to a synthetic dataset; real vehicles differ.",
            "Does not account for elevation, traffic, payload, wind, tyre condition or battery ageing beyond the energy slider.",
        ],
        "speed_factor": round(speed_factor, 4),
        "hvac_delta": round(hvac_delta, 3),
    }
    return res


def sweep(base: dict, p: Params, param: str, values: list[float]) -> list[dict]:
    """Sensitivity analysis: vary one parameter, keep others fixed."""
    from dataclasses import replace
    out = []
    for v in values:
        r = simulate(base, replace(p, **{param: v}))
        out.append({param: v, "range": r["range"]["simulated"], "consumption": r["consumption"]["simulated"],
                    "peak_temp": r["thermal"]["sim_peak_c"] if r["thermal"] else None})
    return out


def save_run(conn, scenario: str, base: dict, p: Params, result: dict) -> int:
    cur = conn.execute(
        "INSERT INTO simulation_runs (created_ts, scenario, baseline_json, params_json, result_json) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), scenario, json.dumps(base), json.dumps(asdict(p)), json.dumps(result)))
    conn.commit()
    return cur.lastrowid


def list_runs(conn, limit: int = 20, vehicle_ids: list[str] | None = None) -> list[dict]:
    rows = conn.execute("SELECT * FROM simulation_runs ORDER BY id DESC LIMIT ?", (limit * 5,)).fetchall()
    if vehicle_ids is not None:
        rows = [r for r in rows if json.loads(r["baseline_json"]).get("vehicle_id", config.VEHICLE_ID) in vehicle_ids][:limit]
    return [{**dict(r), "params": json.loads(r["params_json"]), "result": json.loads(r["result_json"])} for r in rows]
