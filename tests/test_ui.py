"""Headless UI tests (Streamlit AppTest): login flows, both portals, every page, and tampering attempts."""
import os

import pytest
from streamlit.testing.v1 import AppTest

from conftest import PW

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


def app():
    return AppTest.from_file(APP, default_timeout=90)


def _login(at, portal, user):
    at.session_state["auth_view"] = f"login_{portal}"
    at.run()
    at.text_input[0].input(user)
    at.text_input[1].input(PW)
    at.button[0].click().run()  # form submit
    assert not at.exception, at.exception
    return at


def test_landing(db_path):
    at = app(); at.run()
    assert not at.exception
    labels = [b.label for b in at.button]
    assert "Continue as Driver / Vehicle Owner" in labels and "Continue as Engineer" in labels


def test_wrong_password_shows_error(db_path):
    at = app(); at.session_state["auth_view"] = "login_driver"; at.run()
    at.text_input[0].input("alice"); at.text_input[1].input("nope-nope-1"); at.button[0].click().run()
    assert any("Invalid" in e.value for e in at.error)


def test_driver_cannot_use_engineer_login(db_path):
    at = app(); at.session_state["auth_view"] = "login_engineer"; at.run()
    at.text_input[0].input("alice"); at.text_input[1].input(PW); at.button[0].click().run()
    assert any("not authorised" in e.value for e in at.error)
    assert "vg_token" not in at.session_state


@pytest.mark.parametrize("page", ["Overview", "Assistant", "Health Score", "Early Warnings", "Efficiency", "What-If",
                                  "Event History", "Maintenance", "Reports", "Settings & Privacy"])
def test_driver_pages(db_path, page):
    at = _login(app(), "driver", "alice")
    at.session_state["drv_page"] = page
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value


@pytest.mark.parametrize("page", ["Dashboard", "Event Detective", "Technical Query", "Anomaly Lab", "Investigations",
                                  "What-If Lab", "Data Quality", "Data Ingestion", "Diagnostic Reports", "Benchmarks", "Account"])
def test_engineer_pages(db_path, page):
    at = _login(app(), "engineer", "eng.chen")
    at.session_state["eng_page"] = page
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value


@pytest.mark.parametrize("page", ["Users", "Vehicle access", "Audit log", "Account"])
def test_admin_pages(db_path, page):
    at = _login(app(), "engineer", "admin")
    at.session_state["adm_page"] = page
    at.run()
    assert not at.exception, at.exception


def test_driver_session_tampering_cannot_open_engineer_portal(db_path):
    at = _login(app(), "driver", "alice")
    at.session_state["eng_page"] = "Investigations"
    at.session_state["role"] = "engineer"
    at.session_state["eng_vehicle"] = "SYN-EV-002"
    at.run()
    assert not at.exception
    assert at.title[0].value != "Investigations"
    assert all("Engineer portal" not in c.value for c in at.caption)


def test_driver_vehicle_selector_only_offers_owned_vehicles(db_path):
    # Server-side denial of foreign vehicle ids is covered in test_authz; here we check the UI never offers them.
    at = _login(app(), "driver", "alice")
    assert at.selectbox(key="drv_vehicle").options == ["Blue Commuter · SYN-EV-001"]


def test_forged_token_shows_landing(db_path):
    at = app(); at.session_state["vg_token"] = "forged"; at.run()
    assert "Continue as Engineer" in [b.label for b in at.button]


def test_driver_asks_question(db_path):
    at = _login(app(), "driver", "alice")
    at.session_state["drv_page"] = "Assistant"; at.run()
    at.button(key="dsq0").click().run()
    assert not at.exception
    assert any("range" in m.value for m in at.markdown)


def test_logout(db_path):
    at = _login(app(), "driver", "alice")
    at.button(key="logout").click().run()
    assert "Continue as Engineer" in [b.label for b in at.button]
