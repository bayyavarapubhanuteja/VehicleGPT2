# VehicleGPT 2.0 — Role-Based Vehicle Intelligence Platform

Offline, evidence-backed vehicle intelligence with **two separate portals** (plus an admin console),
built on the VehicleGPT 1.0 engine. Both portals use the same pipeline:

```
telemetry → validation → SQLite → structured retrieval → analytics & detection → verified results → role-specific explanation
```

> ⚠️ Informational prototype on **synthetic data**. Not a safety-certified automotive system; no connection to vehicle
> controls. `SIM-*` diagnostic codes are not real OEM/OBD codes; thresholds are demonstration values.

## Quick start
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```
Open http://localhost:8502. On first launch the database (`data/vehiclegpt.db`), a 3-vehicle synthetic fleet, alerts and
the **development demo accounts** are created. An existing VehicleGPT 1.0 database is migrated in place (see below).

### Demo credentials (local development only)
Password for all: `Demo-Pass-2026` (override with `VEHICLEGPT_DEMO_PASSWORD`).

| Portal | Username | Access |
|---|---|---|
| Driver | `alice` | owns SYN-EV-001 “Blue Commuter” |
| Driver | `bob` | owns SYN-EV-002 “Road Tripper” |
| Engineer | `eng.chen` | SYN-EV-001, SYN-EV-002 |
| Engineer | `eng.rao` | SYN-EV-003 “Depot Van 3” |
| Engineer login | `admin` | administration console (no vehicle data) |

Unclaimed vehicle for testing registration: VIN `SYNVIN00000000003`, pairing code `PAIR-003-DEMO`.
**Before any deployment** set `VEHICLEGPT_ENV=production` (demo accounts are then never created, reset codes are not shown
on screen) and provision real accounts with `scripts/manage_users.py`.

## Features
**Driver portal** — Overview (latest readings clearly labelled *historical/synthetic*, stale-data warning) · Assistant
(plain-language, evidence-backed) · Health Score (explainable 0–100 with factors, trend, completeness, limitations) ·
Early Warnings (severity, evidence, next steps, acknowledge, history) · Efficiency (trends, trips, week-over-week, supported
factors only) · What-If (labelled simulations) · Event History (date/category/severity filters + search) · Maintenance
planner (user schedules vs *generic suggestions*, date/odometer due, recurring, done/delete with confirmation) · Reports
(PDF health summary, CSV) · Settings & Privacy (profile, password, add vehicle by VIN + pairing code, local-storage explanation, activity).

**Engineer portal** — Dashboard (vehicle/date/subsystem/severity filters, anomaly timeline & counts, open cases, recent
investigations) · Advanced Event Detective (±5/15/60 min or custom windows, channel overlay with z-score view, correlations,
observed sequence vs candidate explanations vs confirmed root cause, raw records + CSV, open case) · Technical Query (shows
validated structured query, records, calculations) · Anomaly Lab (thresholds, current-vs-candidate P/R/F1 against labelled
data, rolling statistics, repeated-event analysis, rule versions) · Investigations (cases, assignment to authorised
engineers, notes, candidate explanations, review workflow, audit trail, investigation report) · What-If Lab (up to 4
scenarios, sensitivity sweeps, formulas) · Data Quality monitor (missing, gaps, duplicates, implausible, °F/°C unit errors,
stuck sensors, staleness; read-only) · Diagnostic Reports (PDF with timeline, sensor chart, codes, rules, notes; JSON/CSV) ·
Benchmarks (measured results only; shows *unavailable* when not run).

**Admin console** — provision engineer/driver/admin accounts (temporary password, forced change), enable/disable accounts,
issue reset codes, grant/revoke vehicle access, full audit log.

## Security model
See [docs/SECURITY.md](docs/SECURITY.md) for details and known limitations. In short: Argon2id password hashes, lockout after
5 failures, DB-backed sessions (only token hashes stored, 8 h expiry, revoked on logout/password change/disable), role re-read
from the DB on every request, and **every data operation authorised in `vehiclegpt/services.py`** (role + vehicle assignment),
parameterised SQL throughout, audit log for logins, denials and all changes.

## Database & migration
`vehiclegpt/migrations.py` upgrades a 1.0 database in place: adds `users`, `user_vehicles`, `sessions`, `password_resets`,
`maintenance_reminders`, `cases`, `case_notes`, `case_hypotheses`, `audit_log`, `eval_runs`, `data_transformations`,
`schema_version`, and new columns (`vehicles.nickname/vin/pairing_code_hash/data_mode`, `alerts.rule_version`). Existing
telemetry is never deleted (verified by `tests/test_features.py::test_migration_preserves_v1_database`). Regenerate from
scratch with `.venv/bin/python -m vehiclegpt.data_gen`.

## Tests, benchmarks, offline check
```bash
.venv/bin/python -m pytest -q
```
```bash
.venv/bin/python benchmarks/run_benchmarks.py
```
```bash
.venv/bin/python scripts/offline_check.py
```
140 tests: authentication, hashing, lockout, reset, sessions, portal separation, role & vehicle authorisation (including forged
user objects, foreign record ids, crafted event keys, SQL-injection strings), migration, health score, data quality, new query
intents, maintenance, case workflow, reports, simulations, thresholds, plus headless rendering of every page in all three portals.
Latest measured results: [benchmarks/results.md](benchmarks/results.md).

Offline: after `pip install`, disable networking and run the offline check and `./run.sh`; the sidebar shows the connectivity state.

## Optional local LLM
Same adapter as 1.0 (`vehiclegpt/llm.py`): Ollama on loopback or llama.cpp GGUF. Not installed on the build machine, so LLM
latency/memory is reported as **unavailable**, not estimated.

See also [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).
