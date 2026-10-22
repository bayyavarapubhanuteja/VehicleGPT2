"""Camera feed adapter — metadata only.

The assistant never stores or analyses video frames (privacy, storage and compute budget on a 2 GB target). A camera
health publisher (on the ADAS ECU, or a lightweight on-device check) emits per-second metadata records:
{"ts": iso, "camera": "front", "fps": 29.8, "blockage_pct": 12, "exposure": "ok"}. The collector turns sustained
blockage (>= 60 % for >= 30 s) into an ADAS `camera_blocked` event.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import Event, Signal

BLOCK_PCT, BLOCK_SECONDS = 60, 30


def parse(lines, vehicle_id: str) -> tuple[list[Signal], list[Event], int]:
    sigs, events, errors = [], [], 0
    run_start, run_ts = None, None
    last = None
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            ts = datetime.fromisoformat(r["ts"])
            blk = float(r["blockage_pct"])
        except (ValueError, KeyError, TypeError):
            errors += 1
            continue
        sigs.append(Signal(ts.isoformat(timespec="seconds"), vehicle_id, "camera_blockage_pct", blk, "%", "camera",
                           f"CAM {r.get('camera', 'front')} fps={r.get('fps')}"))
        if blk >= BLOCK_PCT:
            run_start = run_start or ts
        else:
            if run_start and last and (last - run_start).total_seconds() >= BLOCK_SECONDS:
                events.append(_ev(vehicle_id, run_start, last, r.get("camera", "front")))
            run_start = None
        last = ts
    if run_start and last and (last - run_start).total_seconds() >= BLOCK_SECONDS:
        events.append(_ev(vehicle_id, run_start, last, "front"))
    return sigs, events, errors


def _ev(vid, a, b, cam):
    return Event(a.isoformat(timespec="seconds"), vid, "adas",
                 {"feature": f"{cam}_camera", "event": "camera_blocked", "reason": "blockage >= 60%",
                  "speed_kmh": None, "duration_s": int((b - a).total_seconds())}, "camera", f"CAM {cam} health stream")
