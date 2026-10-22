"""Natural-language query engine.

Pipeline: interpret (rules → optional local-LLM intent fallback) → resolve time range & metrics →
retrieve records (SQL) → compute (deterministic analytics) → explain (templates; optional
grounded LLM rephrasing) → return answer + evidence + limitations.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict

import pandas as pd

from . import analytics, config, db, detective, llm
from .analytics import Period

WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "fourteen": 14, "fortnight": 14, "twenty": 20, "thirty": 30}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

UNSUPPORTED = {
    "brake": "brake wear / brake pad", "pad": "brake pad", "oil": "engine oil (this is an electric vehicle)",
    "tread": "tyre tread depth", "wiper": "wiper", "fuel": "fuel (this is an electric vehicle)",
    "gps": "location / GPS", "location": "location / GPS", "steering": "steering", "suspension": "suspension",
    "airbag": "airbag", "engine": None,  # 'engine' maps to motor below
}

METRIC_WORDS = [
    ("battery temp", "battery_temp_c"), ("pack temp", "battery_temp_c"), ("battery temperature", "battery_temp_c"),
    ("motor temp", "motor_temp_c"), ("engine temp", "motor_temp_c"), ("motor temperature", "motor_temp_c"),
    ("outside temp", "ambient_temp_c"), ("ambient", "ambient_temp_c"), ("speed", "speed_kmh"),
    ("state of charge", "soc_pct"), ("soc", "soc_pct"), ("charge level", "soc_pct"), ("tyre pressure", "tire_pressure_kpa"),
    ("tire pressure", "tire_pressure_kpa"), ("power", "power_kw"),
]


@dataclass
class Answer:
    question: str
    intent: str
    intent_source: str
    periods: list[dict] = field(default_factory=list)
    driver: str = ""
    engineer: str = ""
    facts: list[dict] = field(default_factory=list)      # {label, value, unit, kind}
    evidence: list[dict] = field(default_factory=list)   # {type, id, ts, text, source_ref, kind}
    limitations: list[str] = field(default_factory=list)
    safety: list[str] = field(default_factory=list)
    chart: dict | None = None
    explanation_source: str = "template"
    ai_text: str | None = None
    latency_ms: float = 0.0
    supported: bool = True
    vehicle_id: str = config.VEHICLE_ID
    structured_query: dict = field(default_factory=dict)
    allowed_vehicles: list[str] = field(default_factory=list)
    table: list[dict] | None = None

    def to_dict(self):
        return asdict(self)


def _fact(label, value, unit="", kind="calculated"):
    return {"label": label, "value": value, "unit": unit, "kind": kind}


# ---------------- interpretation ----------------

def parse_period(q: str, conn, default_days: int = 7) -> tuple[Period, bool]:
    """Return (period, explicit?)."""
    ql = q.lower()
    now = analytics.reference_now(conn)
    m = re.search(r"(20\d\d-\d\d-\d\d)", ql)
    if m:
        return analytics.day(conn, pd.Timestamp(m.group(1))), True
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})", ql) or \
        re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", ql)
    if m:
        a, b = m.groups()
        mon, d = (MONTHS[a[:3]], int(b)) if a[:3] in MONTHS else (MONTHS[b[:3]], int(a))
        return analytics.day(conn, pd.Timestamp(year=now.year, month=mon, day=d)), True
    if "yesterday" in ql:
        return analytics.day(conn, now - pd.Timedelta(days=2), "yesterday"), True
    if "today" in ql:
        return analytics.day(conn, now - pd.Timedelta(days=1), "today (latest recorded day)"), True
    m = re.search(r"(?:last|past|previous)\s+(\d+|[a-z]+)\s+days?", ql)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else WORDNUM.get(m.group(1))
        if n:
            return analytics.last_n_days(conn, n), True
    if "last week" in ql or "previous week" in ql:
        return analytics.last_n_days(conn, 7, 7, "last week"), True
    if "this week" in ql or "past week" in ql:
        return analytics.last_n_days(conn, 7, 0, "this week (last 7 days)"), True
    if "fortnight" in ql or "two weeks" in ql:
        return analytics.last_n_days(conn, 14), True
    if "all" in ql.split() or "ever" in ql or "whole" in ql:
        lo, _ = db.data_bounds(conn)
        return Period(lo.normalize(), now, "entire dataset"), True
    return analytics.last_n_days(conn, default_days), False


def find_metric(q: str) -> str | None:
    ql = q.lower().replace("tyre", "tyre")
    for w, m in METRIC_WORDS:
        if w in ql:
            return m
    if re.search(r"motor|engine", ql) and re.search(r"hot|warm|heat|temp", ql):
        return "motor_temp_c"
    if re.search(r"tyre|tire", ql):
        return "tire_pressure_kpa"
    if "temperature" in ql or "temp" in ql or re.search(r"\bhot\b|warm", ql):
        return "battery_temp_c"
    return None


def classify(q: str) -> str:
    ql = q.lower()
    for w, label in UNSUPPORTED.items():
        if w == "fuel" and re.search(r"efficien|economy|consumption", ql):
            continue  # EV: "fuel efficiency" is interpreted as energy efficiency
        if label and re.search(rf"\b{w}", ql):
            return "unsupported"
    if re.search(r"\badas\b|disengag|lane[- ]keep|cruise control|\bacc\b|\blka\b|autopilot|driver assist|camera block", ql):
        return "adas_events"
    if re.search(r"(largest|biggest|worst|sharpest|greatest) (drop|decline|decrease|fall|dip)|when did .*(efficien|economy).*(drop|declin|worse|fall)", ql):
        return "largest_drop"
    if re.search(r"city|urban|town", ql) and re.search(r"highway|motorway|freeway|expressway", ql):
        return "city_highway"
    if re.search(r"which vehicles|across (the )?fleet|all (my |authori[sz]ed )?vehicles", ql) and re.search(r"temp|therm|overheat", ql):
        return "fleet_temp"
    if re.search(r"diagnostic|code|dtc", ql) and re.search(r"energy|consumption", ql) and re.search(r"associat|high|during|elevated", ql):
        return "dtc_energy"
    if re.search(r"before and after|before vs\.? after|before/after", ql):
        return "before_after"
    if re.search(r"missing|implausible|duplicate|stale|data quality|sensor quality|unit (error|inconsisten)", ql):
        return "data_quality"
    if re.search(r"\bexplain\b", ql) and re.search(r"warning|code|diagnostic|alert", ql):
        return "explain_dtc"
    if re.search(r"(higher|more|lower) than (normal|usual)|normal|usual", ql) and re.search(r"energy|consumption|efficien", ql):
        return "consumption_normal"
    if re.search(r"latest|recent|summari[sz]e|last few", ql) and re.search(r"diagnostic|dtc|codes?\b", ql) and "before" not in ql:
        return "latest_dtcs"
    if "range" in ql:
        return "range_change"
    if re.search(r"\b(before|lead up|led up|leading up|preceded|prior to|what happened)\b", ql) and \
            re.search(r"warning|alert|code|event|fault|dtc|happened", ql):
        return "before_warning"
    if re.search(r"repeat|recurr|again|multiple times|more than once|frequent", ql):
        return "repeated_dtc"
    if re.search(r"unusual|abnormal|anomal|strange|weird|odd|unexpected", ql):
        return "unusual_events"
    if re.search(r"compare|vs\.?|versus|than last", ql) and re.search(r"energy|consumption|efficien|kwh", ql):
        return "compare_energy"
    if re.search(r"how far|distance|kilomet|km did|mileage|miles", ql):
        return "distance"
    if re.search(r"average|avg|mean|max|highest|peak|min|lowest|hottest|coldest", ql) and find_metric(q):
        return "metric_stat"
    if re.search(r"energy|consumption|efficien", ql):
        return "compare_energy"
    if re.search(r"alert|warning|issue|problem|fault|code|dtc", ql):
        return "alerts_summary"
    return "unknown"


# ---------------- handlers ----------------

def _h_range(conn, q, a: Answer):
    new, explicit = parse_period(q, conn, 7)
    n = (new.end - new.start).days
    old = Period(new.start - pd.Timedelta(days=n), new.start, f"previous {n} days")
    cmp = analytics.compare_periods(conn, old, new, a.vehicle_id)
    a.periods = [old.to_dict(), new.to_dict()]
    d = cmp["diff"]
    r, c = d["est_range_km"], d["consumption_kwh_per_100km"]
    if r["old"] is None or r["new"] is None:
        a.supported = False
        a.limitations.append("Not enough driving data in one of the periods to estimate range.")
        a.driver = a.engineer = "I can't compare range for that period: there is not enough driving data."
        return
    a.facts += [
        _fact(f"Est. range ({old.label})", r["old"], "km"), _fact(f"Est. range ({new.label})", r["new"], "km"),
        _fact("Range change", r["abs"], "km"), _fact("Range change %", r["pct"], "%"),
        _fact(f"Consumption ({old.label})", c["old"], "kWh/100 km"), _fact(f"Consumption ({new.label})", c["new"], "kWh/100 km"),
        _fact("Avg driving speed change", d["avg_driving_speed_kmh"]["abs"], "km/h"),
        _fact("Avg ambient change (driving)", d["avg_driving_ambient_c"]["abs"], "°C", "measured"),
        _fact("Lowest tyre pressure (new period)", d["min_tire_pressure_kpa"]["new"], "kPa", "measured"),
    ]
    contributors = []
    if (d["avg_driving_ambient_c"]["abs"] or 0) <= -5:
        contributors.append(f"colder weather (≈{abs(d['avg_driving_ambient_c']['abs']):.0f} °C lower while driving, more heating)")
    if (d["avg_driving_speed_kmh"]["pct"] or 0) >= 10:
        contributors.append(f"faster average driving (+{d['avg_driving_speed_kmh']['pct']:.0f}% speed)")
    tp = analytics.repeated_dtcs(conn, new, 2, a.vehicle_id)
    tp = tp[tp["code"].str.contains("TPMS")] if len(tp) else tp
    if len(tp):
        contributors.append(f"low tyre pressure warnings ({int(tp['count'].iloc[0])}×)")
        for i in tp["ids"].iloc[0]:
            r_ = conn.execute("SELECT * FROM diagnostic_events WHERE id=?", (i,)).fetchone()
            a.evidence.append({"type": "dtc", "id": i, "ts": r_["ts"], "text": f"{r_['code']}: {r_['description']}",
                               "source_ref": r_["source_ref"], "kind": "measured"})
    direction = "decreased" if r["abs"] < 0 else "increased"
    a.driver = (f"Your estimated range {direction} from about {r['old']:.0f} km to {r['new']:.0f} km "
                f"({r['pct']:+.0f}%) because the car used more energy per kilometre "
                f"({c['old']:.1f} → {c['new']:.1f} kWh/100 km)." if r["abs"] < 0 else
                f"Your estimated range {direction} from about {r['old']:.0f} km to {r['new']:.0f} km ({r['pct']:+.0f}%).")
    if contributors and r["abs"] < 0:
        a.driver += " Likely contributors: " + "; ".join(contributors) + ". These are possible factors, not a confirmed cause."
        a.driver += " Tip: check and correct tyre pressures." if len(tp) else ""
    a.engineer = (f"est_range = usable_kWh / (kWh_per_100km/100). {old.label} [{old.s[:10]}..{old.e[:10]}): "
                  f"{c['old']} kWh/100km → {r['old']} km; {new.label} [{new.s[:10]}..{new.e[:10]}): {c['new']} kWh/100km → "
                  f"{r['new']} km. Δrange {r['abs']} km ({r['pct']}%), Δconsumption {c['pct']}%. "
                  f"Δv_avg {d['avg_driving_speed_kmh']['abs']} km/h, ΔT_amb {d['avg_driving_ambient_c']['abs']} °C, "
                  f"min tyre pressure {d['min_tire_pressure_kpa']['old']}→{d['min_tire_pressure_kpa']['new']} kPa. "
                  "Contributors are correlational; no attribution model is applied.")
    for p, s in (("old", old), ("new", new)):
        st = cmp[p]
        a.evidence.append({"type": "aggregate", "id": None, "ts": st["period"]["start"],
                           "text": f"{s.label}: {st['distance_km']} km, {st['energy_kwh']} kWh over {st['trips']} trips "
                                   f"({st['driving_samples']} driving samples, coverage {st['coverage_pct']}%)",
                           "source_ref": f"{st['source_refs'][0]} … {st['source_refs'][1]}" if st["source_refs"] else None,
                           "kind": "calculated"})
        if st["coverage_pct"] < 98:
            a.limitations.append(f"{s.label}: telemetry coverage {st['coverage_pct']}% (some samples missing).")
    dc = analytics.daily_consumption(conn, old.s, new.e, a.vehicle_id)
    a.chart = {"type": "bar", "x": dc["date"].dt.strftime("%Y-%m-%d").tolist(), "y": dc["kwh_per_100km"].round(2).tolist(),
               "title": "Daily consumption (kWh/100 km)", "split": new.s[:10]}
    a.limitations.append("Range is an estimate from measured consumption and nominal usable energy; it is not a measured value.")


def _h_before(conn, q, a: Answer):
    ql = q.lower()
    sub = ("battery" if "battery" in ql or "pack" in ql else "thermal" if re.search(r"motor|engine|coolant|thermal", ql)
           else "tyres" if re.search(r"tyre|tire", ql) else None)
    d = db.read_dtcs(conn, vehicle_id=a.vehicle_id)
    d = d[d["reported_level"] == "warning"]
    if sub:
        d = d[d["subsystem"] == sub]
    if "temperature" in ql or "temp" in ql:
        d = d[d["description"].str.contains("temperature", case=False)] if len(d[d["description"].str.contains("temperature", case=False)]) else d
    if d.empty:
        a.supported = False
        a.driver = a.engineer = "I couldn't find a matching warning in the recorded diagnostic events."
        a.limitations.append("No diagnostic event matches the question; nothing was inferred.")
        return
    ev = d.iloc[-1]
    _investigation_answer(conn, f"dtc:{int(ev['id'])}", a)


def _investigation_answer(conn, key, a: Answer, focus: str = "summary"):
    inv = detective.investigate(conn, key)
    e = inv["event"]
    a.periods = [inv["window"]]
    a.facts.append(_fact("Event", e["title"], "", "measured"))
    a.facts.append(_fact("Event time", e["ts"], "", "measured"))
    a.evidence.append({"type": e["kind"], "id": key, "ts": e["ts"], "text": e["title"], "source_ref": None, "kind": "measured"})
    for t in inv["timeline"]:
        a.evidence.append({"type": t["type"], "id": t["record_id"], "ts": t["ts"], "text": t["text"],
                           "source_ref": None, "kind": t["source"]})
    hyps = inv["possible_explanations"]
    tl_before = [t for t in inv["timeline"] if t["ts"] < e["ts"] and t["type"] in ("state", "dtc")]
    seq = "; ".join(f"{t['ts'][11:16]} {t['text']}" for t in tl_before[-4:])
    a.driver = f"Before {e['title'].split(' — ')[-1].replace(' (synthetic)', '')} at {e['ts'][11:16]} on {e['ts'][:10]}: {seq or 'no recorded activity'}."
    if focus in ("summary", "why") and hyps:
        a.driver += " Possible reasons: " + " ".join(h["text"] for h in hyps[:2])
    if focus == "after":
        aft = [c for c in inv["comparison"] if c["channel"] == inv["focus_channel"]]
        if aft:
            a.driver = f"After the event, {aft[0]['channel']} averaged {aft[0]['after_mean']} (max {aft[0]['after_max']})."
    a.engineer = (f"Window {inv['window']['start'][:16]}–{inv['window']['end'][:16]}, focus={inv['focus_channel']}. CONFIRMED: "
                  + " ".join(inv["confirmed"]) + " POSSIBLE: " + (" | ".join(f"[{h['support']}] {h['text']}" for h in hyps) or "none"))
    a.limitations += inv["limitations"]
    tel = inv["telemetry"]
    ch = inv["focus_channel"] or "battery_temp_c"
    tv = tel[tel["valid"] == 1]
    a.chart = {"type": "line", "x": tv["ts"].dt.strftime("%Y-%m-%d %H:%M").tolist(), "y": tv[ch].tolist(),
               "title": f"{ch} around event", "marker": e["ts"][:16].replace("T", " ")}
    if e["subsystem"] in ("battery", "thermal"):
        a.safety.append("Temperature warning: if a warning is active, reduce load and stop safely when possible.")


def _h_unusual(conn, q, a: Answer):
    per, _ = parse_period(q, conn, 1)
    a.periods = [per.to_dict()]
    al = db.read_alerts(conn, per.s, per.e, a.vehicle_id)
    dt = db.read_dtcs(conn, per.s, per.e, a.vehicle_id)
    if al.empty and dt.empty:
        a.driver = a.engineer = f"No unusual events were detected for {per.label} ({per.s[:10]})."
        st = analytics.period_stats(conn, per, vehicle_id=a.vehicle_id)
        if st.samples == 0:
            a.supported = False
            a.limitations.append("No telemetry exists for that period — absence of alerts is not evidence of normal operation.")
        return
    items = []
    for _, r in al.iterrows():
        items.append(f"[{r['severity']}] {r['message']}")
        a.evidence.append({"type": "alert", "id": int(r["id"]), "ts": r["ts"].isoformat(), "text": r["message"],
                           "source_ref": r["rule_id"], "kind": "calculated"})
    for _, r in dt.iterrows():
        a.evidence.append({"type": "dtc", "id": int(r["id"]), "ts": r["ts"].isoformat(),
                           "text": f"{r['code']}: {r['description']}", "source_ref": r["source_ref"], "kind": "measured"})
    a.facts.append(_fact("Alerts", len(al), "", "calculated"))
    a.facts.append(_fact("Diagnostic events", len(dt), "", "measured"))
    a.driver = f"{per.label.capitalize()} ({per.s[:10]}) had {len(al)} alert(s) and {len(dt)} diagnostic event(s). " + " ".join(
        f"{r['message']}" for _, r in al.iterrows())
    if len(dt):
        a.driver += " Codes logged: " + ", ".join(
            f"{r['code']} at {r['ts']:%H:%M} ({r['description'].replace(' (synthetic)', '').lower()})" for _, r in dt.iterrows()) + "."
    a.engineer = f"Period [{per.s}, {per.e}): alerts={len(al)}, DTCs={len(dt)}. " + " | ".join(
        f"{r['ts']:%H:%M} {r['rule_id']} sev={r['severity']} obs={r['observed_value']} thr={r['threshold']}" for _, r in al.iterrows())
    if (al["severity"] == "High").any():
        a.safety.append("At least one High-severity alert occurred in this period.")


def _h_compare(conn, q, a: Answer):
    ql = q.lower()
    new, explicit = parse_period(q, conn, 7)
    if "last week" in ql and ("this week" in ql or "compare" in ql) or not explicit or "this week" in ql:
        new = analytics.last_n_days(conn, 7, 0, "this week (last 7 days)")
    n = (new.end - new.start).days
    old = Period(new.start - pd.Timedelta(days=n), new.start, "last week" if n == 7 else f"previous {n} days")
    cmp = analytics.compare_periods(conn, old, new, a.vehicle_id)
    a.periods = [old.to_dict(), new.to_dict()]
    d = cmp["diff"]
    c, e, dist = d["consumption_kwh_per_100km"], d["energy_kwh"], d["distance_km"]
    a.facts += [_fact(f"Energy used ({old.label})", e["old"], "kWh"), _fact(f"Energy used ({new.label})", e["new"], "kWh"),
                _fact(f"Distance ({old.label})", dist["old"], "km"), _fact(f"Distance ({new.label})", dist["new"], "km"),
                _fact(f"Consumption ({old.label})", c["old"], "kWh/100 km"), _fact(f"Consumption ({new.label})", c["new"], "kWh/100 km"),
                _fact("Consumption change", c["pct"], "%")]
    a.driver = (f"{new.label.capitalize()} you used {e['new']:.0f} kWh over {dist['new']:.0f} km "
                f"({c['new']:.1f} kWh/100 km) vs {e['old']:.0f} kWh over {dist['old']:.0f} km "
                f"({c['old']:.1f} kWh/100 km) {old.label} — {abs(c['pct']):.0f}% {'more' if c['pct'] > 0 else 'less'} energy per km.")
    a.engineer = (f"E_total {e['old']}→{e['new']} kWh ({e['pct']}%), d {dist['old']}→{dist['new']} km, "
                  f"specific consumption {c['old']}→{c['new']} kWh/100km (Δ{c['pct']}%). Periods "
                  f"[{old.s[:10]},{old.e[:10]}) vs [{new.s[:10]},{new.e[:10]}).")
    dc = analytics.daily_consumption(conn, old.s, new.e, a.vehicle_id)
    a.chart = {"type": "bar", "x": dc["date"].dt.strftime("%Y-%m-%d").tolist(), "y": dc["kwh_per_100km"].round(2).tolist(),
               "title": "Daily consumption (kWh/100 km)", "split": new.s[:10]}
    for p in ("old", "new"):
        st = cmp[p]
        a.evidence.append({"type": "aggregate", "id": None, "ts": st["period"]["start"],
                           "text": f"{st['period']['label']}: {st['driving_samples']} driving samples, {st['trips']} trips",
                           "source_ref": f"{st['source_refs'][0]} … {st['source_refs'][1]}", "kind": "calculated"})


def _h_repeated(conn, q, a: Answer):
    per, explicit = parse_period(q, conn, 21)
    if not explicit:
        lo, _ = db.data_bounds(conn, a.vehicle_id)
        per = Period(lo.normalize(), analytics.reference_now(conn), "entire dataset")
    a.periods = [per.to_dict()]
    rep = analytics.repeated_dtcs(conn, per, 2, a.vehicle_id)
    if rep.empty:
        a.driver = a.engineer = f"No diagnostic code occurred more than once in {per.label}."
        return
    for _, r in rep.iterrows():
        a.facts.append(_fact(f"{r['code']} occurrences", int(r["count"]), "", "measured"))
        for i in r["ids"]:
            rr = conn.execute("SELECT * FROM diagnostic_events WHERE id=?", (i,)).fetchone()
            a.evidence.append({"type": "dtc", "id": i, "ts": rr["ts"], "text": f"{rr['code']}: {rr['description']}",
                               "source_ref": rr["source_ref"], "kind": "measured"})
    a.driver = "Repeated warnings: " + "; ".join(
        f"{r['description'].replace(' (synthetic)', '').lower()} came up {r['count']} times "
        f"({r['first']:%b %d}–{r['last']:%b %d})" for _, r in rep.iterrows()) + "."
    if rep["code"].str.contains("TPMS").any():
        a.driver += " Check and inflate your tyres to the recommended pressure."
    a.engineer = " | ".join(f"{r['code']} [{r['subsystem']}] n={r['count']} first={r['first']:%Y-%m-%d %H:%M} "
                            f"last={r['last']:%Y-%m-%d %H:%M} ids={r['ids']}" for _, r in rep.iterrows())
    a.limitations.append("SIM-* codes are synthetic demonstration codes, not OEM/OBD-II codes.")


def _h_metric(conn, q, a: Answer):
    metric = find_metric(q)
    ql = q.lower()
    agg = "max" if re.search(r"max|highest|peak|hottest|how hot|how warm|fastest", ql) else "min" if re.search(r"min|lowest|coldest", ql) else "mean"
    per, _ = parse_period(q, conn, 7)
    a.periods = [per.to_dict()]
    r = analytics.metric_stat(conn, metric, per, agg, a.vehicle_id)
    name, unit = analytics.METRICS[metric]
    if r["value"] is None:
        a.supported = False
        a.driver = a.engineer = f"There are no valid {name} readings for {per.label}."
        a.limitations.append("No valid samples in the requested period.")
        return
    word = {"mean": "average", "max": "highest", "min": "lowest"}[agg]
    a.facts.append(_fact(f"{word.capitalize()} {name} ({per.label})", r["value"], unit, "calculated"))
    a.driver = f"The {word} {name} for {per.label} was {r['value']} {unit}" + (
        f", at {r['at_ts'][:16].replace('T', ' ')}." if r["at_ts"] else ".")
    a.engineer = (f"{agg}({metric}) over [{per.s}, {per.e}) = {r['value']} {unit}; n={r['n']} valid samples, "
                  f"{r['excluded_invalid']} invalid excluded" + (f"; record id {r['record_id']} ({r['source_ref']})" if r["record_id"] else ""))
    if r["record_id"]:
        a.evidence.append({"type": "telemetry", "id": r["record_id"], "ts": r["at_ts"], "text": f"{metric}={r['value']}",
                           "source_ref": r["source_ref"], "kind": "measured"})
    if r["excluded_invalid"]:
        a.limitations.append(f"{r['excluded_invalid']} invalid reading(s) excluded from the calculation.")


def _h_distance(conn, q, a: Answer):
    per, _ = parse_period(q, conn, 7)
    st = analytics.period_stats(conn, per, vehicle_id=a.vehicle_id)
    a.periods = [per.to_dict()]
    a.facts += [_fact(f"Distance ({per.label})", st.distance_km, "km"), _fact("Trips", st.trips, "")]
    a.driver = f"You drove {st.distance_km:.0f} km over {st.trips} trips in {per.label}."
    a.engineer = f"Σdistance_km over [{per.s}, {per.e}) = {st.distance_km} km, trips={st.trips}, driving samples={st.driving_samples}."


def _h_alerts(conn, q, a: Answer):
    per, explicit = parse_period(q, conn, 7)
    al = db.read_alerts(conn, per.s, per.e, a.vehicle_id)
    a.periods = [per.to_dict()]
    if al.empty:
        a.driver = a.engineer = f"No alerts in {per.label}."
        return
    cnt = al["severity"].value_counts().to_dict()
    unack = al[al["acknowledged"] == 0]
    a.facts += [_fact(f"{k} alerts", v, "", "calculated") for k, v in cnt.items()]
    a.driver = f"{len(al)} alert(s) in {per.label} ({len(unack)} not yet acknowledged). Most important: " + "; ".join(
        al.sort_values("severity", key=lambda s: s.map(config.SEVERITY_RANK), ascending=False)["message"].head(3))
    a.engineer = " | ".join(f"#{r['id']} {r['ts']:%m-%d %H:%M} {r['rule_id']} {r['severity']}" for _, r in al.iterrows())
    for _, r in al.iterrows():
        a.evidence.append({"type": "alert", "id": int(r["id"]), "ts": r["ts"].isoformat(), "text": r["message"],
                           "source_ref": r["rule_id"], "kind": "calculated"})


def _h_adas(conn, q, a: Answer):
    per, _ = parse_period(q, conn, 7)
    a.periods = [per.to_dict()]
    ev = db.read_adas(conn, per.s, per.e, a.vehicle_id)
    a.structured_query["filters"] = {"table": "adas_events", "period": per.to_dict(), "event": ["disengagement", "camera_blocked"]}
    if not conn.execute("SELECT 1 FROM adas_events WHERE vehicle_id = ? LIMIT 1", (a.vehicle_id,)).fetchone():
        a.supported = False
        a.driver = a.engineer = "This vehicle has no ADAS data recorded, so I can't list ADAS events."
        a.limitations.append("No ADAS event stream exists for this vehicle.")
        return
    if ev.empty:
        a.driver = a.engineer = f"No ADAS disengagements or camera events were recorded {per.label} ({per.s[:10]}–{per.e[:10]})."
        return
    dis = ev[ev["event"] == "disengagement"]
    cam = ev[ev["event"] == "camera_blocked"]
    a.table = ev[["id", "ts", "feature", "event", "reason", "speed_kmh", "duration_s", "source_ref"]].astype(str).to_dict("records")
    for r in ev.itertuples():
        a.evidence.append({"type": "adas", "id": int(r.id), "ts": r.ts.isoformat(), "text": f"{r.feature} {r.event} ({r.reason})",
                           "source_ref": r.source_ref, "kind": "measured"})
    by = dis.groupby("reason").size().sort_values(ascending=False)
    a.facts += [_fact("ADAS disengagements", len(dis), "", "measured"), _fact("Camera blockage events", len(cam), "", "measured")]
    a.facts += [_fact(f"Disengagements: {k}", int(v), "", "measured") for k, v in by.items()]
    nice = {"driver_override": "you took over", "lane_markings_lost": "lane markings were not visible",
            "sensor_blocked": "the camera was blocked", "system_limit": "the system reached its limits"}
    a.driver = (f"{per.label.capitalize()}: driver-assist disengaged {len(dis)} time(s)"
                + (" — mostly because " + nice.get(by.index[0], by.index[0]) if len(by) else "")
                + (f"; the front camera was blocked {len(cam)} time(s), so cleaning it may help." if len(cam) else "."))
    a.engineer = (f"adas_events [{per.s},{per.e}): disengagements={len(dis)} by reason {by.to_dict()}, by feature "
                  f"{dis.groupby('feature').size().to_dict()}; camera_blocked={len(cam)}; mean speed at disengagement "
                  f"{dis['speed_kmh'].mean():.0f} km/h.")
    a.limitations.append("ADAS data is synthetic; reasons are as reported by the (simulated) ADAS ECU.")


def _h_largest_drop(conn, q, a: Answer):
    per, explicit = parse_period(q, conn, 56)
    if not explicit:
        lo, _ = db.data_bounds(conn, a.vehicle_id)
        per = Period(lo.normalize(), analytics.reference_now(conn), "entire history")
    dc = analytics.daily_consumption(conn, per.s, per.e, a.vehicle_id)
    dc = dc[dc["distance_km"] >= 5].reset_index(drop=True)
    if len(dc) < 8:
        a.supported = False
        a.driver = a.engineer = "Not enough driving days to find the largest efficiency drop."
        return
    dc["km_per_kwh"] = 100 / dc["kwh_per_100km"]
    dc["baseline"] = dc["km_per_kwh"].shift(1).rolling(7, min_periods=3).mean()
    dc["drop_pct"] = (dc["km_per_kwh"] - dc["baseline"]) / dc["baseline"] * 100
    i = dc["drop_pct"].idxmin()
    r = dc.loc[i]
    a.periods = [per.to_dict()]
    a.structured_query["filters"] = {"metric": "km_per_kwh (daily)", "baseline": "previous 7 driving days (rolling mean)",
                                     "min_daily_km": 5}
    a.facts += [_fact("Date of largest drop", r["date"].strftime("%Y-%m-%d"), "", "calculated"),
                _fact("Efficiency that day", round(r["km_per_kwh"], 2), "km/kWh"),
                _fact("Baseline (previous 7 days)", round(r["baseline"], 2), "km/kWh"),
                _fact("Drop", round(r["drop_pct"], 1), "%"),
                _fact("Avg speed that day", round(r["avg_speed"], 1), "km/h", "measured"),
                _fact("Avg outside temp that day", round(r["avg_ambient"], 1), "°C", "measured")]
    a.evidence.append({"type": "aggregate", "id": None, "ts": r["date"].isoformat(),
                       "text": f"{r['distance_km']:.0f} km, {r['energy_kwh']:.1f} kWh (telemetry ids {int(r['first_id'])}–{int(r['last_id'])})",
                       "source_ref": "telemetry", "kind": "calculated"})
    note = "This is an electric vehicle, so 'fuel efficiency' is measured as energy efficiency (km per kWh). " if "fuel" in q.lower() else ""
    a.driver = (note + f"The largest efficiency drop was on {r['date']:%A %b %d}: {r['km_per_kwh']:.2f} km/kWh, "
                f"{abs(r['drop_pct']):.0f}% below the previous week's {r['baseline']:.2f} km/kWh. That day averaged "
                f"{r['avg_speed']:.0f} km/h at {r['avg_ambient']:.0f} °C outside.")
    a.engineer = (f"argmin over days of (eff_d − mean(eff_{{d−7..d−1}}))/mean · eff = km/kWh. Result {r['date']:%Y-%m-%d}: "
                  f"{r['km_per_kwh']:.3f} vs {r['baseline']:.3f} ({r['drop_pct']:.1f}%), v̄={r['avg_speed']:.1f} km/h, "
                  f"T̄amb={r['avg_ambient']:.1f} °C, d={r['distance_km']:.1f} km.")
    a.chart = {"type": "line", "x": dc["date"].dt.strftime("%Y-%m-%d").tolist(), "y": dc["km_per_kwh"].round(3).tolist(),
               "title": "Daily efficiency (km/kWh)", "marker": r["date"].strftime("%Y-%m-%d")}


CITY_MAX, HWY_MIN = 60.0, 80.0


def _h_city_highway(conn, q, a: Answer):
    per, _ = parse_period(q, conn, 28)
    tel = db.read_telemetry(conn, per.s, per.e, a.vehicle_id, include_invalid=False).drop_duplicates("ts")
    drv = tel[(tel["state"] == "driving") & (tel["distance_km"] > 0)].copy()
    drv["segment"] = pd.cut(drv["speed_kmh"], [0, CITY_MAX, HWY_MIN, 1e9], labels=["city", "mixed", "highway"], right=False)
    g = drv.groupby("segment", observed=False).agg(samples=("id", "size"), distance_km=("distance_km", "sum"),
                                                   energy_kwh=("energy_kwh", "sum"), avg_speed=("speed_kmh", "mean"))
    g["kwh_per_100km"] = g["energy_kwh"] / g["distance_km"] * 100
    a.periods = [per.to_dict()]
    a.structured_query["filters"] = {"segment_rule": f"city <{CITY_MAX:.0f} km/h ≤ mixed <{HWY_MIN:.0f} km/h ≤ highway",
                                     "unit": "10-minute driving samples"}
    a.table = g.reset_index().round(2).to_dict("records")
    c, h = g.loc["city"], g.loc["highway"]
    if not c["samples"] or not h["samples"]:
        a.supported = False
        a.driver = a.engineer = f"Not enough {'highway' if not h['samples'] else 'city'} driving in {per.label} to compare."
        a.limitations.append("Comparison needs both segment types in the period.")
        return
    diff = (h["kwh_per_100km"] - c["kwh_per_100km"]) / c["kwh_per_100km"] * 100
    a.facts += [_fact("City consumption", round(c["kwh_per_100km"], 2), "kWh/100 km"),
                _fact("Highway consumption", round(h["kwh_per_100km"], 2), "kWh/100 km"),
                _fact("Highway vs city", round(diff, 1), "%"), _fact("City distance", round(c["distance_km"], 1), "km"),
                _fact("Highway distance", round(h["distance_km"], 1), "km")]
    a.driver = (f"In {per.label}, highway driving used {h['kwh_per_100km']:.1f} kWh/100 km versus {c['kwh_per_100km']:.1f} in the "
                f"city — {abs(diff):.0f}% {'more' if diff > 0 else 'less'}. Higher speeds raise air resistance, so EVs are usually "
                "more efficient in town.")
    a.engineer = (f"Segmented by 10-min mean speed: city n={int(c['samples'])} d={c['distance_km']:.1f} km "
                  f"{c['kwh_per_100km']:.2f}; highway n={int(h['samples'])} d={h['distance_km']:.1f} km {h['kwh_per_100km']:.2f} "
                  f"kWh/100km (Δ{diff:+.1f}%). Mixed band {CITY_MAX:.0f}–{HWY_MIN:.0f} km/h excluded from the comparison.")
    a.limitations.append("Segments are inferred from average speed per 10-minute sample (no map/road-type data).")


CODE_PLAIN = {
    "SIM-BMS-T01": "The battery pack got hotter than the system's limit. This commonly happens during high-power fast charging "
                   "or in hot weather. Let it cool and avoid back-to-back fast charges; if it keeps happening, have the cooling checked.",
    "SIM-TPMS-L01": "At least one tyre is below its expected pressure. Low pressure increases energy use and tyre wear — check and inflate.",
    "SIM-THM-P02": "The system detected low coolant flow to the motor. Reduced cooling can make the motor run hot — book an inspection.",
    "SIM-THM-T05": "The motor became hotter than expected. Reduce load; if it repeats, have the cooling system inspected.",
    "SIM-INF-U03": "The infotainment unit briefly lost contact with another module. This is usually not driving-related.",
    "SIM-SNS-M09": "A temperature sensor stopped reporting changing values, so its readings may be unreliable.",
}


def _dtc_evidence(a: Answer, r):
    a.evidence.append({"type": "dtc", "id": int(r["id"]), "ts": pd.Timestamp(r["ts"]).isoformat(),
                       "text": f"{r['code']}: {r['description']}", "source_ref": r["source_ref"], "kind": "measured"})


def _h_fleet_temp(conn, q, a: Answer):
    rows = []
    for vid in a.allowed_vehicles or [a.vehicle_id]:
        al = db.read_alerts(conn, vehicle_id=vid)
        al = al[al["rule_id"].isin(["R-BATT-TEMP", "R-MOTOR-ROC"])]
        rows.append({"vehicle_id": vid, "temperature_alerts": len(al), "repeated": len(al) >= 2,
                     "first": al["ts"].min().isoformat() if len(al) else None, "last": al["ts"].max().isoformat() if len(al) else None,
                     "max_severity": max(al["severity"], key=config.SEVERITY_RANK.get) if len(al) else None})
        for _, r in al.iterrows():
            a.evidence.append({"type": "alert", "id": int(r["id"]), "ts": r["ts"].isoformat(),
                               "text": f"{vid}: {r['message']}", "source_ref": r["rule_version"], "kind": "calculated"})
    rep = [r for r in rows if r["repeated"]]
    a.table = rows
    a.structured_query["filters"] = {"rule_id": ["R-BATT-TEMP", "R-MOTOR-ROC"], "min_count": 2,
                                     "vehicle_ids": a.allowed_vehicles}
    a.facts += [_fact(f"{r['vehicle_id']} temperature alerts", r["temperature_alerts"], "", "calculated") for r in rows]
    a.driver = (("Repeated temperature anomalies: " + ", ".join(f"{r['vehicle_id']} ({r['temperature_alerts']}×)" for r in rep))
                if rep else "No authorised vehicle had repeated temperature anomalies.")
    a.engineer = a.driver + f" Scope: {len(rows)} authorised vehicle(s); rules R-BATT-TEMP, R-MOTOR-ROC; repeated = ≥2 episodes."


def _h_dtc_energy(conn, q, a: Answer):
    al = db.read_alerts(conn, vehicle_id=a.vehicle_id)
    hi = al[al["rule_id"] == "R-CONS-DEV"]
    dt = db.read_dtcs(conn, vehicle_id=a.vehicle_id)
    a.structured_query["filters"] = {"alert.rule_id": "R-CONS-DEV", "join": "diagnostic_events on same calendar day"}
    rows = []
    for _, h in hi.iterrows():
        d0 = h["ts"].normalize()
        same = dt[(dt["ts"] >= d0) & (dt["ts"] < d0 + pd.Timedelta(days=1))]
        for _, r in same.iterrows():
            rows.append({"date": d0.strftime("%Y-%m-%d"), "kwh_per_100km": h["observed_value"], "baseline": h["baseline"],
                         "code": r["code"], "dtc_ts": r["ts"].isoformat(), "dtc_id": int(r["id"])})
            _dtc_evidence(a, r)
        if same.empty:
            rows.append({"date": d0.strftime("%Y-%m-%d"), "kwh_per_100km": h["observed_value"], "baseline": h["baseline"],
                         "code": None, "dtc_ts": None, "dtc_id": None})
    a.table = rows
    with_codes = [r for r in rows if r["code"]]
    a.facts.append(_fact("High-consumption days", len(hi), "", "calculated"))
    a.facts.append(_fact("Diagnostic events on those days", len(with_codes), "", "measured"))
    a.driver = (f"{len(hi)} day(s) had unusually high energy use; {len(with_codes)} diagnostic event(s) occurred on those days: "
                + ", ".join(f"{r['code']} ({r['date']})" for r in with_codes) + ".") if with_codes else \
        f"{len(hi)} high-consumption day(s) found, with no diagnostic events on the same days."
    a.engineer = a.driver + " Association is same-day co-occurrence only — not evidence of causation."
    a.limitations.append("Co-occurrence on the same day does not establish a causal link.")


def _h_before_after(conn, q, a: Answer):
    d = db.read_dtcs(conn, vehicle_id=a.vehicle_id)
    d = d[d["reported_level"] == "warning"]
    if d.empty:
        a.supported = False
        a.driver = a.engineer = "No warning-level diagnostic events found for this vehicle."
        return
    m = re.search(r"(\d+)\s*min", q.lower())
    w = int(m.group(1)) if m else 30
    ev = d.iloc[-1]
    inv = detective.investigate(conn, f"dtc:{int(ev['id'])}", w, w)
    a.table = inv["comparison"]
    a.structured_query["filters"] = {"event": f"dtc:{int(ev['id'])}", "window_before_min": w, "window_after_min": w}
    _dtc_evidence(a, ev)
    foc = next((c for c in inv["comparison"] if c["channel"] == inv["focus_channel"]), inv["comparison"][0])
    a.facts += [_fact(f"{foc['channel']} mean before", foc["before_mean"]), _fact(f"{foc['channel']} mean after", foc["after_mean"])]
    a.driver = (f"Around {ev['code']} at {ev['ts']:%Y-%m-%d %H:%M}: {foc['channel']} averaged {foc['before_mean']} in the "
                f"{w} min before and {foc['after_mean']} in the {w} min after.")
    a.engineer = a.driver + " Full channel comparison in table (valid samples only)."
    a.limitations += inv["limitations"]


def _h_data_quality(conn, q, a: Answer):
    from . import dataquality as dq
    r = dq.report(conn, a.vehicle_id)
    a.structured_query["filters"] = {"checks": ["missing", "gaps", "duplicates", "implausible", "unit", "flatline", "stale"]}
    a.facts += [_fact("Completeness", r["completeness_pct"], "%"), _fact("Logging gaps", len(r["gaps"]), ""),
                _fact("Duplicate rows", len(r["duplicates"]), "", "measured"), _fact("Implausible rows", len(r["implausible"]), "", "measured"),
                _fact("Unit suspects", len(r["unit_suspects"]), ""), _fact("Flat-lined segments", len(r["flatlines"]), ""),
                _fact("Data age", r["staleness"]["age_hours"], "h")]
    a.facts += [_fact(f"Missing {k}", v, "", "measured") for k, v in r["missing_values"].items() if v]
    a.table = [{"check": f["label"], "value": f["value"]} for f in a.facts]
    for _, row in r["implausible"].head(20).iterrows():
        a.evidence.append({"type": "telemetry", "id": int(row["id"]), "ts": row["ts"].isoformat(), "text": row["validity_note"],
                           "source_ref": row["source_ref"], "kind": "measured"})
    a.driver = (f"Data quality for {a.vehicle_id}: {r['issue_count']} issue type(s) found. "
                f"{r['completeness_pct']}% complete; data is {'STALE (' + str(r['staleness']['age_hours']) + ' h old)' if r['staleness']['stale'] else 'current'}.")
    a.engineer = a.driver + f" gaps={len(r['gaps'])}, dup_rows={len(r['duplicates'])}, implausible={len(r['implausible'])}, " \
        f"unit_suspects={len(r['unit_suspects'])}, flatlines={len(r['flatlines'])}. {r['note']}"


def _h_latest_dtcs(conn, q, a: Answer):
    d = db.read_dtcs(conn, vehicle_id=a.vehicle_id).tail(5).iloc[::-1]
    a.structured_query["filters"] = {"table": "diagnostic_events", "order": "ts desc", "limit": 5}
    if d.empty:
        a.driver = a.engineer = "No diagnostic events recorded for this vehicle."
        return
    for _, r in d.iterrows():
        _dtc_evidence(a, r)
    a.table = d[["id", "ts", "code", "subsystem", "description", "reported_level"]].astype(str).to_dict("records")
    a.facts.append(_fact("Diagnostic events listed", len(d), "", "measured"))
    a.driver = "Latest diagnostic events: " + "; ".join(
        f"{r['ts']:%b %d %H:%M} — {CODE_PLAIN.get(r['code'], r['description']).split('.')[0]}" for _, r in d.iterrows()) + "."
    a.engineer = " | ".join(f"#{r['id']} {r['ts']:%Y-%m-%d %H:%M} {r['code']} [{r['subsystem']}/{r['reported_level']}]"
                            for _, r in d.iterrows())


def _h_explain_dtc(conn, q, a: Answer, key: str | None = None):
    d = db.read_dtcs(conn, vehicle_id=a.vehicle_id)
    m = re.search(r"SIM-[A-Z]{3}-[A-Z0-9]{3}", q.upper())
    if key and key.startswith("dtc:"):
        d = d[d["id"] == int(key.split(":")[1])]
    elif m:
        d = d[d["code"] == m.group(0)]
    else:
        d = d[d["reported_level"] == "warning"]
    if d.empty:
        a.supported = False
        a.driver = a.engineer = "I couldn't find that diagnostic warning for this vehicle."
        return
    r = d.iloc[-1]
    _dtc_evidence(a, r)
    n = int((db.read_dtcs(conn, vehicle_id=a.vehicle_id)["code"] == r["code"]).sum())
    a.facts += [_fact("Code", r["code"], "", "measured"), _fact("Occurrences", n, "", "measured")]
    a.driver = f"{r['code']} ({r['ts']:%b %d, %H:%M}): " + CODE_PLAIN.get(r["code"], r["description"]) + \
        (f" It has occurred {n} times." if n > 1 else "")
    a.engineer = f"{r['code']} subsystem={r['subsystem']} level={r['reported_level']} ts={r['ts']} id={r['id']} n={n} — {r['description']}"
    a.limitations.append("SIM-* codes are synthetic. The explanation describes the code's meaning, not a confirmed diagnosis.")


def _h_consumption_normal(conn, q, a: Answer):
    new = analytics.last_n_days(conn, 7)
    base = Period(new.start - pd.Timedelta(days=14), new.start, "previous 14 days")
    cmp = analytics.compare_periods(conn, base, new, a.vehicle_id)
    c = cmp["diff"]["consumption_kwh_per_100km"]
    if c["old"] is None or c["new"] is None:
        a.supported = False
        a.driver = a.engineer = "Not enough driving data to compare consumption with normal."
        return
    higher = c["pct"] > 5
    a.facts += [_fact("Consumption (last 7 days)", c["new"], "kWh/100 km"), _fact("Normal (previous 14 days)", c["old"], "kWh/100 km"),
                _fact("Difference", c["pct"], "%")]
    a.structured_query["filters"] = {"current": new.to_dict(), "baseline": base.to_dict(), "normal_band_pct": 5}
    a.driver = (f"Yes — over the last 7 days you used {c['new']:.1f} kWh/100 km, {c['pct']:.0f}% more than your normal "
                f"{c['old']:.1f}." if higher else
                f"No — {c['new']:.1f} kWh/100 km is within 5% of your normal {c['old']:.1f}.")
    a.engineer = f"7d {c['new']} vs 14d baseline {c['old']} kWh/100km (Δ{c['pct']}%); 'normal' band ±5% (demo convention)."
    dc = analytics.daily_consumption(conn, base.s, new.e, a.vehicle_id)
    a.chart = {"type": "bar", "x": dc["date"].dt.strftime("%Y-%m-%d").tolist(), "y": dc["kwh_per_100km"].round(2).tolist(),
               "title": "Daily consumption (kWh/100 km)", "split": new.s[:10]}


HANDLERS = {"adas_events": _h_adas, "largest_drop": _h_largest_drop, "city_highway": _h_city_highway,
            "fleet_temp": _h_fleet_temp, "dtc_energy": _h_dtc_energy, "before_after": _h_before_after,
            "data_quality": _h_data_quality, "latest_dtcs": _h_latest_dtcs, "explain_dtc": _h_explain_dtc,
            "consumption_normal": _h_consumption_normal,
            "range_change": _h_range, "before_warning": _h_before, "unusual_events": _h_unusual,
            "compare_energy": _h_compare, "repeated_dtc": _h_repeated, "metric_stat": _h_metric,
            "distance": _h_distance, "alerts_summary": _h_alerts}

SUPPORTED_HELP = ("I can answer questions about range, energy consumption, ADAS disengagements, city vs highway efficiency, battery/motor temperature, speed, state of charge, "
                  "tyre pressure, diagnostic codes and alerts for the recorded period (2026-07-28 to 2026-09-21).")


def add_explanation(a: Answer) -> Answer:
    """Optional grounded rephrasing by the local model (seconds on edge CPUs; not on the <2 s critical path)."""
    if not (a.supported and a.facts):
        return a
    t = time.perf_counter()
    facts_txt = "\n".join(f"- {f['label']}: {f['value']} {f['unit']}" for f in a.facts) + "\n" + a.engineer
    text, status = llm.explain(facts_txt, "driver")
    a.explanation_source, a.ai_text = status, text
    a.structured_query["explain_ms"] = round((time.perf_counter() - t) * 1000, 1)
    return a


# Intents whose rule patterns require explicit domain keywords (high precision): when the rules fire on one of these
# and the model disagrees, the rule wins. (Choice informed by benchmarks/intent_eval.json — see docs for caveat.)
STRONG_RULE_INTENTS = {"unsupported", "adas_events", "largest_drop", "city_highway", "data_quality", "fleet_temp",
                       "dtc_energy", "before_after"}


def route(q: str, use_llm: bool = True) -> tuple[str, str, dict]:
    """Choose the handler. Rules are always computed; with a local model available the model's label is preferred
    (measured 95 % vs 68 % on benchmarks/intent_eval.json), except that 'unsupported' (no such data) is always enforced."""
    t = time.perf_counter()
    rule = classify(q)
    info = {"rules": rule, "llm": None, "llm_ms": None}
    if not use_llm or not llm.get_adapter().status().available:
        return rule, "rules", info
    li = llm.classify(q)
    info.update(llm=li, llm_ms=round((time.perf_counter() - t) * 1000, 1))
    if rule in STRONG_RULE_INTENTS:
        return rule, "rules" if li != rule else "rules+llm", info
    if config.ROUTER == "llm_first" and li and li in HANDLERS:
        return li, "llm" if li != rule else "rules+llm", info
    if rule == "unknown" and li and li in HANDLERS:
        return li, "llm", info
    return rule, "rules", info


def answer(conn, question: str, use_llm: bool = True, context_event: str | None = None,
           vehicle_id: str = config.VEHICLE_ID, allowed_vehicles: list[str] | None = None, explain: bool = False) -> Answer:
    """Authorisation is enforced by the caller (services.py); this function only reads `vehicle_id`
    and, for fleet questions, `allowed_vehicles`. `explain=True` adds the (slow) local-LLM rephrasing."""
    t = time.perf_counter()
    q = question.strip()[:500]
    intent, source, rinfo = route(q, use_llm)
    if context_event and intent == "explain_dtc":
        pass
    elif context_event and intent in ("unknown", "before_warning", "alerts_summary") or (
            context_event and re.search(r"\b(why|cause|after|peak|this event|it)\b", q.lower())):
        intent, source = "event_followup", "context"
    a = Answer(question=q, intent=intent, intent_source=source, vehicle_id=vehicle_id,
               allowed_vehicles=list(allowed_vehicles or [vehicle_id]))
    if intent == "unsupported":
        what = next(lbl for w, lbl in UNSUPPORTED.items() if lbl and re.search(rf"\b{w}", q.lower()))
        a.supported = False
        a.driver = a.engineer = f"The recorded data does not include {what} information, so I can't answer that."
        a.limitations.append(f"No {what} channel exists in the dataset. Available: " + ", ".join(
            v[0] for v in analytics.METRICS.values()) + ", diagnostic events.")
    elif intent == "event_followup":
        ql = q.lower()
        focus = "after" if "after" in ql else "why" if re.search(r"why|cause", ql) else "summary"
        _investigation_answer(conn, context_event, a, focus)
    elif intent in HANDLERS:
        try:
            if intent == "explain_dtc":
                _h_explain_dtc(conn, q, a, context_event)
            else:
                HANDLERS[intent](conn, q, a)
        except Exception as e:  # noqa: BLE001 — never fabricate; surface the limitation
            a.supported = False
            a.driver = a.engineer = "I couldn't compute an answer for that question."
            a.limitations.append(f"Internal error during calculation: {type(e).__name__}: {e}")
    else:
        a.supported = False
        a.driver = a.engineer = "I'm not sure what you're asking. " + SUPPORTED_HELP
        a.limitations.append("Question not recognised by the rule-based interpreter" +
                             ("" if use_llm and llm.get_adapter().status().available else " and no local LLM is available") + ".")
    a.structured_query = {"intent": intent, "router": rinfo, "vehicle_id": vehicle_id, "periods": a.periods, **a.structured_query}
    if explain and use_llm:
        add_explanation(a)
    a.latency_ms = round((time.perf_counter() - t) * 1000, 1)
    return a
