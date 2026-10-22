"""Investigation cases, notes, candidate explanations and investigation reports (engineer workspace).

A candidate explanation can only become 'confirmed' through an explicit engineer review with a written
justification and at least one linked evidence record. Nothing is auto-confirmed."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from . import detective

STATUSES = ["open", "investigating", "resolved", "closed"]
PRIORITIES = ["Low", "Medium", "High"]
HYP_STATUSES = ["candidate", "supported", "rejected", "confirmed"]

RECOMMENDED_CHECKS = {
    "battery": ["Download BMS cell-temperature logs for the event window", "Inspect battery coolant level and pump operation",
                "Review charger session logs (power, duration, inlet temperature)", "Check pack thermal sensor calibration"],
    "thermal": ["Check coolant level and flow (pump current, flow sensor)", "Inspect radiator/fan operation",
                "Verify motor temperature sensor against a reference", "Inspect for coolant leaks"],
    "tyres": ["Measure all tyre pressures cold", "Inspect tyres/valves for leaks", "Verify TPMS sensor batteries"],
    "data": ["Verify logger configuration (units, sampling interval)", "Inspect sensor connector and wiring",
             "Check telematics connectivity and buffer"],
    "energy": ["Check tyre pressures", "Review HVAC usage logs", "Compare route/speed profile against baseline"],
}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def create(conn, vehicle_id: str, title: str, created_by: int, priority: str = "Medium", event_key: str | None = None,
           summary: str = "", assigned_to: int | None = None) -> int:
    title = (title or "").strip()
    if not title or len(title) > 150:
        raise ValueError("Case title is required (max 150 characters).")
    if priority not in PRIORITIES:
        raise ValueError("Invalid priority.")
    cur = conn.execute(
        "INSERT INTO cases (vehicle_id, title, status, priority, event_key, summary, created_by, assigned_to, created_at, updated_at)"
        " VALUES (?,?, 'open', ?,?,?,?,?,?,?)",
        (vehicle_id, title, priority, event_key, (summary or "")[:4000], created_by, assigned_to, _now(), _now()))
    cid = cur.lastrowid
    if event_key:
        inv = detective.investigate(conn, event_key)
        for h in inv["possible_explanations"]:
            conn.execute("INSERT INTO case_hypotheses (case_id, text, evidence_strength, evidence_json, status, updated_at)"
                         " VALUES (?,?,?,?, 'candidate', ?)", (cid, h["text"], h["support"], json.dumps(h["evidence"]), _now()))
    conn.commit()
    return cid


def get(conn, cid: int):
    return conn.execute("SELECT * FROM cases WHERE id = ?", (cid,)).fetchone()


def list_cases(conn, vehicle_ids: list[str]) -> pd.DataFrame:
    if not vehicle_ids:
        return pd.DataFrame(columns=["id", "vehicle_id", "title", "status", "priority", "assignee", "created_at", "updated_at"])
    q = (f"SELECT c.*, u.display_name AS assignee, cb.display_name AS creator FROM cases c "
         f"LEFT JOIN users u ON u.id = c.assigned_to LEFT JOIN users cb ON cb.id = c.created_by "
         f"WHERE c.vehicle_id IN ({','.join('?' * len(vehicle_ids))}) ORDER BY c.updated_at DESC")
    return pd.read_sql_query(q, conn, params=vehicle_ids)


def update(conn, cid: int, status: str | None = None, assigned_to: int | None = -1, resolution: str | None = None,
           priority: str | None = None) -> None:
    c = get(conn, cid)
    if status is not None:
        if status not in STATUSES:
            raise ValueError("Invalid status.")
        res = resolution if resolution is not None else c["resolution"]
        if status in ("resolved", "closed") and not (res and len(res.strip()) >= 15):
            raise ValueError("A resolution summary (≥ 15 characters) is required to resolve or close a case.")
        conn.execute("UPDATE cases SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), cid))
    if resolution is not None:
        conn.execute("UPDATE cases SET resolution = ?, updated_at = ? WHERE id = ?", (resolution[:4000], _now(), cid))
    if assigned_to != -1:
        conn.execute("UPDATE cases SET assigned_to = ?, updated_at = ? WHERE id = ?", (assigned_to, _now(), cid))
    if priority is not None:
        if priority not in PRIORITIES:
            raise ValueError("Invalid priority.")
        conn.execute("UPDATE cases SET priority = ?, updated_at = ? WHERE id = ?", (priority, _now(), cid))
    conn.commit()


def add_note(conn, cid: int, author_id: int, body: str) -> int:
    body = (body or "").strip()
    if not body or len(body) > 4000:
        raise ValueError("Note must be 1–4000 characters.")
    cur = conn.execute("INSERT INTO case_notes (case_id, author_id, body, created_at) VALUES (?,?,?,?)", (cid, author_id, body, _now()))
    conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (_now(), cid))
    conn.commit()
    return cur.lastrowid


def notes(conn, cid: int) -> pd.DataFrame:
    return pd.read_sql_query("SELECT n.id, n.created_at, u.display_name AS author, n.body FROM case_notes n "
                             "JOIN users u ON u.id = n.author_id WHERE n.case_id = ? ORDER BY n.id", conn, params=(cid,))


def hypotheses(conn, cid: int) -> pd.DataFrame:
    return pd.read_sql_query("SELECT h.*, u.display_name AS reviewer FROM case_hypotheses h LEFT JOIN users u ON u.id = h.reviewed_by "
                             "WHERE h.case_id = ? ORDER BY h.id", conn, params=(cid,))


def add_hypothesis(conn, cid: int, text: str, strength: str = "weak", evidence: list | None = None) -> int:
    if not text or len(text) > 1000:
        raise ValueError("Hypothesis text must be 1–1000 characters.")
    if strength not in ("weak", "moderate", "strong"):
        raise ValueError("Invalid evidence strength.")
    cur = conn.execute("INSERT INTO case_hypotheses (case_id, text, evidence_strength, evidence_json, status, updated_at)"
                       " VALUES (?,?,?,?, 'candidate', ?)", (cid, text.strip(), strength, json.dumps(evidence or []), _now()))
    conn.commit()
    return cur.lastrowid


def review_hypothesis(conn, hid: int, reviewer_id: int, status: str, note: str) -> None:
    if status not in HYP_STATUSES:
        raise ValueError("Invalid status.")
    h = conn.execute("SELECT * FROM case_hypotheses WHERE id = ?", (hid,)).fetchone()
    note = (note or "").strip()
    if status == "confirmed":
        if len(note) < 20:
            raise ValueError("Confirming a root cause requires a written justification (≥ 20 characters).")
        if not json.loads(h["evidence_json"]):
            raise ValueError("Cannot confirm a root cause without at least one linked evidence record.")
    conn.execute("UPDATE case_hypotheses SET status = ?, reviewed_by = ?, review_note = ?, updated_at = ? WHERE id = ?",
                 (status, reviewer_id, note[:2000], _now(), hid))
    statuses = [r[0] for r in conn.execute("SELECT status FROM case_hypotheses WHERE case_id = ?", (h["case_id"],))]
    rc = "confirmed" if "confirmed" in statuses else "candidate" if any(s in ("candidate", "supported") for s in statuses) else "none"
    conn.execute("UPDATE cases SET root_cause_status = ?, updated_at = ? WHERE id = ?", (rc, _now(), h["case_id"]))
    conn.commit()


def investigation_report(conn, cid: int) -> dict:
    c = dict(get(conn, cid))
    inv = detective.investigate(conn, c["event_key"]) if c["event_key"] else None
    hyps = hypotheses(conn, cid)
    missing = []
    if inv:
        missing += [l for l in inv["limitations"] if "not causation" not in l]
        sub = inv["event"]["subsystem"]
    else:
        sub = "data"
        missing.append("No triggering event linked to this case; timeline unavailable.")
    missing.append("No workshop inspection results recorded for this case." if not len(notes(conn, cid)) else
                   "Confirm that physical inspection results are captured in notes.")
    return {
        "case": c, "investigation": inv, "hypotheses": hyps.to_dict("records"), "notes": notes(conn, cid).to_dict("records"),
        "missing_information": missing, "recommended_checks": RECOMMENDED_CHECKS.get(sub, RECOMMENDED_CHECKS["data"]),
        "root_cause_statement": ("Root cause CONFIRMED by engineer review." if c["root_cause_status"] == "confirmed" else
                                 "No root cause confirmed. Listed explanations are candidates pending evidence and review."),
    }
