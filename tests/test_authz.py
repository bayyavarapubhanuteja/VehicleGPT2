import pytest

from vehiclegpt import services as S, simulator, analytics


def test_vehicle_scoping(conn, alice, bob, chen, rao, admin):
    assert S.vehicles_for(conn, alice) == ["SYN-EV-001"]
    assert S.vehicles_for(conn, bob) == ["SYN-EV-002"]
    assert S.vehicles_for(conn, chen) == ["SYN-EV-001", "SYN-EV-002"]
    assert S.vehicles_for(conn, rao) == ["SYN-EV-003"]
    assert S.vehicles_for(conn, admin) == []


@pytest.mark.parametrize("fn", [
    lambda c, u: S.overview(c, u, "SYN-EV-002"), lambda c, u: S.alerts(c, u, "SYN-EV-002"),
    lambda c, u: S.telemetry(c, u, "SYN-EV-002"), lambda c, u: S.ask(c, u, "SYN-EV-002", "range?"),
    lambda c, u: S.health_score(c, u, "SYN-EV-002"), lambda c, u: S.efficiency(c, u, "SYN-EV-002"),
    lambda c, u: S.records_by_id(c, u, "SYN-EV-002", "telemetry", [1]),
    lambda c, u: S.simulate(c, u, "SYN-EV-002", analytics.last_n_days(c, 7), simulator.Params()),
])
def test_driver_cannot_read_other_vehicle(conn, alice, fn):
    with pytest.raises(S.PermissionDenied):
        fn(conn, alice)


@pytest.mark.parametrize("fn", [
    lambda c, u: S.case_list(c, u), lambda c, u: S.data_quality(c, u, "SYN-EV-001"),
    lambda c, u: S.set_thresholds(c, u, {}), lambda c, u: S.run_detection(c, u),
    lambda c, u: S.evaluate_detection(c, u), lambda c, u: S.case_create(c, u, "SYN-EV-001", "x"),
    lambda c, u: S.fleet_alerts(c, u), lambda c, u: S.admin_users(c, u),
    lambda c, u: S.provision_user(c, u, "x1", "x1@x.local", "X", "Abcdefgh123", "engineer"),
])
def test_driver_cannot_use_engineer_or_admin_functions(conn, alice, fn):
    with pytest.raises(S.PermissionDenied):
        fn(conn, alice)


def test_tampered_user_dict_cannot_escalate(conn, alice):
    forged = dict(alice, role="engineer")
    with pytest.raises(S.PermissionDenied):
        S.case_list(conn, forged)
    with pytest.raises(S.PermissionDenied):
        S.overview(conn, {"id": 99999, "role": "admin"}, "SYN-EV-001")


def test_cross_vehicle_ids_rejected(conn, alice):
    other = conn.execute("SELECT id FROM alerts WHERE vehicle_id='SYN-EV-002' LIMIT 1").fetchone()[0]
    with pytest.raises(S.PermissionDenied):
        S.acknowledge_alert(conn, alice, other)
    dtc2 = conn.execute("SELECT id FROM diagnostic_events WHERE vehicle_id='SYN-EV-002' LIMIT 1").fetchone()[0]
    with pytest.raises(S.PermissionDenied):
        S.investigate(conn, alice, f"dtc:{dtc2}")
    with pytest.raises(S.PermissionDenied):
        S.ask(conn, alice, "SYN-EV-001", "why?", context_event=f"dtc:{dtc2}")
    # records_by_id only returns rows of the authorised vehicle even if foreign ids are passed
    tid = conn.execute("SELECT id FROM telemetry WHERE vehicle_id='SYN-EV-002' LIMIT 1").fetchone()[0]
    assert S.records_by_id(conn, alice, "SYN-EV-001", "telemetry", [tid]).empty


def test_engineer_scoping_on_cases(conn, chen, rao):
    cid = S.case_create(conn, chen, "SYN-EV-002", "Repeated pack heating")
    assert cid in S.case_list(conn, chen)["id"].tolist()
    assert cid not in S.case_list(conn, rao)["id"].tolist()
    with pytest.raises(S.PermissionDenied):
        S.case_get(conn, rao, cid)
    with pytest.raises(S.PermissionDenied):
        S.case_note(conn, rao, cid, "private note")
    with pytest.raises(S.PermissionDenied):
        S.data_quality(conn, chen, "SYN-EV-003")
    with pytest.raises(S.PermissionDenied):  # assignee must be authorised for the vehicle
        S.case_update(conn, chen, cid, assigned_to=conn.execute("SELECT id FROM users WHERE username='eng.rao'").fetchone()[0])


def test_admin_cannot_read_vehicle_data(conn, admin):
    with pytest.raises(S.PermissionDenied):
        S.overview(conn, admin, "SYN-EV-001")
    with pytest.raises(S.PermissionDenied):
        S.case_list(conn, admin)


def test_engineer_cannot_administer(conn, chen):
    with pytest.raises(S.PermissionDenied):
        S.assign_vehicle(conn, chen, 1, "SYN-EV-003")


def test_disabled_account_blocked(conn, admin):
    from vehiclegpt import auth
    uid = S.provision_user(conn, admin, "temp.eng", "temp@x.local", "Temp", "Abcdefgh123", "engineer")
    tok = auth.create_session(conn, uid)
    S.set_user_status(conn, admin, uid, "disabled")
    assert auth.validate_session(conn, tok) is None
    with pytest.raises(S.PermissionDenied):
        S.case_list(conn, {"id": uid})


def test_denials_are_audited(conn, alice):
    n0 = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='authz.denied'").fetchone()[0]
    with pytest.raises(S.PermissionDenied):
        S.overview(conn, alice, "SYN-EV-003")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='authz.denied'").fetchone()[0] == n0 + 1


def test_sql_injection_inputs_are_inert(conn, alice):
    with pytest.raises(S.PermissionDenied):
        S.overview(conn, alice, "SYN-EV-001' OR '1'='1")
    a = S.ask(conn, alice, "SYN-EV-001", "'; DROP TABLE telemetry; --")
    assert conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] > 0 and not a.supported
