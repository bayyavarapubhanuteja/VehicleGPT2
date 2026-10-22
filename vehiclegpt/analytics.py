"""Deterministic, validated analytics. Every number shown to the user is computed here (never by the LLM)."""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from . import config, db

METRICS = {
    "battery_temp_c": ("battery temperature", "°C"),
    "motor_temp_c": ("motor temperature", "°C"),
    "ambient_temp_c": ("outside temperature", "°C"),
    "speed_kmh": ("speed", "km/h"),
    "soc_pct": ("state of charge", "%"),
    "power_kw": ("battery power", "kW"),
    "tire_pressure_kpa": ("tyre pressure", "kPa"),
}


@dataclass
class Period:
    start: pd.Timestamp
    end: pd.Timestamp  # exclusive
    label: str

    @property
    def s(self) -> str:
        return self.start.isoformat()

    @property
    def e(self) -> str:
        return self.end.isoformat()

    def to_dict(self):
        return {"start": self.s, "end": self.e, "label": self.label}


def reference_now(conn) -> pd.Timestamp:
    """Synthetic data is historical; 'now' is the end of the latest recorded day across the fleet."""
    hi = pd.Timestamp(conn.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0])
    return hi.normalize() + pd.Timedelta(days=1)


def last_n_days(conn, n: int, offset_days: int = 0, label: str | None = None) -> Period:
    end = reference_now(conn) - pd.Timedelta(days=offset_days)
    return Period(end - pd.Timedelta(days=n), end, label or f"last {n} days")


def day(conn, date: pd.Timestamp, label: str | None = None) -> Period:
    d = pd.Timestamp(date).normalize()
    return Period(d, d + pd.Timedelta(days=1), label or f"{d:%Y-%m-%d}")


@dataclass
class PeriodStats:
    period: dict
    samples: int
    driving_samples: int
    trips: int
    distance_km: float
    energy_kwh: float
    consumption_kwh_per_100km: float | None
    est_range_km: float | None
    avg_driving_speed_kmh: float | None
    avg_driving_ambient_c: float | None
    min_tire_pressure_kpa: float | None
    coverage_pct: float
    source_refs: tuple[str, str] | None

    def to_dict(self):
        return asdict(self)


def consumption(energy_kwh: float, distance_km: float) -> float | None:
    if distance_km <= 0:
        return None
    return energy_kwh / distance_km * 100.0


def estimated_range_km(usable_kwh: float, kwh_per_100km: float | None) -> float | None:
    """Range = usable energy / (energy per km). kWh/100km -> kWh/km by dividing by 100."""
    if not kwh_per_100km or kwh_per_100km <= 0:
        return None
    return usable_kwh / (kwh_per_100km / 100.0)


def period_stats(conn, period: Period, usable_kwh: float | None = None,
                 vehicle_id: str = config.VEHICLE_ID) -> PeriodStats:
    df = db.read_telemetry(conn, period.s, period.e, vehicle_id)
    usable_kwh = usable_kwh or db.get_vehicle(conn, vehicle_id).get("usable_battery_kwh", config.USABLE_BATTERY_KWH)
    expected = int((period.end - period.start) / pd.Timedelta(minutes=config.SAMPLE_MINUTES))
    drv = df[df["state"] == "driving"]
    dist = float(drv["distance_km"].sum())
    en = float(drv["energy_kwh"].sum())
    cons = consumption(en, dist)
    return PeriodStats(
        period=period.to_dict(),
        samples=len(df),
        driving_samples=len(drv),
        trips=int(drv["trip_id"].nunique()),
        distance_km=round(dist, 1),
        energy_kwh=round(en, 2),
        consumption_kwh_per_100km=round(cons, 2) if cons else None,
        est_range_km=round(estimated_range_km(usable_kwh, cons), 0) if cons else None,
        avg_driving_speed_kmh=round(float(drv["speed_kmh"].mean()), 1) if len(drv) else None,
        avg_driving_ambient_c=round(float(drv["ambient_temp_c"].mean()), 1) if len(drv) else None,
        min_tire_pressure_kpa=round(float(df["tire_pressure_kpa"].min()), 1) if len(df) else None,
        coverage_pct=round(100.0 * len(df) / expected, 1) if expected else 0.0,
        source_refs=(df["source_ref"].iloc[0], df["source_ref"].iloc[-1]) if len(df) else None,
    )


