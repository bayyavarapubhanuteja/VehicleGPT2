"""Vehicle data collection layer.

Every source adapter turns raw automotive data into normalised `Signal`s (timestamp, vehicle, name, value, unit,
source reference). The `Collector` keeps a live ring buffer for real-time queries and aggregates signals into the
historical SQLite store (10-minute telemetry rows, diagnostic events, ADAS events) — the same tables every
analytics/query module already reads. Sources:

  can_source.py   CAN / CAN-FD frames decoded with a DBC (cantools; python-can virtual bus or SocketCAN)
  someip.py       SOME/IP event notifications over UDP (header encode/decode per AUTOSAR PRS)
  vhal.py         Android Automotive Vehicle HAL property dumps (standard VehicleProperty ids)
  qnx_slog.py     QNX slog2info text logs  -> diagnostic events
  diag_log.py     UDS/DoIP-style DTC report logs -> diagnostic events
  camera_meta.py  Camera health metadata (blockage %, fps) — no images are stored (privacy by design)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Signal:
    ts: str            # ISO-8601
    vehicle_id: str
    name: str          # canonical name, e.g. "battery_temp_c"
    value: float | str
    unit: str
    source: str        # can | canfd | someip | vhal | qnx_slog | diag | camera
    ref: str           # raw reference, e.g. "CAN 0x101 BMS_PackTemp"


@dataclass
class Event:
    ts: str
    vehicle_id: str
    kind: str          # dtc | adas
    fields: dict = field(default_factory=dict)
    source: str = ""
    ref: str = ""


_EPOCH0 = __import__("datetime").datetime(1970, 1, 1)


def to_epoch(ts) -> float:
    """Naive vehicle-local timestamp -> seconds (timezone-free, avoids host TZ shifts)."""
    import pandas as pd
    return (pd.Timestamp(ts).to_pydatetime().replace(tzinfo=None) - _EPOCH0).total_seconds()


def from_epoch(e: float) -> str:
    from datetime import timedelta
    return (_EPOCH0 + timedelta(seconds=float(e))).isoformat(timespec="seconds")
