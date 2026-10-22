"""Landing page, portal-specific login, driver registration, password reset, forced password change."""
from __future__ import annotations

import streamlit as st

from vehiclegpt import auth, services
from ui.common import LOGO, badge

DEV_SHOW_RESET = auth.env() != "production"


def _go(view: str):
    st.session_state.auth_view = view
    st.rerun()


def landing():
    st.markdown(f"""<div class="vg-hero">{LOGO}
<h1>VehicleGPT 2.0</h1>
<p>Offline, evidence-backed vehicle intelligence. Owners get clear health insights; engineers get a full diagnostic workspace —
both powered by the same verified analytics engine running entirely on this device.</p>
<p>{badge('offline', 'RUNS LOCALLY')} {badge('historical')} {badge('calculated', 'EVIDENCE-BACKED')}</p></div>""",
                unsafe_allow_html=True)
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown('<div class="vg-portal"><h3>🚗 Driver / Vehicle Owner</h3><p class="vg-muted">Vehicle health score, plain-language '
                    'assistant, early warnings, efficiency insights, maintenance planner and reports.</p></div>', unsafe_allow_html=True)
        if st.button("Continue as Driver / Vehicle Owner", type="primary", width="stretch", key="go_driver"):
            _go("login_driver")
    with c2:
        st.markdown('<div class="vg-portal"><h3>🛠️ Engineer</h3><p class="vg-muted">Event detective, technical queries, anomaly lab, '
                    'root-cause cases, data-quality monitor, diagnostic reports and benchmarks.</p></div>', unsafe_allow_html=True)
        if st.button("Continue as Engineer", type="primary", width="stretch", key="go_engineer"):
            _go("login_engineer")
    st.caption("Informational prototype using synthetic data — not a safety-certified automotive system. "
               "No connection to vehicle controls.")


def login(portal: str):
    title = "Driver / Vehicle Owner sign-in" if portal == "driver" else "Engineer sign-in"
    st.markdown(f"<div style='display:flex;gap:12px;align-items:center'>{LOGO}<h2 style='margin:0'>{title}</h2></div>",
                unsafe_allow_html=True)
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    with st.form(f"login_{portal}"):
        ident = st.text_input("Username or email", max_chars=120)
        pw = st.text_input("Password", type="password", max_chars=128)
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        conn = st.session_state._conn
        try:
            with st.spinner("Verifying…"):
                user = auth.authenticate(conn, ident, pw, portal)
            st.session_state.vg_token = auth.create_session(conn, user["id"])
            st.session_state.auth_view = None
            st.rerun()
        except auth.AuthError as e:
            st.error(str(e))
    c1, c2, c3 = st.columns(3)
    if c1.button("← Back", key="back"):
        _go(None)
    if c2.button("Forgot password?", key="forgot"):
        _go(f"reset_{portal}")
    if portal == "driver":
        if c3.button("Create a driver account", key="register"):
            _go("register")
    else:
        c3.caption("Engineer accounts are provisioned by an administrator.")
    if auth.env() != "production":
        with st.expander("Local development demo accounts"):
            st.markdown("Password for all demo accounts: `" + auth.DEMO_PASSWORD + "` (set `VEHICLEGPT_DEMO_PASSWORD` to change; "
                        "demo accounts are never created when `VEHICLEGPT_ENV=production`).")
            st.markdown("- Driver portal: `alice` (owns SYN-EV-001), `bob` (owns SYN-EV-002)\n"
                        "- Engineer portal: `eng.chen` (SYN-EV-001, 002), `eng.rao` (SYN-EV-003), `admin` (administration)")


def register():
    st.markdown("## Create a driver account")
    st.caption("Public registration creates **driver** accounts only. You need your vehicle's VIN and pairing code.")
    with st.form("register"):
        c1, c2 = st.columns(2)
        username = c1.text_input("Username", max_chars=32)
        name = c2.text_input("Display name", max_chars=80)
        email = c1.text_input("Email", max_chars=200)
        vin = c2.text_input("Vehicle VIN", max_chars=17)
        pw = c1.text_input("Password (≥10 chars, letters + digits)", type="password", max_chars=128)
        pw2 = c2.text_input("Confirm password", type="password", max_chars=128)
        code = st.text_input("Vehicle pairing code", type="password", max_chars=40,
                             help="Demo: VIN SYNVIN00000000003 / code PAIR-003-DEMO is unclaimed.")
        ok = st.form_submit_button("Create account", type="primary")
    if ok:
        if pw != pw2:
            st.error("Passwords do not match.")
        else:
            try:
                services.register_driver(st.session_state._conn, username, email, name, pw, vin, code)
                st.session_state.flash = "Account created — please sign in."
                _go("login_driver")
            except auth.AuthError as e:
                st.error(str(e))
    if st.button("← Back to sign-in"):
        _go("login_driver")


def reset(portal: str):
    st.markdown("## Reset password")
    conn = st.session_state._conn
    st.caption("This offline prototype has no email service. In development mode the one-time reset code is shown here; "
               "in production an administrator issues the code out-of-band.")
    with st.form("req"):
        ident = st.text_input("Username or email")
        if st.form_submit_button("Request reset code"):
            token = auth.request_reset(conn, ident) if ident.strip() else None
            st.success("If the account exists, a reset code has been issued (valid 30 minutes).")
            if token and DEV_SHOW_RESET:
                st.info(f"Development mode — reset code: `{token}`")
    with st.form("do"):
        code = st.text_input("Reset code")
        pw = st.text_input("New password", type="password")
        pw2 = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Set new password", type="primary"):
            if pw != pw2:
                st.error("Passwords do not match.")
            else:
                try:
                    auth.reset_password(conn, code, pw)
                    st.session_state.flash = "Password updated — please sign in."
                    _go(f"login_{portal}")
                except auth.AuthError as e:
                    st.error(str(e))
    if st.button("← Back to sign-in"):
        _go(f"login_{portal}")


def force_change(user: dict):
    st.markdown("## Change your password")
    st.warning("Your account requires a password change before continuing.")
    with st.form("chg"):
        old = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        new2 = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Update password", type="primary"):
            if new != new2:
                st.error("Passwords do not match.")
            else:
                try:
                    auth.change_password(st.session_state._conn, user["id"], old, new)
                    st.session_state.vg_token = auth.create_session(st.session_state._conn, user["id"])
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))


def password_form(user: dict, key: str):
    with st.form(f"pw_{key}"):
        old = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        new2 = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Change password"):
            if new != new2:
                st.error("Passwords do not match.")
            else:
                try:
                    auth.change_password(st.session_state._conn, user["id"], old, new)
                    st.session_state.vg_token = auth.create_session(st.session_state._conn, user["id"])
                    st.success("Password changed. Other sessions were signed out.")
                except auth.AuthError as e:
                    st.error(str(e))
