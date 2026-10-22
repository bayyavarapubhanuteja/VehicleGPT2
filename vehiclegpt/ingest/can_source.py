"""CAN / CAN-FD collection. Frames are decoded with the project DBC via cantools.

On Linux/automotive targets use SocketCAN (`can.Bus(interface="socketcan", channel="can0", fd=True)`); for demos and
tests the python-can in-process `virtual` interface is used, so the same code path runs everywhere.
"""
from __future__ import annotations

from pathlib import Path

import can
import cantools

from . import Signal

DBC_PATH = Path(__file__).with_name("vehiclegpt.dbc")
_db = cantools.database.load_file(str(DBC_PATH))

# DBC signal -> canonical telemetry column
SIGNAL_MAP = {
    "BMS_SOC": ("soc_pct", "%"), "BMS_PackTemp": ("battery_temp_c", "°C"), "BMS_PackPower": ("power_kw", "kW"),
    "BMS_ChargeState": ("state", ""), "MCU_VehicleSpeed": ("speed_kmh", "km/h"), "MCU_MotorTemp": ("motor_temp_c", "°C"),
    "MCU_Odometer": ("odometer_km", "km"), "BCM_AmbientTemp": ("ambient_temp_c", "°C"),
    "BCM_TirePressureMin": ("tire_pressure_kpa", "kPa"), "ADAS_Feature": ("adas_feature", ""), "ADAS_State": ("adas_state", ""),
    "ADAS_DisengageReason": ("adas_reason", ""), "ADAS_CameraBlockage": ("camera_blockage_pct", "%"),
}
BY_ID = {(m.frame_id, m.is_extended_frame): m for m in _db.messages}
FD_IDS = {_db.get_message_by_name("ADAS_Status_FD").frame_id}
STATE_FROM_CHARGE = {"idle": "parked", "driving": "driving", "ac_charging": "charging", "dc_charging": "charging"}


def dbc():
    return _db


def encode_row(row: dict) -> list[can.Message]:
    """Encode one telemetry sample into the CAN frames a real vehicle bus would carry (used by the replay simulator)."""
    cs = "driving" if row["state"] == "driving" else ("dc_charging" if (row.get("power_kw") or 0) < -50 else
                                                       "ac_charging" if row["state"] == "charging" else "idle")
    msgs = []
    for name, sig in [("BMS_Status", {"BMS_SOC": row.get("soc_pct") or 0, "BMS_PackTemp": row["battery_temp_c"],
                                      "BMS_PackPower": row["power_kw"], "BMS_ChargeState": cs}),
                      ("MCU_Status", {"MCU_VehicleSpeed": row["speed_kmh"], "MCU_MotorTemp": row["motor_temp_c"],
                                      "MCU_Odometer": row["odometer_km"]}),
                      ("BCM_Env", {"BCM_AmbientTemp": row["ambient_temp_c"], "BCM_TirePressureMin": row["tire_pressure_kpa"]})]:
        m = _db.get_message_by_name(name)
        msgs.append(can.Message(arbitration_id=m.frame_id, data=m.encode(sig, strict=False), is_extended_id=m.is_extended_frame,
                                timestamp=row["_epoch"]))
    return msgs


def encode_adas(feature: str, state: str, reason: str, blockage: float, epoch: float) -> can.Message:
    m = _db.get_message_by_name("ADAS_Status_FD")
    data = m.encode({"ADAS_Feature": feature, "ADAS_State": state, "ADAS_DisengageReason": reason,
                     "ADAS_CameraBlockage": blockage}, strict=False)
    return can.Message(arbitration_id=m.frame_id, data=data, is_extended_id=True, is_fd=True, timestamp=epoch)


def decode(msg: can.Message, vehicle_id: str, ts_iso: str) -> list[Signal]:
    m = BY_ID.get((msg.arbitration_id, bool(msg.is_extended_id)))
    if m is None:
        return []
    vals = m.decode(bytes(msg.data), decode_choices=True)
    src = "canfd" if msg.is_fd else "can"
    out = []
    for k, v in vals.items():
        if k not in SIGNAL_MAP:
            continue
        name, unit = SIGNAL_MAP[k]
        if name == "state":
            v = STATE_FROM_CHARGE.get(str(v), "parked")
        elif hasattr(v, "name"):
            v = v.name
        out.append(Signal(ts_iso, vehicle_id, name, v if isinstance(v, str) else round(float(v), 3), unit, src,
                          f"{src.upper()} 0x{msg.arbitration_id:X} {k}"))
    return out


def open_bus(channel: str = "vgpt0", interface: str = "virtual") -> can.BusABC:
    return can.Bus(interface=interface, channel=channel, fd=True, receive_own_messages=False)


class ReadOnlyBus:
    """Receive-only wrapper used by the collector: VehicleGPT never transmits on a vehicle bus.

    On target hardware this is backed by (1) a listen-only CAN controller mode (`ip link set can0 type can ... listen-only on`),
    (2) a gateway ECU that forwards a mirrored copy of selected frames one-way to the infotainment/AI domain, and
    (3) hypervisor isolation of the AI VM from safety-critical domains (see docs/EDGE_DEPLOYMENT.md)."""

    def __init__(self, bus: can.BusABC):
        self._bus = bus

    def recv(self, timeout: float | None = None):
        return self._bus.recv(timeout)

    def send(self, *_a, **_k):
        raise PermissionError("VehicleGPT is receive-only: transmitting on a vehicle bus is not permitted.")

    def shutdown(self):
        self._bus.shutdown()
