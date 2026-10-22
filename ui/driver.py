"""User Portal — Driver / Vehicle Owner. All data access goes through vehiclegpt.services."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from vehiclegpt import analytics, maintenance, reports, services as S, simulator
from ui.auth_pages import password_form
from ui.common import (SEVERITIES, badge, confirm_button, empty, fig_style, sev_badge, show_answer, status_sidebar, tile)

PAGES = ["Overview", "Assistant", "Health Score", "Early Warnings", "Efficiency", "What-If", "Event History",
         "Maintenance", "Reports", "Settings & Privacy"]
SUGGESTED = ["Why did my driving range decrease?", "What happened before the latest warning?",
             "Is my energy consumption higher than normal?", "Show my vehicle's unusual events this week.",
             "Explain this diagnostic warning in simple language."]


def render(conn, user):
    vdf = S.vehicle_list(conn, user)
    with st.sidebar:
        st.markdown(f"### 🚗 Hi, {user['display_name'].split()[0]}")
        st.caption("Driver / Vehicle Owner portal")
        if vdf.empty:
            vid = None
        else:
            labels = {r.vehicle_id: f"{r.nickname or r.name} · {r.vehicle_id}" for r in vdf.itertuples()}
            if st.session_state.get("drv_vehicle") not in labels:
                st.session_state.pop("drv_vehicle", None)  # discard tampered/stale selection
            vid = st.selectbox("Vehicle", list(labels), format_func=labels.get, key="drv_vehicle")
        page = st.radio("Navigate", PAGES, key="drv_page", label_visibility="collapsed")
        st.divider()
        status_sidebar()
    if vid is None and page != "Settings & Privacy":
        st.title("No vehicles yet")
        empty("No vehicle is registered to your account. Add one with its VIN and pairing code in Settings & Privacy.")
        return
    {"Overview": overview, "Assistant": assistant, "Health Score": health_page, "Early Warnings": warnings,
     "Efficiency": efficiency, "What-If": whatif, "Event History": history, "Maintenance": maint,
     "Reports": reports_page, "Settings & Privacy": settings}[page](conn, user, vid)


def overview(conn, user, vid):
    o = S.overview(conn, user, vid)
    v = o["vehicle"]
    st.title(v["nickname"] or v["name"])
    st.markdown(f'{v["vehicle_type"]} · {vid} · ' + badge("historical") + (badge("stale") if o["staleness"]["stale"] else "") +
                f' <span class="vg-muted">latest record {o["last_ts"]:%Y-%m-%d %H:%M} — not a live connection</span>',
                unsafe_allow_html=True)
    c = st.columns(5)
    tile(c[0], "Battery charge", f"{o['soc']:.0f}%" if o["soc"] is not None else "—", "measured")
    tile(c[1], "Est. range", f"{o['est_range_km']:.0f} km" if o["est_range_km"] else "—", "calculated · 7-day consumption")
    tile(c[2], "Consumption (7 d)", f"{o['week'].consumption_kwh_per_100km or '—'}", "kWh/100 km · calculated")
    last = o["last"]
    tile(c[3], "Battery temp", f"{last['battery_temp_c']:.1f} °C" if last is not None else "—",
         f"outside {last['ambient_temp_c']:.1f} °C" if last is not None else "")
    al = S.alerts(conn, user, vid)
    opn = al[(al["acknowledged"] == 0) & (al["severity"] != "Informational")]
    tile(c[4], "Open warnings", len(opn), f"{(opn['severity'] == 'High').sum()} high")
    if (opn["severity"] == "High").any():
        st.markdown('<div class="vg-safety">⚠️ You have an important warning. Open <b>Early Warnings</b> for details and next steps.</div>',
                    unsafe_allow_html=True)
    if o["staleness"]["stale"]:
        st.warning(f"The latest data is {o['staleness']['age_hours']:.0f} hours old. Readings may not reflect your vehicle now.")
    l, r = st.columns([3, 2])
    with l:
        t = o["telemetry_48h"]
        fig = go.Figure()
        fig.add_scatter(x=t["ts"], y=t["soc_pct"], name="Charge %", line=dict(color="#34d399"))
        fig.add_scatter(x=t["ts"], y=t["battery_temp_c"], name="Battery °C", line=dict(color="#fb923c"))
        fig.update_layout(title="Last 48 recorded hours")
        st.plotly_chart(fig_style(fig, 300), width="stretch")
    with r:
        st.markdown("#### Recent events")
        ev = S.events(conn, user, vid).head(6)
        if ev.empty:
            empty("No events recorded.")
        for e in ev.itertuples():
            st.markdown(f"<span class='vg-muted'>{e.ts:%b %d %H:%M}</span> · {e.label[:90]}", unsafe_allow_html=True)


def assistant(conn, user, vid):
    st.title("Vehicle Assistant")
    st.caption("Answers come from verified calculations on your vehicle's local records.")
    cols = st.columns(len(SUGGESTED))
    for i, q in enumerate(SUGGESTED):
        if cols[i].button(q, key=f"dsq{i}", width="stretch"):
            st.session_state.drv_q = q
    with st.form("drv_ask", clear_on_submit=True):
        q = st.text_input("Ask about your vehicle", max_chars=500)
        if st.form_submit_button("Ask", type="primary") and q.strip():
            st.session_state.drv_q = q
    if st.session_state.get("drv_q"):
        with st.spinner("Checking your vehicle records…"):
            a = S.ask(conn, user, vid, st.session_state.pop("drv_q"))
        st.session_state.setdefault("drv_hist", []).insert(0, a)
    hist = [a for a in st.session_state.get("drv_hist", []) if a.vehicle_id == vid]
    if not hist:
        empty("Pick a suggested question or type your own.")
    for i, a in enumerate(hist[:5]):
        st.markdown(f"#### 💬 {a.question}")
        show_answer(a, technical=False)
        if a.supported and a.facts and not a.ai_text and S.llm_available():
            if st.button("✨ AI summary (local model, takes a few seconds)", key=f"drv_ai{i}"):
                with st.spinner("Local model is writing a grounded summary…"):
                    S.explain_answer(conn, user, a)
                if not a.ai_text:
                    st.caption(f"AI summary unavailable ({a.explanation_source}); the verified answer above stands.")
                else:
                    st.rerun()


def health_page(conn, user, vid):
    st.title("Vehicle Health Score")
    h = S.health_score(conn, user, vid)
    col = {"Good": "#34d399", "Fair": "#facc15", "Needs attention": "#f87171"}[h["band"]]
    l, r = st.columns([1, 2])
    with l:
        st.markdown(f'<div class="vg-card"><div class="vg-score" style="color:{col}">{h["score"]}</div>'
                    f'<div>/100 · <b>{h["band"]}</b></div><div class="vg-muted">as of {h["as_of"][:10]} · data '
                    f'{h["completeness_pct"]}% complete · confidence {h["confidence"]}</div></div>', unsafe_allow_html=True)
        st.caption("Explainable indicator built from validated readings — not a manufacturer rating or certified diagnosis.")
    with r:
        tr = S.health_trend(conn, user, vid)
        fig = go.Figure(go.Scatter(x=tr["as_of"], y=tr["score"], mode="lines+markers", line=dict(color=col)))
        fig.update_layout(title="Score over the last 14 days", yaxis_range=[0, 100])
        st.plotly_chart(fig_style(fig, 250), width="stretch")
    st.markdown("#### What affects the score")
    if not h["factors"]:
        empty("No contributing factors in the last 7 days.")
    for f in h["factors"]:
        st.markdown(f"- **{f['factor']}** — {f['detail']}  →  `{f['points']:+g}` points")
    with st.expander("Limitations & method"):
        for l_ in h["limitations"]:
            st.markdown(f"- {l_}")
        st.code(h["method"])


def warnings(conn, user, vid):
    st.title("Early Warnings")
    al = S.alerts(conn, user, vid)
    if al.empty:
        empty("No warnings recorded for this vehicle.")
        return
    c1, c2 = st.columns(2)
    sev = c1.multiselect("Severity", SEVERITIES, default=["Low", "Medium", "High"])
    status = c2.radio("Show", ["Open", "Acknowledged", "All"], horizontal=True)
    f = al[al["severity"].isin(sev)]
    f = f[f["acknowledged"] == 0] if status == "Open" else f[f["acknowledged"] == 1] if status == "Acknowledged" else f
    f = f.sort_values("ts", ascending=False)
    if f.empty:
        empty("Nothing matches these filters.")
    for a in f.itertuples():
        with st.container(border=True):
            st.markdown(f"{sev_badge(a.severity)} <span class='vg-muted'>{a.ts:%a %b %d, %H:%M} · {a.subsystem}</span>",
                        unsafe_allow_html=True)
            st.markdown(f"**{a.message}**")
            st.markdown(f"👉 {a.next_step}")
            if a.severity in ("High", "Medium") and a.subsystem in ("battery", "thermal", "tyres"):
                st.caption("Recommendation: have a qualified technician inspect the vehicle. This app cannot confirm a fault.")
            with st.expander("Evidence"):
                ev = json.loads(a.evidence_json)
                ids = ev.get("record_ids") or []
                if ev.get("record_id_range"):
                    ids = list(range(ev["record_id_range"][0], ev["record_id_range"][1] + 1))
                if ids:
                    recs = S.records_by_id(conn, user, vid, "telemetry", ids)
                    st.dataframe(recs[["ts", "state", "battery_temp_c", "motor_temp_c", "speed_kmh", "ambient_temp_c"]],
                                 hide_index=True, width="stretch")
                if ev.get("dtc_ids"):
                    st.dataframe(S.records_by_id(conn, user, vid, "diagnostic_events", ev["dtc_ids"])[["ts", "code", "description"]],
                                 hide_index=True, width="stretch")
            if a.acknowledged:
                st.caption(f"✓ Acknowledged {a.ack_ts}")
                if st.button("Mark as unread", key=f"un{a.id}"):
                    S.acknowledge_alert(conn, user, a.id, False); st.rerun()
            elif st.button("Acknowledge", key=f"ack{a.id}", type="primary"):
                S.acknowledge_alert(conn, user, a.id, True); st.rerun()


def efficiency(conn, user, vid):
    st.title("Driving Efficiency")
    e = S.efficiency(conn, user, vid)
    d = e["comparison"]["diff"]
    c = st.columns(4)
    cons = d["consumption_kwh_per_100km"]
    tile(c[0], "This week", f"{cons['new'] or '—'}", "kWh/100 km")
    tile(c[1], "Previous week", f"{cons['old'] or '—'}", "kWh/100 km")
    tile(c[2], "Change", f"{cons['pct']:+.1f}%" if cons["pct"] is not None else "—", "calculated")
    tile(c[3], "Distance this week", f"{d['distance_km']['new']:.0f} km", f"prev {d['distance_km']['old']:.0f} km")
    dc = e["daily"]
    fig = go.Figure(go.Bar(x=dc["date"], y=dc["kwh_per_100km"], marker_color="#22d3ee"))
    fig.update_layout(title="Daily energy use (kWh/100 km)")
    st.plotly_chart(fig_style(fig, 280), width="stretch")
    st.markdown("#### Possible contributing factors")
    shown = False
    if cons["pct"] and cons["pct"] > 5:
        if (d["avg_driving_ambient_c"]["abs"] or 0) <= -5:
            st.markdown(f"- Colder weather while driving ({d['avg_driving_ambient_c']['abs']:+.1f} °C) — heating uses energy."); shown = True
        if (d["avg_driving_speed_kmh"]["pct"] or 0) >= 10:
            st.markdown(f"- Faster average driving ({d['avg_driving_speed_kmh']['pct']:+.0f}%)."); shown = True
        if (d["min_tire_pressure_kpa"]["abs"] or 0) <= -15:
            st.markdown(f"- Lower tyre pressure ({d['min_tire_pressure_kpa']['new']} kPa)."); shown = True
    if not shown:
        st.caption("No contributing factor is clearly supported by the recorded data.")
    st.caption("These are associations in the data, not confirmed causes.")
    st.markdown("#### Trips")
    t = e["trips"].copy()
    st.dataframe(t.assign(**{c_: t[c_].round(1) for c_ in ["distance_km", "energy_kwh", "avg_speed", "avg_ambient", "kwh_per_100km"]}),
                 hide_index=True, width="stretch")


def whatif(conn, user, vid):
    st.title("What-If Simulator")
    st.markdown(f'{badge("simulated")} Every result here is a <b>hypothetical estimate</b>, not a measurement. Your records are never changed.',
                unsafe_allow_html=True)
    period = st.selectbox("Baseline", ["Last 7 days", "Previous 7 days"])
    per = analytics.last_n_days(conn, 7) if period == "Last 7 days" else analytics.last_n_days(conn, 7, 7, "previous 7 days")
    c1, c2, c3 = st.columns(3)
    cons = c1.slider("Energy use change (%)", -30, 50, 10)
    spd = c2.slider("Average speed change (%)", -30, 30, 0)
    amb = c3.slider("Outside temperature (°C)", -20, 40, 20)
    try:
        base, res = S.simulate(conn, user, vid, per, simulator.Params(cons, spd, 0, 0, float(amb)))
    except ValueError as e:
        st.error(str(e)); return
    r = res["range"]
    k = st.columns(3)
    tile(k[0], "Baseline range", f"{r['baseline']:.0f} km", "from your records")
    tile(k[1], "Simulated range", f"{r['simulated']:.0f} km", "SIMULATED")
    tile(k[2], "Difference", f"{r['abs']:+.0f} km", f"{r['pct']:+.1f}%")
    fig = go.Figure(go.Bar(x=["Baseline", "Simulated"], y=[r["baseline"], r["simulated"]], marker_color=["#475569", "#c084fc"]))
    fig.update_layout(title="Estimated range (km)")
    st.plotly_chart(fig_style(fig, 260), width="stretch")
    with st.expander("Assumptions"):
        for a in res["assumptions"] + res["limitations"]:
            st.markdown(f"- {a}")
    if st.button("Save this scenario"):
        S.save_simulation(conn, user, vid, f"Driver scenario {cons:+d}% / {spd:+d}% / {amb}°C", base,
                          simulator.Params(cons, spd, 0, 0, float(amb)), res)
        st.success("Saved.")


def history(conn, user, vid):
    st.title("Event History")
    al = S.alerts(conn, user, vid)
    dt = S.dtcs(conn, user, vid)
    rows = [{"ts": a.ts, "category": "warning" if a.subsystem != "data" else "data quality", "severity": a.severity,
             "subsystem": a.subsystem, "description": a.message} for a in al.itertuples() if a.rule_id != "R-DTC-SINGLE"]
    rows += [{"ts": d.ts, "category": "diagnostic code", "severity": "Low" if d.reported_level == "warning" else "Informational",
              "subsystem": d.subsystem, "description": f"{d.code}: {d.description}"} for d in dt.itertuples()]
    df = pd.DataFrame(rows)
    if df.empty:
        empty("No events recorded."); return
    lo, hi = df["ts"].min().date(), df["ts"].max().date()
    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    rng = c1.date_input("Date range", (lo, hi), min_value=lo, max_value=hi)
    cats = c2.multiselect("Category", sorted(df["category"].unique()), default=sorted(df["category"].unique()))
    sev = c3.multiselect("Severity", SEVERITIES, default=SEVERITIES)
    text = c4.text_input("Search", placeholder="e.g. tyre, battery")
    f = df[df["category"].isin(cats) & df["severity"].isin(sev)]
    if isinstance(rng, tuple) and len(rng) == 2:
        f = f[(f["ts"].dt.date >= rng[0]) & (f["ts"].dt.date <= rng[1])]
    if text.strip():
        f = f[f["description"].str.contains(text.strip(), case=False, regex=False)]
    st.caption(f"{len(f)} of {len(df)} events")
    st.dataframe(f.sort_values("ts", ascending=False), hide_index=True, width="stretch")


def maint(conn, user, vid):
    st.title("Maintenance Planner")
    st.caption("Reminders are **your own schedule** or **generic suggestions**. None are manufacturer-recommended intervals.")
    odo = maintenance.latest_odometer(conn, vid)
    st.markdown(f"Latest recorded odometer: **{odo:,.0f} km** {badge('historical')}" if odo else "Odometer unavailable.",
                unsafe_allow_html=True)
    df = S.maintenance_list(conn, user, vid)
    if df.empty:
        empty("No reminders yet. Add one below or start from a generic suggestion.")
    for r in df.itertuples():
        icon = {"overdue": "🔴", "due soon": "🟡", "scheduled": "🟢", "done": "✅"}[r.due_state]
        src = "Generic suggestion" if r.source == "generic_suggestion" else "Your schedule"
        with st.container(border=True):
            a, b, c = st.columns([5, 1, 1])
            a.markdown(f"{icon} **{r.title}** · {r.category} · _{src}_<br><span class='vg-muted'>due "
                       f"{r.due_date or '—'}{f' / {r.due_odometer_km:,.0f} km' if pd.notna(r.due_odometer_km) else ''} · "
                       f"{r.due_state}</span>" + (f"<br>{r.notes}" if r.notes else ""), unsafe_allow_html=True)
            if r.status == "open" and b.button("Done", key=f"md{r.id}"):
                S.maintenance_complete(conn, user, r.id); st.rerun()
            with c:
                if confirm_button("Delete", f"mdel{r.id}"):
                    S.maintenance_delete(conn, user, r.id); st.rerun()
    st.markdown("#### Add reminder")
    with st.form("madd", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        title = c1.text_input("Title", max_chars=120)
        cat = c2.selectbox("Category", maintenance.CATEGORIES)
        due = c3.date_input("Due date", value=None)
        km = c1.number_input("Due odometer (km, optional)", min_value=0.0, value=None, step=500.0)
        idays = c2.number_input("Repeat every N days (optional)", min_value=1, max_value=3650, value=None)
        ikm = c3.number_input("Repeat every N km (optional)", min_value=100.0, value=None, step=500.0)
        notes = st.text_area("Notes", max_chars=1000)
        if st.form_submit_button("Add", type="primary"):
            try:
                S.maintenance_add(conn, user, vid, title=title, category=cat, due_date=due.isoformat() if due else None,
                                  due_odometer_km=km, interval_days=idays, interval_km=ikm, notes=notes)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    st.markdown("#### Generic suggestions")
    for i, s in enumerate(maintenance.GENERIC_SUGGESTIONS):
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"{s['title']} — every {s['interval_days'] or ''}{' days' if s['interval_days'] else ''}"
                    f"{(str(int(s['interval_km'])) + ' km') if s['interval_km'] else ''} _(generic, not manufacturer)_")
        if c2.button("Add", key=f"sg{i}"):
            S.maintenance_suggest(conn, user, vid, i); st.rerun()


def reports_page(conn, user, vid):
    st.title("Reports & Exports")
    h = S.health_score(conn, user, vid)
    al = S.alerts(conn, user, vid, (pd.Timestamp(h["as_of"]) - pd.Timedelta(days=7)).isoformat())
    e = S.efficiency(conn, user, vid)
    v = S.vehicle_list(conn, user).set_index("vehicle_id").loc[vid].to_dict() | {"vehicle_id": vid}
    st.markdown(f"Health summary for **{v['nickname']}**: score {h['score']}/100 ({h['band']}), {len(al)} alerts in the last 7 days.")
    with st.spinner("Preparing PDF…"):
        pdf = reports.driver_health_pdf(user, v, h, al, e)
    c1, c2, c3 = st.columns(3)
    c1.download_button("⬇ Health summary (PDF)", pdf, f"health_{vid}.pdf", "application/pdf", type="primary")
    c2.download_button("⬇ All alerts (CSV)", reports.to_csv(S.alerts(conn, user, vid).drop(columns=["evidence_json", "fingerprint"])),
                       f"alerts_{vid}.csv", "text/csv")
    c3.download_button("⬇ Daily consumption (CSV)", reports.to_csv(e["daily"]), f"consumption_{vid}.csv", "text/csv")


def settings(conn, user, vid):
    st.title("Settings & Privacy")
    t1, t2, t3, t4 = st.tabs(["Profile", "Password", "My vehicles", "Privacy & data"])
    with t1:
        with st.form("prof"):
            name = st.text_input("Display name", user["display_name"])
            email = st.text_input("Email", user["email"])
            if st.form_submit_button("Save profile"):
                try:
                    S.update_profile(conn, user, name, email); st.success("Saved."); st.rerun()
                except ValueError as e:
                    st.error(str(e))
        st.caption(f"Username: {user['username']} · role: driver · member since {user['created_at'][:10]}")
    with t2:
        password_form(user, "drv")
    with t3:
        st.dataframe(S.vehicle_list(conn, user), hide_index=True, width="stretch")
        with st.form("claim"):
            st.markdown("**Add a vehicle** (requires its VIN and pairing code)")
            vin = st.text_input("VIN", max_chars=17)
            code = st.text_input("Pairing code", type="password", max_chars=40)
            if st.form_submit_button("Add vehicle"):
                try:
                    st.success(f"Added {S.claim_vehicle(conn, user, vin, code)}."); st.rerun()
                except S.PermissionDenied as e:
                    st.error(str(e))
    with t4:
        status_sidebar()
        st.markdown("""
**What is stored, and where:** everything stays in a local SQLite file on this device (`data/vehiclegpt.db`): your account
(password stored only as an Argon2 hash), your vehicle assignments, telemetry and diagnostic records, alerts, maintenance
reminders, saved simulations and an audit log of your actions. Nothing is sent to cloud services.

**Data sources:** synthetic historical telemetry and synthetic `SIM-*` diagnostic codes generated for demonstration.
There is **no live connection** to your vehicle. Questions may be rephrased by a *local* language model if one is installed.""")
        st.markdown("**Your recent activity**")
        st.dataframe(S.my_activity(conn, user), hide_index=True, width="stretch")
