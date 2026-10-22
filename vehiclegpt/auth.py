"""Authentication: Argon2id password hashing, lockout, DB-backed sessions, password reset.

Security notes
* Passwords are hashed with Argon2id (argon2-cffi); plaintext is never stored or logged.
* Session tokens are 256-bit random values; only their SHA-256 hash is stored. The role is always
  re-read from the database on each request — nothing in the browser session can elevate privileges.
* Public registration can only create *driver* accounts. Engineer/admin accounts are provisioned by an
  admin (services.provision_user) or the CLI (scripts/manage_users.py).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError

from . import audit

_ph = PasswordHasher()  # Argon2id defaults
SESSION_HOURS = 8
MAX_FAILED = 5
LOCK_MINUTES = 15
RESET_MINUTES = 30
ROLES = ("driver", "engineer", "admin")
PORTAL_ROLES = {"driver": ("driver",), "engineer": ("engineer", "admin")}
_DUMMY_HASH = _ph.hash("dummy-password-for-timing")


class AuthError(Exception):
    """User-facing authentication error (message is safe to display)."""


def env() -> str:
    return os.environ.get("VEHICLEGPT_ENV", "development")


def _now() -> datetime:
    return datetime.now()


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------- validation ----------------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


def validate_password(pw: str) -> list[str]:
    errs = []
    if len(pw) < 10:
        errs.append("at least 10 characters")
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        errs.append("both letters and digits")
    if len(pw) > 128:
        errs.append("at most 128 characters")
    return errs


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hash_: str, pw: str) -> bool:
    try:
        return _ph.verify(hash_, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def public_user(row) -> dict:
    d = dict(row)
    d.pop("password_hash", None)
    return d


# ---------------- users ----------------

def create_user(conn, username: str, email: str, display_name: str, password: str, role: str,
                actor_id: int | None = None, must_change: bool = False) -> int:
    username, email, display_name = username.strip(), email.strip().lower(), display_name.strip()
    if role not in ROLES:
        raise AuthError("Invalid role.")
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 3–32 characters: letters, digits, '.', '_' or '-'.")
    if not EMAIL_RE.match(email):
        raise AuthError("Please enter a valid email address.")
    if not display_name or len(display_name) > 80:
        raise AuthError("Display name is required (max 80 characters).")
    errs = validate_password(password)
    if errs:
        raise AuthError("Password must contain " + ", ".join(errs) + ".")
    if conn.execute("SELECT 1 FROM users WHERE username = ? OR email = ?", (username, email)).fetchone():
        raise AuthError("That username or email is already registered.")
    now = _iso(_now())
    cur = conn.execute(
        "INSERT INTO users (username, email, display_name, password_hash, role, status, must_change_password,"
        " created_at, updated_at) VALUES (?,?,?,?,?, 'active', ?, ?, ?)",
        (username, email, display_name, hash_password(password), role, int(must_change), now, now))
    conn.commit()
    audit.log(conn, actor_id or cur.lastrowid, "user.create", "user", cur.lastrowid, f"role={role} username={username}")
    return cur.lastrowid


def authenticate(conn, identifier: str, password: str, portal: str) -> dict:
    """Return the public user dict or raise AuthError with a non-enumerating message."""
    ident = (identifier or "").strip()
    if not ident or not password:
        raise AuthError("Enter your username/email and password.")
    row = conn.execute("SELECT * FROM users WHERE username = ? OR email = ?", (ident, ident.lower())).fetchone()
    generic = AuthError("Invalid username/email or password.")
    if row is None:
        verify_password(_DUMMY_HASH, password)  # equalise timing
        audit.log(conn, None, "auth.login_failed", "user", None, "unknown identifier")
        raise generic
    if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > _now():
        audit.log(conn, row["id"], "auth.login_locked", "user", row["id"], "")
        raise AuthError(f"Account temporarily locked after repeated failed attempts. Try again after {row['locked_until'][11:16]}.")
    if not verify_password(row["password_hash"], password):
        fails = row["failed_attempts"] + 1
        lock = _iso(_now() + timedelta(minutes=LOCK_MINUTES)) if fails >= MAX_FAILED else None
        conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                     (0 if lock else fails, lock, row["id"]))
        conn.commit()
        audit.log(conn, row["id"], "auth.login_failed", "user", row["id"], f"attempt={fails}")
        raise generic
    if row["status"] != "active":
        raise AuthError("This account is disabled. Contact your administrator.")
    if row["role"] not in PORTAL_ROLES[portal]:
        audit.log(conn, row["id"], "auth.wrong_portal", "user", row["id"], f"portal={portal}")
        raise AuthError("This account is not authorised for the " + ("Engineer" if portal == "engineer" else "Driver") +
                        " portal. Please use the other login.")
    if _ph.check_needs_rehash(row["password_hash"]):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), row["id"]))
    conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (row["id"],))
    conn.commit()
    audit.log(conn, row["id"], "auth.login", "user", row["id"], f"portal={portal}")
    return public_user(conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone())


def change_password(conn, user_id: int, old: str, new: str) -> None:
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not verify_password(row["password_hash"], old):
        raise AuthError("Current password is incorrect.")
    errs = validate_password(new)
    if errs:
        raise AuthError("New password must contain " + ", ".join(errs) + ".")
    if old == new:
        raise AuthError("New password must differ from the current one.")
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
                 (hash_password(new), _iso(_now()), user_id))
    conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    audit.log(conn, user_id, "auth.password_changed", "user", user_id, "all sessions revoked")


# ---------------- sessions ----------------

def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,0)",
                 (_sha(token), user_id, _iso(now), _iso(now + timedelta(hours=SESSION_HOURS))))
    conn.commit()
    return token


def validate_session(conn, token: str | None) -> dict | None:
    if not token or not isinstance(token, str) or len(token) > 100:
        return None
    r = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.revoked = 0 "
        "AND s.expires_at > ? AND u.status = 'active'", (_sha(token), _iso(_now()))).fetchone()
    return public_user(r) if r else None


def logout(conn, token: str | None) -> None:
    if token:
        r = conn.execute("SELECT user_id FROM sessions WHERE token_hash = ?", (_sha(token),)).fetchone()
        conn.execute("UPDATE sessions SET revoked = 1 WHERE token_hash = ?", (_sha(token),))
        conn.commit()
        if r:
            audit.log(conn, r[0], "auth.logout", "user", r[0], "")


# ---------------- password reset ----------------

def request_reset(conn, identifier: str, actor_id: int | None = None) -> str | None:
    """Create a single-use reset token (30 min). Returns the token, or None if no such account.
    This offline prototype has no email channel: the caller decides how the token is delivered
    (dev mode: shown on screen; admin: handed over out-of-band)."""
    row = conn.execute("SELECT id FROM users WHERE (username = ? OR email = ?) AND status = 'active'",
                       (identifier.strip(), identifier.strip().lower())).fetchone()
    if not row:
        return None
    token = secrets.token_urlsafe(16)
    conn.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (row["id"],))
    conn.execute("INSERT INTO password_resets (user_id, token_hash, expires_at, created_by, created_at) VALUES (?,?,?,?,?)",
                 (row["id"], _sha(token), _iso(_now() + timedelta(minutes=RESET_MINUTES)), actor_id, _iso(_now())))
    conn.commit()
    audit.log(conn, actor_id or row["id"], "auth.reset_requested", "user", row["id"], "")
    return token


def reset_password(conn, token: str, new: str) -> None:
    r = conn.execute("SELECT * FROM password_resets WHERE token_hash = ? AND used = 0 AND expires_at > ?",
                     (_sha((token or "").strip()), _iso(_now()))).fetchone()
    if not r:
        raise AuthError("Reset code is invalid or has expired.")
    errs = validate_password(new)
    if errs:
        raise AuthError("New password must contain " + ", ".join(errs) + ".")
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0, failed_attempts = 0, locked_until = NULL,"
                 " updated_at = ? WHERE id = ?", (hash_password(new), _iso(_now()), r["user_id"]))
    conn.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (r["id"],))
    conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (r["user_id"],))
    conn.commit()
    audit.log(conn, r["user_id"], "auth.password_reset", "user", r["user_id"], "")


# ---------------- development seed ----------------

DEMO_USERS = [
    # username, email, display, role, vehicles(access)
    ("admin", "admin@vehiclegpt.local", "Fleet Administrator", "admin", []),
    ("alice", "alice@example.local", "Alice Driver", "driver", [("SYN-EV-001", "owner")]),
    ("bob", "bob@example.local", "Bob Driver", "driver", [("SYN-EV-002", "owner")]),
    ("eng.chen", "chen@vehiclegpt.local", "Dr. Mei Chen", "engineer", [("SYN-EV-001", "engineer"), ("SYN-EV-002", "engineer")]),
    ("eng.rao", "rao@vehiclegpt.local", "Arjun Rao", "engineer", [("SYN-EV-003", "engineer")]),
]
DEMO_PASSWORD = os.environ.get("VEHICLEGPT_DEMO_PASSWORD", "Demo-Pass-2026")


def seed_demo_users(conn) -> bool:
    """Create documented demo accounts — development only. Refuses in production."""
    if env() == "production":
        return False
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return False
    vids = {r[0] for r in conn.execute("SELECT vehicle_id FROM vehicles")}
    admin_id = None
    for u, e, d, role, vehicles in DEMO_USERS:
        uid = create_user(conn, u, e, d, DEMO_PASSWORD, role, admin_id)
        admin_id = admin_id or uid
        for vid, access in vehicles:
            if vid in vids:
                conn.execute("INSERT OR IGNORE INTO user_vehicles VALUES (?,?,?,?,?)", (uid, vid, access, admin_id, _iso(_now())))
    conn.commit()
    return True
