"""Shared UI components (styling, tiles, answer rendering, charts)."""
from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from vehiclegpt import llm, runtime

CSS = """
<style>
:root{--bg:#0b1020;--panel:#131a2e;--panel2:#1a2340;--line:#26314f;--text:#e6ebf5;--muted:#93a0bd;--accent:#22d3ee;
--ok:#34d399;--info:#60a5fa;--low:#facc15;--med:#fb923c;--high:#f87171;--sim:#c084fc;--ai:#f472b6}
.block-container{padding-top:1.3rem;max-width:1440px}
.vg-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:10px}
.vg-tile{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:12px 14px;height:100%}
.vg-tile .k{color:var(--muted);font-size:.74rem;text-transform:uppercase;letter-spacing:.06em}
.vg-tile .v{font-size:1.6rem;font-weight:700;color:var(--text);font-variant-numeric:tabular-nums}
.vg-tile .s{color:var(--muted);font-size:.78rem}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.7rem;font-weight:600;margin-right:4px;border:1px solid}
.b-measured{color:var(--ok);border-color:var(--ok)} .b-calculated{color:var(--accent);border-color:var(--accent)}
.b-simulated{color:var(--sim);border-color:var(--sim)} .b-ai{color:var(--ai);border-color:var(--ai)}
.b-historical{color:#cbd5e1;border-color:#64748b} .b-stale{color:var(--high);border-color:var(--high)}
.b-offline{color:var(--ok);border-color:var(--ok)} .b-unavailable{color:var(--muted);border-color:var(--muted)}
.sev-High{color:#0b1020;background:var(--high);border-color:var(--high)} .sev-Medium{color:#0b1020;background:var(--med);border-color:var(--med)}
.sev-Low{color:#0b1020;background:var(--low);border-color:var(--low)} .sev-Informational{color:#0b1020;background:var(--info);border-color:var(--info)}
.vg-answer{font-size:1.04rem;line-height:1.55}
.vg-muted{color:var(--muted);font-size:.84rem}
.vg-safety{border-left:4px solid var(--high);background:#2a1620;padding:10px 14px;border-radius:8px;margin:8px 0}
.vg-hyp{border-left:3px solid var(--med);padding:6px 10px;margin:6px 0;background:var(--panel2);border-radius:6px}
.vg-conf{border-left:3px solid var(--ok);padding:6px 10px;margin:6px 0;background:var(--panel2);border-radius:6px}
.vg-assoc{border-left:3px solid var(--info);padding:6px 10px;margin:6px 0;background:var(--panel2);border-radius:6px}
.vg-hero{border-radius:20px;padding:44px 36px;margin-bottom:18px;border:1px solid var(--line);
 background:radial-gradient(1200px 400px at 80% -10%,rgba(34,211,238,.18),transparent 60%),
 radial-gradient(800px 300px at 0% 120%,rgba(192,132,252,.15),transparent 60%),linear-gradient(160deg,#0f172a,#0b1020)}
.vg-hero h1{font-size:2.6rem;margin:0 0 .3rem 0} .vg-hero p{color:var(--muted);font-size:1.05rem;max-width:720px}
.vg-portal{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;min-height:170px}
.vg-portal h3{margin-top:0}
.vg-score{font-size:3.4rem;font-weight:800;line-height:1}
</style>
"""

LOGO = """<svg width="44" height="44" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-label="VehicleGPT logo">
<defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#c084fc"/></linearGradient></defs>
<rect x="2" y="2" width="60" height="60" rx="16" fill="#0b1020" stroke="url(#g)" stroke-width="3"/>
<path d="M14 38 l6-11 c1-2 3-3 5-3 h14 c2 0 4 1 5 3 l6 11 v6 h-36z" fill="none" stroke="url(#g)" stroke-width="3" stroke-linejoin="round"/>
<circle cx="22" cy="44" r="4" fill="#22d3ee"/><circle cx="42" cy="44" r="4" fill="#c084fc"/>
<path d="M30 16 l4 0 l-3 6 h4 l-6 8 l2-6 h-4z" fill="#facc15"/></svg>"""

SEV_COLORS = {"High": "#f87171", "Medium": "#fb923c", "Low": "#facc15", "Informational": "#60a5fa"}
SEVERITIES = ["Informational", "Low", "Medium", "High"]


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def badge(kind: str, label: str | None = None) -> str:
    lbl = label or {"measured": "MEASURED", "calculated": "CALCULATED", "simulated": "SIMULATED", "ai": "AI-GENERATED",
                    "historical": "HISTORICAL (SYNTHETIC)", "stale": "STALE", "offline": "OFFLINE",
                    "unavailable": "UNAVAILABLE"}.get(kind, kind.upper())
    return f'<span class="badge b-{kind}">{lbl}</span>'


