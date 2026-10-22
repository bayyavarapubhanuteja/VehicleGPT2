"""Controlled provisioning CLI (local operator only).

  python scripts/manage_users.py create --username eng.lee --email lee@corp.local --name "Lee" --role engineer
  python scripts/manage_users.py grant --username eng.lee --vehicle SYN-EV-003
  python scripts/manage_users.py list
The password is prompted (never passed on the command line). New accounts must change it at first login.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vehiclegpt import auth, db, runtime  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--username", required=True); c.add_argument("--email", required=True)
    c.add_argument("--name", required=True); c.add_argument("--role", choices=auth.ROLES, required=True)
    g = sub.add_parser("grant")
    g.add_argument("--username", required=True); g.add_argument("--vehicle", required=True)
    sub.add_parser("list")
    a = p.parse_args()
    runtime.ensure_database()
    conn = db.connect()
    if a.cmd == "create":
        pw = getpass.getpass("Temporary password: ")
        uid = auth.create_user(conn, a.username, a.email, a.name, pw, a.role, must_change=True)
        print(f"created user #{uid} ({a.role})")
    elif a.cmd == "grant":
        u = conn.execute("SELECT id, role FROM users WHERE username = ?", (a.username,)).fetchone()
        if not u or u["role"] == "admin" or a.vehicle not in db.vehicle_ids(conn):
            sys.exit("unknown user/vehicle or admin account")
        conn.execute("INSERT OR IGNORE INTO user_vehicles VALUES (?,?,?,NULL, datetime('now'))",
                     (u["id"], a.vehicle, "owner" if u["role"] == "driver" else "engineer"))
        conn.commit()
        print("granted")
    else:
        for r in conn.execute("SELECT id, username, role, status FROM users"):
            print(dict(r))


if __name__ == "__main__":
    main()
