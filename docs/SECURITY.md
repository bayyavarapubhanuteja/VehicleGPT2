# Security design and known limitations

## Implemented
- **Passwords**: Argon2id (`argon2-cffi`), policy ≥10 chars with letters and digits; transparent rehash; never logged.
- **Login**: identical error for unknown user / wrong password (no enumeration) with equalised hashing time; lockout for
  15 min after 5 failures; disabled accounts rejected; driver and engineer logins are separate and reject the other role.
- **Sessions**: 256-bit random token in Streamlit server-side session state; only its SHA-256 is stored in `sessions`;
  8 h expiry; revoked on logout, password change/reset and account disable.
- **Authorisation**: `vehiclegpt/services.py` is the only data-access path used by the UI. Each call re-reads the user from the
  database (a tampered user dict or session value cannot change the role), checks the role, and checks `user_vehicles`
  for the vehicle, alert, event, case, reminder or record being touched. Foreign ids return nothing or raise `PermissionDenied`.
  Admins manage accounts and access but cannot read vehicle telemetry.
- **Provisioning**: public registration creates **driver** accounts only and requires the vehicle's VIN + pairing code
  (stored as SHA-256). Engineer/admin accounts are created by an admin in the console or with `scripts/manage_users.py`
  (password prompted, forced change at first login).
- **Password reset**: single-use code, 30 min expiry, stored hashed.
- **Data integrity**: parameterised SQL everywhere; input validation on all forms; raw telemetry is never modified by
  analytics (validation only sets `valid`/`validity_note`); simulations are stored separately.
- **Audit**: logins, failures, denials, threshold changes, case/maintenance/alert actions, account changes.
- **Demo accounts**: created only when `VEHICLEGPT_ENV` ≠ `production`.

## Known limitations (must be addressed before real deployment)
1. **No email delivery** — reset codes are shown on screen in development mode, or issued by an admin out-of-band.
2. **Transport security** — Streamlit serves plain HTTP; deploy behind a TLS reverse proxy.
3. **No MFA**, no CSRF tokens beyond Streamlit's websocket model, no rate limiting beyond per-account lockout.
4. **SQLite file is not encrypted at rest**; protect it with OS file permissions / disk encryption.
5. **Single-process SQLite** — not designed for many concurrent writers.
6. Session tokens live in Streamlit server memory per browser tab; closing the tab ends the UI session (the DB session
   remains valid until expiry or logout).
7. The pairing-code mechanism is a demonstration of possession-based vehicle claiming, not a production enrolment protocol.