def sev_badge(sev: str) -> str:
    return f'<span class="badge sev-{sev}">{sev}</span>'


def tile(col, k, v, s=""):
    col.markdown(f'<div class="vg-tile"><div class="k">{k}</div><div class="v">{v}</div><div class="s">{s}</div></div>',
                 unsafe_allow_html=True)


def fig_style(fig, h=320):
    fig.update_layout(template="plotly_dark", height=h, margin=dict(l=10, r=10, t=40, b=10),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#131a2e", legend=dict(orientation="h", y=-0.22),
                      font=dict(color="#e6ebf5"))
    fig.update_xaxes(gridcolor="#26314f")
    fig.update_yaxes(gridcolor="#26314f")
    return fig


@st.cache_data(ttl=30, show_spinner=False)
def net_status():
    return runtime.network_available()


def status_sidebar():
    net = net_status()
    st.markdown("**Connectivity:** " + ("🟢 Offline — fully local" if net is False else
                                       "🟡 Network detected (not used for core processing)" if net else "⚪ Probe disabled"))
    s = llm.get_adapter().status()
    st.markdown(f"**AI model:** {'🟢 ' + s.backend if s.available else '⚪ deterministic fallback'}")
    st.markdown("**Data:** " + badge("historical") + " no live vehicle link", unsafe_allow_html=True)


def empty(msg: str):
    st.info(msg, icon="ℹ️")


def render_chart(c: dict):
    fig = go.Figure()
    if c["type"] == "bar":
        colors = ["#22d3ee" if x >= c.get("split", "9999") else "#475569" for x in c["x"]]
        fig.add_bar(x=c["x"], y=c["y"], marker_color=colors, name="kWh/100 km")
    else:
        fig.add_scatter(x=c["x"], y=c["y"], mode="lines+markers", line=dict(color="#22d3ee"), name=c["title"])
        if c.get("marker"):
            fig.add_vline(x=c["marker"], line_dash="dash", line_color="#f87171")
    fig.update_layout(title=c["title"])
    st.plotly_chart(fig_style(fig, 280), width="stretch")


def show_answer(a, technical: bool):
    with st.container(border=True):
        kinds = {e["kind"] for e in a.evidence} | {f["kind"] for f in a.facts}
        st.markdown(" ".join(badge(k) for k in sorted(kinds)) + (badge("ai") if a.ai_text else ""), unsafe_allow_html=True)
        for s in a.safety:
            st.markdown(f'<div class="vg-safety">⚠️ {s}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="vg-answer">{a.engineer if technical else a.driver}</div>', unsafe_allow_html=True)
        if a.ai_text:
            st.markdown(f'{badge("ai")} <span class="vg-muted">Local-model rephrasing (grounding-checked):</span> {a.ai_text}',
                        unsafe_allow_html=True)
        if a.limitations:
            with st.expander(f"Limitations & data caveats ({len(a.limitations)})", expanded=not a.supported):
                for l in a.limitations:
                    st.markdown(f"- {l}")
    if technical and a.structured_query:
        with st.expander("Structured query (validated)", expanded=True):
            st.code(json.dumps(a.structured_query, indent=2, default=str), language="json")
    if a.chart:
        render_chart(a.chart)
    if a.table:
        st.dataframe(pd.DataFrame(a.table), hide_index=True, width="stretch")
    if a.facts:
        with st.expander("Key figures", expanded=technical):
            st.dataframe(pd.DataFrame(a.facts), hide_index=True, width="stretch")
    if a.evidence:
        with st.expander(f"Evidence ({len(a.evidence)} records)", expanded=technical):
            ev = pd.DataFrame(a.evidence)
            st.dataframe(ev if technical else ev[["ts", "text", "kind"]], hide_index=True, width="stretch")
    r = a.structured_query.get("router", {}) if a.structured_query else {}
    st.caption(f"Answered in {a.latency_ms:.0f} ms · routed by {a.intent_source}"
               + (f" (model {r.get('llm_ms')} ms)" if r.get("llm_ms") else "")
               + (f" · intent={a.intent} · rules={r.get('rules')} · model={r.get('llm')}" if technical else ""))


def confirm_button(label: str, key: str, help_text: str = "This cannot be undone.") -> bool:
    """Two-step confirmation for destructive actions."""
    if st.session_state.get(f"confirm_{key}"):
        st.warning(f"Confirm: {label}? {help_text}")
        c1, c2 = st.columns(2)
        if c1.button("Yes, continue", key=f"yes_{key}", type="primary"):
            st.session_state.pop(f"confirm_{key}")
            return True
        if c2.button("Cancel", key=f"no_{key}"):
            st.session_state.pop(f"confirm_{key}")
            st.rerun()
        return False
    if st.button(label, key=f"ask_{key}"):
        st.session_state[f"confirm_{key}"] = True
        st.rerun()
    return False