def pct_change(a: float | None, b: float | None) -> float | None:
    if a in (None, 0) or b is None:
        return None
    return (b - a) / a * 100.0


def compare_periods(conn, p_old: Period, p_new: Period, vehicle_id: str = config.VEHICLE_ID) -> dict:
    a, b = period_stats(conn, p_old, vehicle_id=vehicle_id), period_stats(conn, p_new, vehicle_id=vehicle_id)
    keys = ["distance_km", "energy_kwh", "consumption_kwh_per_100km", "est_range_km",
            "avg_driving_speed_kmh", "avg_driving_ambient_c", "min_tire_pressure_kpa"]
    diff = {}
    for k in keys:
        va, vb = getattr(a, k), getattr(b, k)
        diff[k] = {
            "old": va, "new": vb,
            "abs": round(vb - va, 2) if va is not None and vb is not None else None,
            "pct": round(pct_change(va, vb), 1) if pct_change(va, vb) is not None else None,
        }
    return {"old": a.to_dict(), "new": b.to_dict(), "diff": diff}


def metric_stat(conn, metric: str, period: Period, agg: str = "mean", vehicle_id: str = config.VEHICLE_ID) -> dict:
    if metric not in METRICS:
        raise ValueError(f"unsupported metric {metric}")
    df = db.read_telemetry(conn, period.s, period.e, vehicle_id, include_invalid=False)
    s = df[metric].dropna()
    excluded = len(db.read_telemetry(conn, period.s, period.e, vehicle_id)) - len(df)
    if s.empty:
        return {"metric": metric, "agg": agg, "value": None, "n": 0, "excluded_invalid": excluded}
    fn = {"mean": s.mean, "max": s.max, "min": s.min}[agg]
    val = float(fn())
    idx = s.idxmax() if agg == "max" else s.idxmin() if agg == "min" else None
    rec = df.loc[idx] if idx is not None else None
    return {
        "metric": metric, "agg": agg, "value": round(val, 2), "n": int(s.size), "excluded_invalid": excluded,
        "at_ts": rec["ts"].isoformat() if rec is not None else None,
        "record_id": int(rec["id"]) if rec is not None else None,
        "source_ref": rec["source_ref"] if rec is not None else None,
    }


def daily_consumption(conn, start: str | None = None, end: str | None = None,
                      vehicle_id: str = config.VEHICLE_ID) -> pd.DataFrame:
    df = db.read_telemetry(conn, start, end, vehicle_id)
    drv = df[df["state"] == "driving"].copy()
    drv["date"] = drv["ts"].dt.normalize()
    g = drv.groupby("date").agg(distance_km=("distance_km", "sum"), energy_kwh=("energy_kwh", "sum"),
                                avg_speed=("speed_kmh", "mean"), avg_ambient=("ambient_temp_c", "mean"),
                                first_id=("id", "min"), last_id=("id", "max"))
    g["kwh_per_100km"] = g["energy_kwh"] / g["distance_km"] * 100
    return g.reset_index()


def repeated_dtcs(conn, period: Period | None = None, min_count: int = 2,
                  vehicle_id: str = config.VEHICLE_ID) -> pd.DataFrame:
    df = db.read_dtcs(conn, period.s if period else None, period.e if period else None, vehicle_id)
    if df.empty:
        return df
    g = df.groupby(["code", "subsystem", "description"]).agg(
        count=("id", "size"), first=("ts", "min"), last=("ts", "max"), ids=("id", list)).reset_index()
    return g[g["count"] >= min_count].sort_values("count", ascending=False)


def data_gaps(df: pd.DataFrame, gap_minutes: float) -> list[dict]:
    if len(df) < 2:
        return []
    d = df["ts"].diff().dt.total_seconds() / 60
    out = []
    for i in np.where(d > gap_minutes)[0]:
        out.append({"from": df["ts"].iloc[i - 1].isoformat(), "to": df["ts"].iloc[i].isoformat(),
                    "minutes": float(d.iloc[i]), "before_id": int(df["id"].iloc[i - 1]),
                    "after_id": int(df["id"].iloc[i])})
    return out
