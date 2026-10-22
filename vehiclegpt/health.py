"""Explainable Vehicle Health Indicator (0–100).

NOT an official manufacturer rating or certified diagnosis. It is a transparent sum of documented
penalties computed from validated records in a 7-day window. Acknowledging an alert does not change it.

Penalties (demonstration weights)
  alerts in window      High −20, Medium −8, Low −3  (max −50; data-quality alerts count half)
  battery temperature   peak ≥ high threshold −10, ≥ medium −5
  repeated DTC codes    −8 per code with ≥ 2 occurrences in 14 days
  efficiency            consumption vs previous 14 days: > +20 % −10, > +10 % −5
Confidence is reduced when data completeness < 90 % or the latest data is > 24 h old.
"""
from __future__ import annotations

import pandas as pd

from . import analytics, config, db, dataquality

SEV_PTS = {"High": 20, "Medium": 8, "Low": 3, "Informational": 0}


def compute(conn, vehicle_id: str, as_of: pd.Timestamp | None = None) -> dict:
    as_of = pd.Timestamp(as_of) if as_of is not None else analytics.reference_now(conn)
    win = analytics.Period(as_of - pd.Timedelta(days=7), as_of, "7 days")
    th = db.get_thresholds(conn)
    factors = []

    al = db.read_alerts(conn, win.s, win.e, vehicle_id)
    al = al[al["rule_id"] != "R-DTC-SINGLE"]
    pts = 0.0
    for _, a in al.iterrows():
        p = SEV_PTS[a["severity"]] * (0.5 if a["subsystem"] == "data" else 1)
        pts += p
    pts = min(pts, 50)
    if pts:
        factors.append({"factor": "Alerts in the last 7 days", "points": -round(pts, 1),
                        "detail": ", ".join(f"{k}×{v}" for k, v in al["severity"].value_counts().items()),
                        "evidence": [int(i) for i in al["id"]]})

    tel = db.read_telemetry(conn, win.s, win.e, vehicle_id, include_invalid=False)
    if len(tel) and tel["battery_temp_c"].notna().any():
        mx = float(tel["battery_temp_c"].max())
        p = 10 if mx >= th["battery_temp_high_c"] else 5 if mx >= th["battery_temp_medium_c"] else 0
        factors.append({"factor": "Peak battery temperature", "points": -p, "detail": f"{mx:.1f} °C", "evidence": []})

    p14 = analytics.Period(as_of - pd.Timedelta(days=14), as_of, "14 days")
    rep = analytics.repeated_dtcs(conn, p14, 2, vehicle_id)
    if len(rep):
        factors.append({"factor": "Repeated diagnostic codes", "points": -8 * len(rep),
                        "detail": ", ".join(f"{r['code']}×{r['count']}" for _, r in rep.iterrows()),
                        "evidence": [i for ids in rep["ids"] for i in ids]})

    base = analytics.Period(win.start - pd.Timedelta(days=14), win.start, "previous 14 days")
    c_new = analytics.period_stats(conn, win, vehicle_id=vehicle_id).consumption_kwh_per_100km
    c_old = analytics.period_stats(conn, base, vehicle_id=vehicle_id).consumption_kwh_per_100km
    if c_new and c_old:
        d = (c_new - c_old) / c_old * 100
        p = 10 if d > 20 else 5 if d > 10 else 0
        factors.append({"factor": "Energy efficiency vs normal", "points": -p,
                        "detail": f"{c_new:.1f} vs {c_old:.1f} kWh/100 km ({d:+.0f}%)", "evidence": []})

    score = max(0.0, 100 + sum(f["points"] for f in factors))
    st = analytics.period_stats(conn, win, vehicle_id=vehicle_id)
    stale = dataquality.staleness(conn, vehicle_id)
    limitations = ["Indicator built from synthetic data and demonstration weights — not a manufacturer rating or diagnosis.",
                   "Components without sensors (brakes, suspension, 12 V battery, tyres' tread) are not assessed."]
    confidence = "normal"
    if st.coverage_pct < 90:
        confidence = "reduced"
        limitations.append(f"Only {st.coverage_pct}% of expected samples exist in the window.")
    if stale["stale"]:
        confidence = "low"
        limitations.append(f"Latest data is {stale['age_hours']:.0f} h old — the score may not reflect current condition.")
    band = "Good" if score >= 80 else "Fair" if score >= 60 else "Needs attention"
    return {"vehicle_id": vehicle_id, "as_of": as_of.isoformat(), "score": round(score), "band": band,
            "factors": factors, "completeness_pct": st.coverage_pct, "data_age_h": stale["age_hours"],
            "confidence": confidence, "limitations": limitations, "method": __doc__}


def trend(conn, vehicle_id: str, days: int = 14) -> pd.DataFrame:
    now = analytics.reference_now(conn)
    rows = [{"as_of": now - pd.Timedelta(days=d), "score": compute(conn, vehicle_id, now - pd.Timedelta(days=d))["score"]}
            for d in range(days, -1, -1)]
    return pd.DataFrame(rows)
