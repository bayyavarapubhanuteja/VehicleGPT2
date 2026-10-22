"""Append-only audit trail. Never pass secrets (passwords, tokens) in `detail`."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

_SENSITIVE = ("password", "token", "secret")


def log(conn, user_id, action: str, target_type: str | None = None, target_id=None, detail: str = "") -> None:
    d = (detail or "")[:500]
    if any(s in d.lower() for s in _SENSITIVE if s not in action):
        d = "[redacted]"
    conn.execute("INSERT INTO audit_log (ts, user_id, action, target_type, target_id, detail) VALUES (?,?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), user_id, action, target_type,
                  None if target_id is None else str(target_id), d))
    conn.commit()


def read(conn, limit: int = 500, target_type: str | None = None, target_ids: list[str] | None = None) -> pd.DataFrame:
    q = ("SELECT a.id, a.ts, u.username, a.action, a.target_type, a.target_id, a.detail FROM audit_log a "
         "LEFT JOIN users u ON u.id = a.user_id WHERE 1=1")
    args: list = []
    if target_type:
        q += " AND a.target_type = ?"; args.append(target_type)
    if target_ids is not None:
        if not target_ids:
            return pd.DataFrame(columns=["id", "ts", "username", "action", "target_type", "target_id", "detail"])
        q += f" AND a.target_id IN ({','.join('?' * len(target_ids))})"; args += [str(t) for t in target_ids]
    q += " ORDER BY a.id DESC LIMIT ?"
    args.append(limit)
    return pd.read_sql_query(q, conn, params=args)
