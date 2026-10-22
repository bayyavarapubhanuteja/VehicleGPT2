"""Minimal SOME/IP (AUTOSAR PRS_SOMEIPProtocol) event encoding/decoding over UDP.

Header (16 bytes, big-endian): Service ID(2) Method/Event ID(2) Length(4) Client ID(2) Session ID(2)
Protocol Ver(1)=0x01 Interface Ver(1) Message Type(1)=0x02 NOTIFICATION Return Code(1)=0x00.
Service 0x4C01 "VehicleState" publishes event 0x8001 with payload: >d (epoch) f speed f soc f batt_temp f motor_temp
f ambient f power. Service discovery (SOME/IP-SD) is out of scope; endpoints are configured statically.
"""
from __future__ import annotations

import socket
import struct
from datetime import datetime

from . import Signal, from_epoch

SERVICE_ID, EVENT_ID, IFACE_VER = 0x4C01, 0x8001, 1
HDR = struct.Struct(">HHIHHBBBB")
PAYLOAD = struct.Struct(">dffffff")
FIELDS = [("speed_kmh", "km/h"), ("soc_pct", "%"), ("battery_temp_c", "°C"), ("motor_temp_c", "°C"),
          ("ambient_temp_c", "°C"), ("power_kw", "kW")]


def encode(epoch: float, values: dict, session: int = 1) -> bytes:
    payload = PAYLOAD.pack(epoch, *[float(values.get(k) or 0.0) for k, _ in FIELDS])
    return HDR.pack(SERVICE_ID, EVENT_ID, len(payload) + 8, 0x0000, session & 0xFFFF, 0x01, IFACE_VER, 0x02, 0x00) + payload


def decode(datagram: bytes, vehicle_id: str) -> list[Signal]:
    if len(datagram) < HDR.size:
        raise ValueError("short SOME/IP datagram")
    sid, eid, length, _cid, sess, proto, _iv, mtype, rc = HDR.unpack_from(datagram)
    if proto != 0x01 or mtype not in (0x02, 0x00) or rc != 0:
        raise ValueError("unsupported SOME/IP message")
    if (sid, eid) != (SERVICE_ID, EVENT_ID) or length != len(datagram) - 8:
        raise ValueError("unknown service/event or bad length")
    epoch, *vals = PAYLOAD.unpack_from(datagram, HDR.size)
    ts = from_epoch(epoch)
    return [Signal(ts, vehicle_id, k, round(v, 3), u, "someip", f"SOMEIP 0x{sid:04X}.0x{eid:04X} s{sess}")
            for (k, u), v in zip(FIELDS, vals)]


class UdpEndpoint:
    """Loopback UDP endpoint (unicast). Bind to 127.0.0.1 by default — no external network exposure."""

    def __init__(self, host: str = "127.0.0.1", port: int = 30490):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.5)
        self.addr = self.sock.getsockname()

    def send(self, datagram: bytes, addr):
        self.sock.sendto(datagram, addr)

    def recv(self) -> bytes | None:
        try:
            return self.sock.recvfrom(1500)[0]
        except socket.timeout:
            return None

    def close(self):
        self.sock.close()
