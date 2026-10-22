"""OPTIONAL CARLA bridge — NOT exercised in this repository's tests (CARLA is not installed on the build machine).

Maps a CARLA ego vehicle's state to the same CAN frames the replay uses, so driving scenarios (highway, city, ADAS
edge cases, weather) can be generated in CARLA and fed through the identical ingestion path.

Usage (on a machine running CARLA 0.9.x server):
    python -m vehiclegpt.ingest.carla_bridge --host 127.0.0.1 --seconds 600
"""
from __future__ import annotations

import argparse
import math
import time


def run(host: str, port: int, seconds: int, channel: str = "vgpt0", interface: str = "virtual"):
    import carla  # noqa: F401  (optional dependency)

    from . import can_source
    client = carla.Client(host, port)
    world = client.get_world()
    ego = next(a for a in world.get_actors().filter("vehicle.*"))
    bus = can_source.open_bus(channel, interface)
    soc, batt, motor, odo = 80.0, 25.0, 30.0, 0.0
    t_end = time.time() + seconds
    while time.time() < t_end:
        v = ego.get_velocity()
        speed = 3.6 * math.sqrt(v.x ** 2 + v.y ** 2 + v.z ** 2)
        weather = world.get_weather()
        amb = 20 - weather.precipitation / 10
        power = 0.15 * speed + 0.0004 * speed ** 2 * speed / 10
        soc -= power / 3600 / 60 * 100
        odo += speed / 3600
        motor += ((amb + 22 + 0.22 * speed) - motor) * 0.01
        batt += ((amb + 3 + 0.04 * power) - batt) * 0.005
        row = {"state": "driving" if speed > 1 else "parked", "soc_pct": soc, "battery_temp_c": batt, "power_kw": power,
               "speed_kmh": speed, "motor_temp_c": motor, "odometer_km": odo, "ambient_temp_c": amb,
               "tire_pressure_kpa": 240, "_epoch": time.time()}
        for m in can_source.encode_row(row):
            bus.send(m)
        time.sleep(1.0)
    bus.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=2000)
    ap.add_argument("--seconds", type=int, default=600)
    a = ap.parse_args()
    run(a.host, a.port, a.seconds)
