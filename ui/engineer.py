"""Engineer Portal — diagnostic and technical workspace. All data access goes through vehiclegpt.services."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from vehiclegpt import analytics, anomaly, cases, config, reports, runtime, services as S, simulator
from ui.auth_pages import password_form
from ui.common import (SEV_COLORS, SEVERITIES, badge, empty, fig_style, sev_badge, show_answer, status_sidebar, tile)

PAGES = ["Dashboard", "Event Detective", "Technical Query", "Anomaly Lab", "Investigations", "What-If Lab",
         "Data Quality", "Data Ingestion", "Diagnostic Reports", "Benchmarks", "Account"]
TECH_Q = ["Which vehicles experienced repeated temperature anomalies?",
          "Show diagnostic events associated with unusually high energy consumption.",
          "Compare sensor readings before and after a warning.", "Identify missing or implausible telemetry.",
          "Summarize the latest diagnostic events for this vehicle."]
CHANNELS = ["battery_temp_c", "motor_temp_c", "ambient_temp_c", "speed_kmh", "power_kw", "soc_pct", "tire_pressure_kpa"]


def render(conn, user):
    vdf = S.vehicle_list(conn, user)
    with st.sidebar:
        st.markdown(f"### 🛠️ {user['display_name']}")
        st.caption("Engineer portal")
        vids = vdf["vehicle_id"].tolist()
        if st.session_state.get("eng_vehicle") not in vids:
            st.session_state.pop("eng_vehicle", None)  # discard tampered/stale selection
        vid = st.selectbox("Vehicle", vids, key="eng_vehicle",
                           format_func=lambda v: f"{v} · {vdf.set_index('vehicle_id').loc[v, 'nickname']}") if vids else None
        page = st.radio("Navigate", PAGES, key="eng_page", label_visibility="collapsed")
        st.divider()
        status_sidebar()
    if vid is None and page not in ("Account", "Benchmarks"):
        st.title(page)
        empty("No vehicles are assigned to your account. Ask an administrator to grant access.")
        return
    {"Dashboard": dashboard, "Event Detective": detective_page, "Technical Query": tech_query, "Anomaly Lab": anomaly_lab,
     "Investigations": investigations, "What-If Lab": whatif_lab, "Data Quality": dq_page, "Data Ingestion": ingestion_page,
     "Diagnostic Reports": report_page, "Benchmarks": benchmarks, "Account": account}[page](conn, user, vid)


def dashboard(conn, user, vid):
    st.title("Engineer Dashboard")
    al = S.fleet_alerts(conn, user)
    cs = S.case_list(conn, user)
    vids = S.vehicles_for(conn, user)
    c = st.columns(5)
    tile(c[0], "Authorised vehicles", len(vids), ", ".join(vids))
    tile(c[1], "Open alerts", int(((al["acknowledged"] == 0) & (al["severity"] != "Informational")).sum()) if len(al) else 0, "unacknowledged")
    tile(c[2], "Unresolved cases", int(cs["status"].isin(["open", "investigating"]).sum()) if len(cs) else 0, "open / investigating")
    tile(c[3], "Anomalies (High)", int((al["severity"] == "High").sum()) if len(al) else 0, "all time")
    tile(c[4], "Diagnostic events", sum(len(S.dtcs(conn, user, v)) for v in vids), "recorded")
    if al.empty:
        empty("No alerts."); return
    with st.container(border=True):
        f1, f2, f3, f4 = st.columns(4)
        fv = f1.multiselect("Vehicle", vids, default=vids)
        lo, hi = al["ts"].min().date(), al["ts"].max().date()
        dr = f2.date_input("Date range", (lo, hi), min_value=lo, max_value=hi)
        fs = f3.multiselect("Subsystem", sorted(al["subsystem"].unique()), default=sorted(al["subsystem"].unique()))
        fsev = f4.multiselect("Severity", SEVERITIES, default=["Low", "Medium", "High"])
    f = al[al["vehicle_id"].isin(fv) & al["subsystem"].isin(fs) & al["severity"].isin(fsev)]
    if isinstance(dr, tuple) and len(dr) == 2:
        f = f[(f["ts"].dt.date >= dr[0]) & (f["ts"].dt.date <= dr[1])]
    l, r = st.columns(2)
    with l:
        fig = go.Figure()
        for s in SEVERITIES:
            g = f[f["severity"] == s]
            fig.add_scatter(x=g["ts"], y=g["vehicle_id"], mode="markers", name=s, marker=dict(size=11, color=SEV_COLORS[s]),
                            hovertext=g["message"])
        fig.update_layout(title="Anomaly timeline by vehicle")
        st.plotly_chart(fig_style(fig, 300), width="stretch")
    with r:
        cnt = f.groupby(["subsystem", "severity"]).size().reset_index(name="n")
        fig = go.Figure([go.Bar(x=cnt[cnt.severity == s]["subsystem"], y=cnt[cnt.severity == s]["n"], name=s,
                                marker_color=SEV_COLORS[s]) for s in SEVERITIES])
        fig.update_layout(barmode="stack", title="Anomaly counts by subsystem")
        st.plotly_chart(fig_style(fig, 300), width="stretch")
    st.dataframe(f[["id", "vehicle_id", "ts", "severity", "subsystem", "rule_id", "observed_value", "threshold", "message",
                    "acknowledged", "rule_version"]].sort_values("ts", ascending=False), hide_index=True, width="stretch")
    st.markdown("#### Recent investigation activity")
    ri = S.recent_investigations(conn, user)
    st.dataframe(ri, hide_index=True, width="stretch") if len(ri) else empty("No investigation activity yet.")


def detective_page(conn, user, vid):
    st.title("Advanced Event Detective")
    evs = S.events(conn, user, vid)
    if evs.empty:
        empty("No events for this vehicle."); return
    labels = {r.key: f"{r.ts:%Y-%m-%d %H:%M} · {r.kind} · {r.label[:90]}" for r in evs.itertuples()}
    c1, c2, c3 = st.columns([4, 1, 1])
    key = c1.selectbox("Warning / event", list(labels), format_func=labels.get, key="det_key")
    preset = c2.selectbox("Window preset", ["±5 min", "±15 min", "±60 min", "-120/+60 min", "Custom"], index=3)
    pm = {"±5 min": (5, 5), "±15 min": (15, 15), "±60 min": (60, 60), "-120/+60 min": (120, 60)}
    if preset == "Custom":
        b = c3.number_input("Before (min)", 1, 1440, 30); a_ = c3.number_input("After (min)", 1, 1440, 30)
    else:
        b, a_ = pm[preset]
    inv = S.investigate(conn, user, key, b, a_)
    e = inv["event"]
    st.markdown(f"<div class='vg-card'><b>{e['title']}</b><br><span class='vg-muted'>{vid} · {e['ts']} · subsystem {e['subsystem']} · "
                f"window {inv['window']['start']} → {inv['window']['end']}</span></div>", unsafe_allow_html=True)
    tel = inv["telemetry"]
    if tel.empty:
        st.warning("No telemetry in this window — insufficient evidence.")
    else:
        tv = tel[tel["valid"] == 1]
        chans = st.multiselect("Channels", CHANNELS, default=list(dict.fromkeys([inv["focus_channel"] or "battery_temp_c",
                                                                                 "ambient_temp_c", "power_kw"])))
        norm = st.toggle("Normalise channels (z-score) for correlation view")
        fig = go.Figure()
        for ch in chans:
            y = tv[ch]
            if norm and y.std():
                y = (y - y.mean()) / y.std()
            fig.add_scatter(x=tv["ts"], y=y, mode="lines+markers", name=ch)
        fig.add_vline(x=pd.Timestamp(e["ts"]), line_dash="dash", line_color="#f87171")
        fig.update_layout(title="Timestamped telemetry (valid samples; red = event)")
        st.plotly_chart(fig_style(fig, 360), width="stretch")
        if len(tv) > 3:
            corr = tv[CHANNELS].corr().round(2)
            with st.expander("Channel correlations in window (statistical association only)"):
                st.dataframe(corr, width="stretch")
    l, r = st.columns(2)
    with l:
        st.markdown(f"#### Observed sequence {badge('measured')}", unsafe_allow_html=True)
        for t in inv["timeline"]:
            st.markdown(f"<div class='vg-conf'><code>{t['ts'][5:16]}</code> {t['text']} <span class='vg-muted'>#{t['record_id']} · "
                        f"{t['source']}</span></div>", unsafe_allow_html=True)
        for c_ in inv["confirmed"]:
            st.markdown(f"<div class='vg-conf'>{c_}</div>", unsafe_allow_html=True)
    with r:
        st.markdown("#### Statistical associations & candidate explanations")
        for h in inv["possible_explanations"]:
            st.markdown(f"<div class='vg-hyp'><b>[candidate · {h['support']} support]</b> {h['text']}<br>"
                        f"<span class='vg-muted'>evidence records {h['evidence'][:8]}</span></div>", unsafe_allow_html=True)
        if not inv["possible_explanations"]:
            empty("Insufficient evidence for any candidate explanation.")
        st.markdown("<div class='vg-assoc'><b>Confirmed root cause:</b> none — requires engineer review in an investigation case.</div>",
                    unsafe_allow_html=True)
        for l_ in inv["limitations"]:
            st.caption("⚠️ " + l_)
    st.markdown("#### Before / after")
    st.dataframe(pd.DataFrame(inv["comparison"]), hide_index=True, width="stretch")
    if inv["history"]:
        st.markdown("**Historical incidents (same code):** " + ", ".join(h["ts"][:16] for h in inv["history"]))
    with st.expander(f"Raw records ({len(tel)})"):
        st.dataframe(tel, hide_index=True, width="stretch")
        st.download_button("⬇ Window CSV", reports.to_csv(tel), f"window_{vid}_{e['ts'][:10]}.csv", "text/csv")
    with st.form("open_case"):
        st.markdown("**Open an investigation case for this event**")
        title = st.text_input("Case title", value=e["title"][:120])
        prio = st.selectbox("Priority", cases.PRIORITIES, index=1)
        summ = st.text_area("Summary")
        if st.form_submit_button("Create case", type="primary"):
            try:
                cid = S.case_create(conn, user, vid, title, prio, key, summ)
                st.success(f"Case #{cid} created with {len(inv['possible_explanations'])} candidate explanation(s). See Investigations.")
            except (ValueError, S.PermissionDenied) as ex:
                st.error(str(ex))


def tech_query(conn, user, vid):
    st.title("Technical Query")
    st.caption("Natural-language requests are converted to validated structured queries over your authorised vehicles.")
    cols = st.columns(len(TECH_Q))
    for i, q in enumerate(TECH_Q):
        if cols[i].button(q, key=f"tq{i}", width="stretch"):
            st.session_state.eng_q = q
    with st.form("eng_ask", clear_on_submit=True):
        q = st.text_input("Query", max_chars=500, placeholder="e.g. max motor temperature on Sep 19")
        use_llm = st.checkbox("Allow local LLM for interpretation (numbers are always computed deterministically)", value=True)
        if st.form_submit_button("Run", type="primary") and q.strip():
            st.session_state.eng_q = q
            st.session_state.eng_llm = use_llm
    if st.session_state.get("eng_q"):
        with st.spinner("Running structured query…"):
            a = S.ask(conn, user, vid, st.session_state.pop("eng_q"), st.session_state.get("eng_llm", True))
        st.session_state.setdefault("eng_hist", []).insert(0, a)
    for a in st.session_state.get("eng_hist", [])[:5]:
        st.markdown(f"#### ⌘ {a.question} <span class='vg-muted'>[{a.vehicle_id}]</span>", unsafe_allow_html=True)
        show_answer(a, technical=True)
        if a.supported and a.facts and not a.ai_text and S.llm_available():
            if st.button("✨ Grounded AI summary", key=f"eng_ai{id(a)}"):
                with st.spinner("Local model running…"):
                    S.explain_answer(conn, user, a)
                st.rerun()
        st.divider()


def anomaly_lab(conn, user, vid):
    st.title("Anomaly Lab")
    st.caption(f"Ruleset {anomaly.RULESET_VERSION}. Thresholds are demonstration values, not manufacturer specifications.")
    th = S.thresholds(conn, user)
    with st.form("th"):
        cols = st.columns(3)
        new = {k: cols[i % 3].number_input(k, value=float(v), step=0.5) for i, (k, v) in enumerate(th.items())}
        c1, c2 = st.columns(2)
        test = c1.form_submit_button("Evaluate candidate thresholds (no changes saved)")
        save = c2.form_submit_button("Save thresholds & re-run detection", type="primary")
    if test or save:
        try:
            with st.spinner("Running detection against labelled synthetic events…"):
                cur = S.evaluate_detection(conn, user)
                cand = S.evaluate_detection(conn, user, new)
            rows = []
            for scope in ("demo_vehicle", "fleet"):
                for lbl, res in (("current", cur), ("candidate", cand)):
                    r = res[scope]
                    rows.append({"scope": scope, "thresholds": lbl, "precision": r["precision"], "recall": r["recall"],
                                 "f1": r["f1"], "alerts": r["alerts_scored"], "missed": ", ".join(r["missed"])})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            with st.expander("False positives (candidate, fleet)"):
                st.dataframe(pd.DataFrame(cand["fleet"]["false_positives"]), hide_index=True, width="stretch")
            if save:
                n = S.set_thresholds(conn, user, new)
                st.success(f"Saved and re-ran detection: {n} alerts. Change recorded in the audit log.")
        except ValueError as e:
            st.error(str(e))
    st.markdown("#### Rolling statistics (daily consumption vs rolling baseline)")
    dc = analytics.daily_consumption(conn, vehicle_id=vid)
    n = int(th["consumption_baseline_days"])
    dc["rolling_mean"] = dc["kwh_per_100km"].shift(1).rolling(n).mean()
    dc["rolling_std"] = dc["kwh_per_100km"].shift(1).rolling(n).std()
    dc["z"] = (dc["kwh_per_100km"] - dc["rolling_mean"]) / dc["rolling_std"]
    fig = go.Figure()
    fig.add_bar(x=dc["date"], y=dc["kwh_per_100km"], name="daily", marker_color="#22d3ee")
    fig.add_scatter(x=dc["date"], y=dc["rolling_mean"], name=f"rolling {n}-day mean", line=dict(color="#facc15"))
    fig.add_scatter(x=dc["date"], y=dc["rolling_mean"] * (1 + th["consumption_low_pct"] / 100), name="Low threshold",
                    line=dict(color="#fb923c", dash="dot"))
    st.plotly_chart(fig_style(fig, 300), width="stretch")
    st.dataframe(dc[["date", "kwh_per_100km", "rolling_mean", "rolling_std", "z"]].round(2), hide_index=True, width="stretch")
    st.markdown("#### Repeated-event analysis")
    rep = analytics.repeated_dtcs(conn, None, 2, vid)
    st.dataframe(rep, hide_index=True, width="stretch") if len(rep) else empty("No repeated diagnostic codes.")
    st.markdown("#### Rules & versions")
    st.dataframe(pd.DataFrame([{"rule": k, "version": v} for k, v in anomaly.RULE_VERSIONS.items()]), hide_index=True, width="stretch")
    with st.expander("Severity rules"):
        st.code(anomaly.__doc__)
    if st.button("Re-run detection for my vehicles"):
        st.success(f"{S.run_detection(conn, user)} alerts generated.")


def investigations(conn, user, vid):
    st.title("Investigations & Cases")
    cs = S.case_list(conn, user)
    with st.expander("➕ New case", expanded=cs.empty):
        with st.form("newcase"):
            evs = S.events(conn, user, vid)
            opts = [None] + evs["key"].tolist()
            lab = {r.key: f"{r.ts:%m-%d %H:%M} {r.label[:70]}" for r in evs.itertuples()}
            title = st.text_input("Title")
            ek = st.selectbox("Linked event (optional)", opts, format_func=lambda k: "— none —" if k is None else lab[k])
            engs = S.engineers_for_vehicle(conn, user, vid)
            asg = st.selectbox("Assign to", [None] + engs["id"].tolist(),
                               format_func=lambda i: "— unassigned —" if i is None else engs.set_index("id").loc[i, "display_name"])
            prio = st.selectbox("Priority", cases.PRIORITIES, index=1)
            summ = st.text_area("Summary")
            if st.form_submit_button("Create", type="primary"):
                try:
                    st.success(f"Case #{S.case_create(conn, user, vid, title, prio, ek, summ, asg)} created."); st.rerun()
                except (ValueError, S.PermissionDenied) as e:
                    st.error(str(e))
    if cs.empty:
        empty("No cases for your vehicles yet."); return
    st.dataframe(cs[["id", "vehicle_id", "title", "status", "priority", "root_cause_status", "assignee", "creator", "updated_at"]],
                 hide_index=True, width="stretch")
    cid = st.selectbox("Open case", cs["id"].tolist(), format_func=lambda i: f"#{i} · {cs.set_index('id').loc[i, 'title']}")
    c = S.case_get(conn, user, cid)
    st.markdown(f"### Case #{cid}: {c['title']}")
    st.markdown(f"{badge('calculated', c['status'].upper())} {badge('historical', 'ROOT CAUSE: ' + c['root_cause_status'].upper())} "
                f"<span class='vg-muted'>{c['vehicle_id']} · priority {c['priority']} · event {c['event_key'] or '—'}</span>",
                unsafe_allow_html=True)
    t1, t2, t3, t4 = st.tabs(["Candidate explanations", "Notes", "Status & assignment", "Audit trail"])
    with t1:
        hy = S.case_hypotheses(conn, user, cid)
        for h in hy.itertuples():
            with st.container(border=True):
                st.markdown(f"**#{h.id}** {h.text}<br><span class='vg-muted'>evidence strength: {h.evidence_strength} · "
                            f"status: <b>{h.status}</b> · evidence {json.loads(h.evidence_json)[:8]}"
                            f"{' · reviewed by ' + h.reviewer if h.reviewer else ''}</span>"
                            + (f"<br><i>{h.review_note}</i>" if h.review_note else ""), unsafe_allow_html=True)
                with st.form(f"rev{h.id}"):
                    s_ = st.selectbox("Review", cases.HYP_STATUSES, index=cases.HYP_STATUSES.index(h.status))
                    note = st.text_input("Justification (required ≥ 20 chars to confirm)")
                    if st.form_submit_button("Save review"):
                        try:
                            S.case_review_hypothesis(conn, user, h.id, s_, note); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
        with st.form("addhyp"):
            txt = st.text_area("Add candidate explanation")
            stg = st.selectbox("Evidence strength", ["weak", "moderate", "strong"])
            ids = st.text_input("Evidence record ids (comma-separated)")
            if st.form_submit_button("Add"):
                try:
                    S.case_add_hypothesis(conn, user, cid, txt, stg, [int(x) for x in ids.split(",") if x.strip().isdigit()]); st.rerun()
                except ValueError as e:
                    st.error(str(e))
    with t2:
        nt = S.case_notes(conn, user, cid)
        for n_ in nt.itertuples():
            st.markdown(f"<span class='vg-muted'>{n_.created_at} · {n_.author}</span><br>{n_.body}", unsafe_allow_html=True)
        with st.form("note", clear_on_submit=True):
            body = st.text_area("Add note")
            if st.form_submit_button("Add note") and body.strip():
                S.case_note(conn, user, cid, body); st.rerun()
    with t3:
        engs = S.engineers_for_vehicle(conn, user, c["vehicle_id"])
        with st.form("upd"):
            stt = st.selectbox("Status", cases.STATUSES, index=cases.STATUSES.index(c["status"]))
            ids = [None] + engs["id"].tolist()
            asg = st.selectbox("Assignee", ids, index=ids.index(c["assigned_to"]) if c["assigned_to"] in ids else 0,
                               format_func=lambda i: "— unassigned —" if i is None else engs.set_index("id").loc[i, "display_name"])
            pr = st.selectbox("Priority", cases.PRIORITIES, index=cases.PRIORITIES.index(c["priority"]))
            res = st.text_area("Resolution summary", c["resolution"] or "")
            if st.form_submit_button("Update case", type="primary"):
                try:
                    S.case_update(conn, user, cid, status=stt, assigned_to=asg, priority=pr, resolution=res); st.rerun()
                except (ValueError, S.PermissionDenied) as e:
                    st.error(str(e))
    with t4:
        st.dataframe(S.case_audit(conn, user, cid), hide_index=True, width="stretch")
    rep = S.case_report(conn, user, cid)
    st.markdown("#### Investigation report")
    st.markdown(f"**{rep['root_cause_statement']}**")
    st.markdown("**Missing information:**\n" + "\n".join(f"- {m}" for m in rep["missing_information"]))
    st.markdown("**Recommended diagnostic checks:**\n" + "\n".join(f"- {m}" for m in rep["recommended_checks"]))


def whatif_lab(conn, user, vid):
    st.title("What-If Laboratory")
    st.markdown(f"{badge('simulated')} Hypothetical estimates only. Results are stored in `simulation_runs`, never in telemetry.",
                unsafe_allow_html=True)
    now = analytics.reference_now(conn)
    days = pd.date_range(now - pd.Timedelta(days=21), now - pd.Timedelta(days=1))
    opts = {"Last 7 days": analytics.last_n_days(conn, 7), "Previous 7 days": analytics.last_n_days(conn, 7, 7, "previous 7 days")}
    opts |= {f"Day {d:%Y-%m-%d}": analytics.day(conn, d) for d in days}
    per = opts[st.selectbox("Baseline", list(opts))]
    n = st.number_input("Number of scenarios", 1, 4, 3)
    scen = []
    cols = st.columns(int(n))
    for i, c in enumerate(cols):
        with c:
            st.markdown(f"**Scenario {chr(65 + i)}**")
            scen.append(simulator.Params(c.slider("Consumption %", -50, 100, [0, 10, -10, 20][i], key=f"c{i}"),
                                         c.slider("Speed %", -50, 50, 0, key=f"s{i}"),
                                         c.slider("Usable energy %", -40, 10, 0, key=f"e{i}"),
                                         c.slider("Cooling improvement %", -50, 60, [0, 0, 20, 0][i], key=f"k{i}"),
                                         c.number_input("Ambient °C (blank = baseline)", -30.0, 50.0, value=None, key=f"a{i}")))
    try:
        results = [S.simulate(conn, user, vid, per, p) for p in scen]
    except ValueError as e:
        st.error(str(e)); return
    base = results[0][0]
    st.json({k: base[k] for k in ("consumption_kwh_per_100km", "avg_speed_kmh", "ambient_c", "usable_kwh", "range_km",
                                   "peak_battery_temp_c")}, expanded=False)
    tab = pd.DataFrame([{"scenario": chr(65 + i), **{k: v for k, v in p.__dict__.items() if k != "unit"},
                         "consumption": r["consumption"]["simulated"], "range_km": r["range"]["simulated"], "Δrange_km": r["range"]["abs"],
                         "Δrange_%": r["range"]["pct"], "peak_batt_C": r["thermal"]["sim_peak_c"] if r["thermal"] else None}
                        for i, (p, (_, r)) in enumerate(zip(scen, results))])
    st.dataframe(tab, hide_index=True, width="stretch")
    fig = go.Figure(go.Bar(x=["Baseline"] + tab["scenario"].tolist(), y=[results[0][1]["range"]["baseline"]] + tab["range_km"].tolist(),
                           marker_color=["#475569"] + ["#c084fc"] * len(tab)))
    fig.update_layout(title="Estimated range by scenario (km, SIMULATED)")
    st.plotly_chart(fig_style(fig, 280), width="stretch")
    st.markdown("#### Sensitivity analysis (scenario A)")
    param = st.selectbox("Parameter", ["consumption_change_pct", "speed_change_pct", "usable_energy_change_pct", "cooling_improvement_pct"])
    lo, hi = simulator.PARAM_LIMITS[param]
    sw = pd.DataFrame(simulator.sweep(base, scen[0], param, list(range(int(lo), int(hi) + 1, 5))))
    fig = go.Figure(go.Scatter(x=sw[param], y=sw["peak_temp" if param == "cooling_improvement_pct" else "range"], mode="lines+markers",
                               line=dict(color="#c084fc")))
    fig.update_layout(title=f"{'Peak battery °C' if param == 'cooling_improvement_pct' else 'Range km'} vs {param}")
    st.plotly_chart(fig_style(fig, 280), width="stretch")
    with st.expander("Formulas, assumptions, limitations", expanded=True):
        r = results[0][1]
        st.markdown("\n".join(f"- `{f}`" for f in r["formulas"] + ([r["thermal"]["formula"]] if r["thermal"] else [])))
        st.markdown("\n".join(f"- {x}" for x in r["assumptions"] + r["limitations"]))
    if st.button("Save all scenarios", type="primary"):
        for i, (p, (b, r)) in enumerate(zip(scen, results)):
            S.save_simulation(conn, user, vid, f"Lab scenario {chr(65 + i)}", b, p, r)
        st.success("Saved.")
    runs = S.simulation_runs(conn, user, vid)
    if runs:
        st.dataframe(pd.DataFrame([{"id": x["id"], "name": x["scenario"], "created": x["created_ts"],
                                    "range": x["result"]["range"]["simulated"], "Δ%": x["result"]["range"]["pct"]} for x in runs]),
                     hide_index=True, width="stretch")


def dq_page(conn, user, vid):
    st.title("Sensor & Data Quality Monitor")
    r = S.data_quality(conn, user, vid)
    c = st.columns(6)
    tile(c[0], "Completeness", f"{r['completeness_pct']}%", f"{r['rows']} rows")
    tile(c[1], "Gaps", len(r["gaps"]), "> threshold")
    tile(c[2], "Duplicate rows", len(r["duplicates"]), "same timestamp")
    tile(c[3], "Implausible", len(r["implausible"]), "flagged invalid")
    tile(c[4], "Unit suspects", len(r["unit_suspects"]), "°F vs °C etc.")
    tile(c[5], "Data age", f"{r['staleness']['age_hours']:.0f} h", "STALE" if r["staleness"]["stale"] else "current")
    st.caption(r["note"])
    st.markdown("#### Missing values by channel")
    st.dataframe(pd.DataFrame([r["missing_values"]]), hide_index=True, width="stretch")
    for title, data in [("Logging gaps", pd.DataFrame(r["gaps"])), ("Duplicate timestamps", r["duplicates"]),
                        ("Implausible readings", r["implausible"]), ("Unit inconsistencies", pd.DataFrame(r["unit_suspects"])),
                        ("Flat-lined (stuck) sensors", pd.DataFrame(r["flatlines"]))]:
        st.markdown(f"#### {title}")
        if len(data):
            st.dataframe(data.drop(columns=["record_ids"], errors="ignore"), hide_index=True, width="stretch")
        else:
            st.caption("None found.")
    st.markdown("#### Inspect affected records")
    ids_txt = st.text_input("Record ids (comma-separated) — e.g. from the tables above")
    ids = [int(x) for x in ids_txt.split(",") if x.strip().isdigit()]
    if not ids and len(r["unit_suspects"]):
        ids = r["unit_suspects"][0]["record_ids"][:20]
    if ids:
        st.dataframe(S.records_by_id(conn, user, vid, "telemetry", ids), hide_index=True, width="stretch")
    tr = pd.read_sql_query("SELECT * FROM data_transformations WHERE vehicle_id = ? ORDER BY id DESC", conn, params=(vid,))
    st.markdown("#### Recorded transformations")
    st.caption("Raw rows are never modified or deleted. Validation only sets `valid`/`validity_note`; analytics de-duplicate in memory.")
    st.dataframe(tr, hide_index=True, width="stretch") if len(tr) else st.caption("None.")


def ingestion_page(conn, user, vid):
    st.title("Data Ingestion & Retention")
    st.caption("Recorded drives are replayed through the real protocol decoders — CAN/CAN-FD (DBC), SOME/IP over UDP, "
               "Android VHAL properties, QNX slog2, UDS DTC reports and camera-health metadata — into a bench vehicle "
               "(SYN-LAB-01). The collector's bus tap is receive-only.")
    with st.form("replay"):
        c1, c2, c3 = st.columns(3)
        src = c1.selectbox("Recorded source vehicle", [v for v in S.vehicles_for(conn, user) if v != "SYN-LAB-01"])
        start = c2.text_input("Start (ISO)", "2026-09-19T08:00:00")
        hours = c3.number_input("Hours", 0.5, 24.0, 4.0, 0.5)
        go_ = st.form_submit_button("Run replay through ingestion pipeline", type="primary")
    if go_:
        try:
            with st.spinner("Encoding frames, publishing on CAN / SOME/IP / VHAL / QNX / UDS, decoding, aggregating…"):
                r = S.ingest_replay(conn, user, src, start, hours)
            k = st.columns(4)
            tile(k[0], "Frames/records", sum(p["frames"] for p in r["per_source"]), f"{r['frames_per_s']:,}/s")
            tile(k[1], "Telemetry rows", r["telemetry_rows_written"], "10-min buckets")
            tile(k[2], "Alerts on ingested data", r["alerts"], "SYN-LAB-01")
            tile(k[3], "Elapsed", f"{r['elapsed_s']} s", f"{r['samples']} samples")
            st.dataframe(pd.DataFrame(r["per_source"]), hide_index=True, width="stretch")
            st.markdown(f"#### Live buffer snapshot {badge('measured', 'LIVE BUFFER')}", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(r["live_snapshot"]).T, width="stretch")
            st.success("Select SYN-LAB-01 in the sidebar vehicle list to query the ingested data.")
        except (ValueError, S.PermissionDenied) as e:
            st.error(str(e))
    s_ = S.ingest_status(conn, user)
    st.markdown("#### Historical storage")
    st.json(s_["storage"])
    st.caption("Tiering: live ring buffer (last hour, in memory) → 10-minute rows kept 90 days → hourly roll-ups kept indefinitely.")
    days = st.slider("Preview retention cut-off (days of raw data to keep)", 7, 120, 90)
    st.json(S.retention_preview(conn, user, vid, days))
    st.markdown("#### Ingest log")
    st.dataframe(s_["log"], hide_index=True, width="stretch") if len(s_["log"]) else empty("No ingestion runs yet.")


def report_page(conn, user, vid):
    st.title("Diagnostic Report Generator")
    cs = S.case_list(conn, user)
    cs = cs[cs["vehicle_id"] == vid]
    v = S.vehicle_list(conn, user).set_index("vehicle_id").loc[vid].to_dict() | {"vehicle_id": vid}
    rules = pd.DataFrame([{"rule": k, "version": f"{v_} / ruleset {anomaly.RULESET_VERSION}"} for k, v_ in anomaly.RULE_VERSIONS.items()])
    if cs.empty:
        empty("Create an investigation case for this vehicle to generate a full diagnostic report. CSV exports are available below.")
    else:
        cid = st.selectbox("Case", cs["id"].tolist(), format_func=lambda i: f"#{i} · {cs.set_index('id').loc[i, 'title']}")
        with st.spinner("Building report…"):
            rep = S.case_report(conn, user, cid)
            pdf = reports.engineer_case_pdf(user, v, rep, rules)
        st.download_button("⬇ Diagnostic report (PDF)", pdf, f"case_{cid}_{vid}.pdf", "application/pdf", type="primary")
        st.download_button("⬇ Report data (JSON)", reports.json_bytes({k: v_ for k, v_ in rep.items() if k != "investigation"} |
                                                                        {"investigation": {k: v_ for k, v_ in (rep["investigation"] or {}).items()
                                                                                           if k != "telemetry"}}),
                           f"case_{cid}.json", "application/json")
    st.markdown("#### CSV exports")
    c1, c2, c3 = st.columns(3)
    c1.download_button("Alerts", reports.to_csv(S.alerts(conn, user, vid).drop(columns=["evidence_json"])), f"alerts_{vid}.csv", "text/csv")
    c2.download_button("Diagnostic events", reports.to_csv(S.dtcs(conn, user, vid)), f"dtcs_{vid}.csv", "text/csv")
    c3.download_button("Telemetry (full)", reports.to_csv(S.telemetry(conn, user, vid)), f"telemetry_{vid}.csv", "text/csv")


def benchmarks(conn, user, vid):
    st.title("Benchmarks & Model Evaluation")
    p = config.ROOT / "benchmarks" / "results.json"
    if p.exists():
        b = json.loads(p.read_text())
        st.markdown(f"Last benchmark run: **{b['timestamp']}** · {b['conditions']['platform']} · Python {b['conditions']['python']}")
        c = st.columns(4)
        q = b["query_latency_deterministic"]
        tile(c[0], "Query latency (median)", f"{q['median_of_medians_ms']} ms", f"worst median {q['max_median_ms']} ms")
        tile(c[1], "Intent accuracy", q["intent_accuracy"], "labelled test questions")
        tile(c[2], "Process RSS", f"{b['memory']['process_rss_end_mb']} MB", "no LLM loaded")
        de = b["detection_eval"]
        tile(c[3], "Detection F1 (demo)", de["mean_f1"], f"P {de['mean_precision']} · R {de['mean_recall']}")
        st.caption(de["caveat"])
        if "fleet_eval" in b:
            st.markdown(f"Fleet detection: P **{b['fleet_eval']['precision']}**, R **{b['fleet_eval']['recall']}**, "
                        f"F1 **{b['fleet_eval']['f1']}** (missed: {', '.join(b['fleet_eval']['missed']) or 'none'})")
        st.markdown("**LLM:** " + (f"{b['llm']['backend']} — measured" if b["llm"]["available"]
                                   else f"UNAVAILABLE — {b['llm'].get('note', b['llm']['detail'])}"))
        st.dataframe(pd.DataFrame(b["query_latency_deterministic"]["per_query"]).T, width="stretch")
        if b.get("tests"):
            st.markdown(f"**Automated tests:** {b['tests']}")
    me = config.ROOT / "benchmarks" / "model_eval.md"
    if me.exists():
        st.markdown("#### On-device language models (measured, 2 inference threads)")
        st.markdown(me.read_text())
    else:
        st.caption("Model comparison not run — `python benchmarks/model_eval.py`.")
    ep = config.ROOT / "benchmarks" / "edge_profile.json"
    if ep.exists():
        st.markdown("#### Edge resource profile")
        st.json(json.loads(ep.read_text()), expanded=False)
    else:
        st.warning("Benchmark has not been run on this machine — no results to show. Run `python benchmarks/run_benchmarks.py`.")
    st.markdown("#### Live measurement")
    if st.button("Run quick evaluation now"):
        with st.spinner("Measuring…"):
            ev, ms = runtime.timed(S.evaluate_detection, conn, user)
            lat = [runtime.timed(S.ask, conn, user, vid, q, False)[1] for q in TECH_Q]
        st.json({"detection_demo": {k: ev["demo_vehicle"][k] for k in ("precision", "recall", "f1")},
                 "detection_fleet": {k: ev["fleet"][k] for k in ("precision", "recall", "f1")},
                 "evaluation_ms": round(ms, 1), "query_latency_ms": [round(x, 1) for x in lat],
                 "process_rss_mb": round(runtime.process_rss_mb() or 0, 1)})
    h = S.eval_history(conn, user)
    if len(h):
        st.markdown("#### Evaluation history")
        st.dataframe(h, hide_index=True, width="stretch")


def account(conn, user, vid):
    st.title("Account")
    st.markdown(f"**{user['display_name']}** · `{user['username']}` · {user['email']} · role **{user['role']}**")
    st.dataframe(S.vehicle_list(conn, user), hide_index=True, width="stretch")
    password_form(user, "eng")
