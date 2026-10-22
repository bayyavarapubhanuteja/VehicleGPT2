"""Administrator console: provisioning, vehicle assignment, account status, reset codes, audit log."""
from __future__ import annotations

import streamlit as st

from vehiclegpt import auth, services as S
from ui.auth_pages import password_form
from ui.common import confirm_button, status_sidebar


def render(conn, user):
    with st.sidebar:
        st.markdown(f"### 🔐 {user['display_name']}")
        st.caption("Administrator console")
        page = st.radio("Navigate", ["Users", "Vehicle access", "Audit log", "Account"], key="adm_page", label_visibility="collapsed")
        st.divider()
        status_sidebar()
    st.title(page)
    if page == "Users":
        st.dataframe(S.admin_users(conn, user), hide_index=True, width="stretch")
        with st.form("prov", clear_on_submit=True):
            st.markdown("**Provision account** (engineer accounts can only be created here or via `scripts/manage_users.py`)")
            c1, c2 = st.columns(2)
            un = c1.text_input("Username"); em = c2.text_input("Email")
            dn = c1.text_input("Display name"); role = c2.selectbox("Role", ["engineer", "driver", "admin"])
            pw = st.text_input("Temporary password (user must change it at first login)", type="password")
            if st.form_submit_button("Create account", type="primary"):
                try:
                    st.success(f"Created user #{S.provision_user(conn, user, un, em, dn, pw, role)}.")
                except (auth.AuthError, ValueError) as e:
                    st.error(str(e))
        users = S.admin_users(conn, user)
        uid = st.selectbox("Select user", users["id"].tolist(),
                           format_func=lambda i: f"#{i} {users.set_index('id').loc[i, 'username']} ({users.set_index('id').loc[i, 'role']})")
        row = users.set_index("id").loc[uid]
        c1, c2 = st.columns(2)
        with c1:
            if row["status"] == "active" and uid != user["id"]:
                if confirm_button("Disable account", f"dis{uid}", "The user is signed out immediately."):
                    S.set_user_status(conn, user, uid, "disabled"); st.rerun()
            elif row["status"] == "disabled" and st.button("Re-enable account"):
                S.set_user_status(conn, user, uid, "active"); st.rerun()
        with c2:
            if st.button("Issue password-reset code"):
                st.info(f"One-time code (valid 30 min) — deliver out-of-band: `{S.admin_reset_token(conn, user, uid)}`")
    elif page == "Vehicle access":
        st.dataframe(S.admin_assignments(conn, user), hide_index=True, width="stretch")
        users = S.admin_users(conn, user)
        users = users[users["role"] != "admin"]
        vids = S.admin_vehicles(conn, user)["vehicle_id"].tolist()
        with st.form("grant"):
            uid = st.selectbox("User", users["id"].tolist(),
                               format_func=lambda i: f"{users.set_index('id').loc[i, 'username']} ({users.set_index('id').loc[i, 'role']})")
            vid = st.selectbox("Vehicle", vids)
            c1, c2 = st.columns(2)
            g = c1.form_submit_button("Grant access", type="primary")
            r = c2.form_submit_button("Revoke access")
            if g or r:
                try:
                    S.assign_vehicle(conn, user, uid, vid, grant=g); st.rerun()
                except ValueError as e:
                    st.error(str(e))
        st.dataframe(S.admin_vehicles(conn, user), hide_index=True, width="stretch")
        st.caption("Administrators manage access but cannot view vehicle telemetry.")
    elif page == "Audit log":
        st.dataframe(S.admin_audit(conn, user), hide_index=True, width="stretch")
    else:
        password_form(user, "adm")
