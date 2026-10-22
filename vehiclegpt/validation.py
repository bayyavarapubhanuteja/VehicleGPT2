"""Ingestion validation: flags implausible and missing readings without altering original values."""
from __future__ import annotations

from . import config


def validate_telemetry(conn) -> dict:
    lo_b = config.DEFAULT_THRESHOLDS["battery_temp_plausible_min_c"]
    hi_b = config.DEFAULT_THRESHOLDS["battery_temp_plausible_max_c"]
    hi_m = config.DEFAULT_THRESHOLDS["motor_temp_plausible_max_c"]
    checks = [
        (f"battery_temp_c < {lo_b} OR battery_temp_c > {hi_b}", "implausible battery_temp_c"),
        (f"motor_temp_c > {hi_m}", "implausible motor_temp_c"),
    ]
    for col, (lo, hi) in config.PLAUSIBLE.items():
        checks.append((f"{col} < {lo} OR {col} > {hi}", f"implausible {col}"))
    flagged = 0
    for cond, note in checks:
        cur = conn.execute(
            f"UPDATE telemetry SET valid = 0, validity_note = COALESCE(validity_note || '; ', '') || ? WHERE {cond}",
            (note,))
        flagged += cur.rowcount
    cur = conn.execute(
        "UPDATE telemetry SET validity_note = COALESCE(validity_note || '; ', '') || 'missing soc_pct' "
        "WHERE soc_pct IS NULL")
    missing = cur.rowcount
    conn.commit()
    return {"implausible_flagged": flagged, "missing_soc": missing}
