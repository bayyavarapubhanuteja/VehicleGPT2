import pytest

from vehiclegpt import auth, services as S
from conftest import PW


def test_passwords_hashed_with_argon2(conn):
    for (h,) in conn.execute("SELECT password_hash FROM users"):
        assert h.startswith("$argon2id$") and PW not in h


def test_login_success_and_wrong_password(conn):
    assert auth.authenticate(conn, "alice", PW, "driver")["role"] == "driver"
    with pytest.raises(auth.AuthError, match="Invalid"):
        auth.authenticate(conn, "alice", "wrong-password-1", "driver")
    with pytest.raises(auth.AuthError, match="Invalid"):
        auth.authenticate(conn, "nobody", PW, "driver")  # same message: no user enumeration


def test_login_by_email(conn):
    assert auth.authenticate(conn, "ALICE@example.local", PW, "driver")["username"] == "alice"


def test_portal_separation(conn):
    with pytest.raises(auth.AuthError, match="not authorised"):
        auth.authenticate(conn, "alice", PW, "engineer")
    with pytest.raises(auth.AuthError, match="not authorised"):
        auth.authenticate(conn, "eng.chen", PW, "driver")


def test_lockout(conn):
    auth.create_user(conn, "lockme", "lock@x.local", "Lock", "Abcdefgh123", "driver")
    for _ in range(auth.MAX_FAILED):
        with pytest.raises(auth.AuthError):
            auth.authenticate(conn, "lockme", "bad-password-9", "driver")
    with pytest.raises(auth.AuthError, match="locked"):
        auth.authenticate(conn, "lockme", "Abcdefgh123", "driver")


def test_password_policy_and_validation(conn):
    for pw in ["short1", "allletterslong", "1234567890123"]:
        with pytest.raises(auth.AuthError):
            auth.create_user(conn, f"u{len(pw)}x", f"u{len(pw)}@x.local", "U", pw, "driver")
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, "bad name!", "b@x.local", "B", "Abcdefgh123", "driver")
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, "alice", "new@x.local", "dup", "Abcdefgh123", "driver")


def test_sessions(conn, alice):
    tok = auth.create_session(conn, alice["id"])
    assert auth.validate_session(conn, tok)["id"] == alice["id"]
    assert auth.validate_session(conn, "forged-token") is None
    assert auth.validate_session(conn, None) is None
    auth.logout(conn, tok)
    assert auth.validate_session(conn, tok) is None
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (tok,)).fetchone()[0] == 0  # only hash stored


def test_password_reset_single_use(conn):
    auth.create_user(conn, "resetme", "reset@x.local", "R", "Abcdefgh123", "driver")
    tok = auth.request_reset(conn, "resetme")
    auth.reset_password(conn, tok, "Newpassword456")
    assert auth.authenticate(conn, "resetme", "Newpassword456", "driver")
    with pytest.raises(auth.AuthError):
        auth.reset_password(conn, tok, "Another789xyz")
    assert auth.request_reset(conn, "ghost") is None


def test_change_password_revokes_sessions(conn):
    uid = auth.create_user(conn, "chg", "chg@x.local", "C", "Abcdefgh123", "driver")
    tok = auth.create_session(conn, uid)
    with pytest.raises(auth.AuthError):
        auth.change_password(conn, uid, "wrong", "Newpassword456")
    auth.change_password(conn, uid, "Abcdefgh123", "Newpassword456")
    assert auth.validate_session(conn, tok) is None


def test_public_registration_is_driver_only_and_needs_pairing(conn):
    with pytest.raises(auth.AuthError, match="pairing"):
        S.register_driver(conn, "newd", "newd@x.local", "N", "Abcdefgh123", "SYNVIN00000000003", "WRONG")
    with pytest.raises(auth.AuthError, match="already registered"):
        S.register_driver(conn, "newd", "newd@x.local", "N", "Abcdefgh123", "SYNVIN00000000001", "PAIR-001-DEMO")
    uid = S.register_driver(conn, "newd", "newd@x.local", "N", "Abcdefgh123", "SYNVIN00000000003", "PAIR-003-DEMO")
    assert conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()[0] == "driver"


def test_no_demo_users_in_production(tmp_path, monkeypatch):
    from vehiclegpt import db
    monkeypatch.setenv("VEHICLEGPT_ENV", "production")
    c = db.connect(tmp_path / "p.db")
    db.init_schema(c)
    assert auth.seed_demo_users(c) is False
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_audit_has_no_secrets(conn):
    for (d,) in conn.execute("SELECT detail FROM audit_log"):
        assert PW not in (d or "")
