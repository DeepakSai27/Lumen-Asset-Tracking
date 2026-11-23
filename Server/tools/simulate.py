"""Pretend to be one or more checkpoints so the website can be tried without hardware.

    python tools/simulate.py --url http://localhost:8000 --token change-me

Creates three demo employees with fake tags (if they don't exist), then reports
sightings every 5 s. Employees randomly walk in and out and between checkpoints.
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request

CHECKPOINTS = {"10:20:30:40:50:01": "Main entrance", "10:20:30:40:50:02": "Dev room"}
PEOPLE = [("Demo Asha", "R&D", "DE:AD:00:00:00:01"),
          ("Demo Ben", "Production", "DE:AD:00:00:00:02"),
          ("Demo Chen", "Repair", "DE:AD:00:00:00:03")]


def call(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"null")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--token", default="change-me")
    ap.add_argument("--interval", type=float, default=5.0)
    a = ap.parse_args()

    existing = {e["name"]: e for e in call(f"{a.url}/api/employees")}
    for name, dept, mac in PEOPLE:
        if name not in existing:
            emp = call(f"{a.url}/api/employees", "POST", {"name": name, "department": dept})
            call(f"{a.url}/api/tags", "POST", {"employee_id": emp["id"], "mac": mac, "label": "demo"})
            print(f"created {name} with tag {mac}")

    state = {mac: {"in": True, "cp": random.choice(list(CHECKPOINTS))} for _, _, mac in PEOPLE}
    auth = {"Authorization": f"Bearer {a.token}"}
    while True:
        for mac, s in state.items():
            if random.random() < 0.02:
                s["in"] = not s["in"]
            if random.random() < 0.05:
                s["cp"] = random.choice(list(CHECKPOINTS))
        for cp, name in CHECKPOINTS.items():
            sightings = [{"mac": mac, "rssi": random.randint(-65, -50) if s["cp"] == cp
                          else random.randint(-88, -75), "age_ms": random.randint(0, 4000)}
                         for mac, s in state.items() if s["in"]]
            call(f"{a.url}/api/checkpoint/report", "POST",
                 {"checkpoint": cp, "name": name, "fw": "sim", "sightings": sightings}, auth)
        print(time.strftime("%H:%M:%S"), {m[-2:]: ("in@" + s["cp"][-2:] if s["in"] else "out")
                                          for m, s in state.items()})
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
