"""VehicleGPT 2.0 — role-based vehicle intelligence platform.  Run:  ./run.sh

Routing is decided ONLY by the role stored in the database for the session token's user.
Nothing a browser can set (page names, query parameters, session_state values) grants access:
every data operation is re-authorised in vehiclegpt/services.py.
"""
from __future__ import annotations

import streamlit as st

from vehiclegpt import auth, db, runtime
from ui import admin, auth_pages, driver, engineer
from ui.common import inject_css

st.set_page_config(page_title="VehicleGPT 2.0", page_icon="🚘", layout="wide", initial_sidebar_state="expanded")
inject_css()


@st.cache_resource(show_spinner="Preparing local database…")
def _bootstrap():
    return runtime.ensure_database()


_bootstrap()
conn = db.connect()  # one connection per script run
st.session_state._conn = conn

user = auth.validate_session(conn, st.session_state.get("vg_token"))

if user is None:
    st.session_state.pop("vg_token", None)
    view = st.session_state.get("auth_view")
    if view in ("login_driver", "login_engineer"):
        auth_pages.login(view.split("_")[1])
    elif view == "register":
        auth_pages.register()
    elif view in ("reset_driver", "reset_engineer"):
        auth_pages.reset(view.split("_")[1])
    else:
        auth_pages.landing()
    st.stop()

with st.sidebar:
    if st.button("Sign out", key="logout"):
        auth.logout(conn, st.session_state.get("vg_token"))
        for k in list(st.session_state.keys()):
            if k != "_conn":
                del st.session_state[k]
        st.rerun()

if user["must_change_password"]:
    auth_pages.force_change(user)
elif user["role"] == "driver":
    driver.render(conn, user)
elif user["role"] == "engineer":
    engineer.render(conn, user)
elif user["role"] == "admin":
    admin.render(conn, user)
else:
    st.error("Unknown role.")
