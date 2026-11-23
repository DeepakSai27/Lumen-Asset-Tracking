"""Presence engine: turns checkpoint sightings into presence sessions and attendance.

Model
-----
* Every report from a checkpoint lists the tags it heard (MAC, RSSI, iBeacon ID, age).
* A sighting of a registered tag extends that employee's open session, or opens a
  new one if the previous sighting is older than ``absence_timeout_s``.
* Housekeeping closes sessions whose last sighting is older than the timeout; the
  session ends at the *last sighting* (not when the timeout expired), so durations
  are not inflated.
* Attendance for a day = the sum of all session time clipped to that local day.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timedelta

from .config import Settings
from .db import Database

MAX_AGE_S = 60.0          # ignore device-reported ages above this (stale buffer)
LOCATION_HYSTERESIS_DB = 3  # a different checkpoint must be this much stronger to take over
LOCATION_STALE_S = 20.0     # ...or the current location must be this old
DISCOVERED_KEEP_S = 600.0
SIGHTING_LOG_INTERVAL_S = 15.0  # store at most one raw sighting per tag+checkpoint per this


def norm_mac(value: str | None) -> str | None:
    if not value:
        return None
    hexdigits = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(hexdigits) != 12:
        return None
    hexdigits = hexdigits.upper()
    return ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))


def norm_uuid(value: str | None) -> str | None:
    if not value:
        return None
    h = re.sub(r"[^0-9A-Fa-f]", "", value).lower()
    if len(h) != 32:
        return None
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def day_bounds(d: date) -> tuple[float, float]:
    """Epoch seconds of local midnight at the start and end of ``d``."""
    start = datetime(d.year, d.month, d.day)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


class PresenceEngine:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings
        self._mem_lock = threading.Lock()
        self.discovered: dict[str, dict] = {}
        self._location: dict[int, tuple[str, int, float]] = {}
        self.discover_until = 0.0
        self._last_prune = 0.0
        self._last_logged: dict[tuple[int, str], float] = {}

    # ------------------------------------------------------------------ ingest
    def ingest(self, checkpoint: str, name: str, fw: str, ip: str,
               sightings: list[dict], now: float | None = None) -> dict:
        now = time.time() if now is None else now
        cp_id = norm_mac(checkpoint) or checkpoint.strip()[:64] or "unknown"
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO checkpoints(id, name, fw, ip, last_report_ts)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       fw = excluded.fw, ip = excluded.ip,
                       last_report_ts = excluded.last_report_ts,
                       name = CASE WHEN checkpoints.name = '' THEN excluded.name
                                   ELSE checkpoints.name END""",
                (cp_id, name or "", fw or "", ip or "", now),
            )
            tags = [dict(r) for r in c.execute(
                "SELECT id, employee_id, mac, ib_uuid, ib_major, ib_minor FROM tags")]
            by_mac = {t["mac"]: t for t in tags if t["mac"]}
            by_ib = {(t["ib_uuid"], t["ib_major"], t["ib_minor"]): t
                     for t in tags if t["ib_uuid"]}

            for s in sightings:
                mac = norm_mac(s.get("mac"))
                if not mac:
                    continue
                rssi = int(s.get("rssi", -127))
                age = min(max(float(s.get("age_ms", 0)) / 1000.0, 0.0), MAX_AGE_S)
                ts = now - age
                ib = s.get("ib") or None
                ib_key = None
                if ib:
                    uuid = norm_uuid(ib.get("uuid"))
                    if uuid is not None:
                        ib_key = (uuid, int(ib.get("major", 0)), int(ib.get("minor", 0)))

                tag = by_mac.get(mac) or (by_ib.get(ib_key) if ib_key else None)
                if tag is None:
                    self._remember_unknown(mac, ib_key, rssi, cp_id, ts)
                    continue
                if rssi < self.s.min_rssi:
                    continue
                key = (tag["id"], cp_id)
                last = self._last_logged.get(key)
                if last is None or ts - last >= SIGHTING_LOG_INTERVAL_S or ts < last:
                    c.execute(
                        "INSERT INTO sightings(ts, checkpoint_id, tag_id, rssi) VALUES (?,?,?,?)",
                        (ts, cp_id, tag["id"], rssi))
                    self._last_logged[key] = ts
                if tag["employee_id"] is not None:
                    self._touch_session(c, tag["employee_id"], cp_id, rssi, ts)

        return {
            "whitelist": sorted(by_mac.keys()),
            "discover": now < self.discover_until,
            "server_time": now,
        }

    def _remember_unknown(self, mac, ib_key, rssi, cp_id, ts) -> None:
        with self._mem_lock:
            prev = self.discovered.get(mac)
            entry = {
                "mac": mac,
                "ib_uuid": ib_key[0] if ib_key else None,
                "ib_major": ib_key[1] if ib_key else None,
                "ib_minor": ib_key[2] if ib_key else None,
                "rssi": rssi,
                "checkpoint_id": cp_id,
                "last_seen": ts,
            }
            # Keep the checkpoint that hears it best (that is where the fob is held).
            if prev and prev["checkpoint_id"] != cp_id and prev["rssi"] > rssi \
                    and ts - prev["last_seen"] < 10:
                prev["last_seen"] = ts
                return
            self.discovered[mac] = entry
            if len(self.discovered) > 1000:
                oldest = sorted(self.discovered.values(), key=lambda e: e["last_seen"])
                for e in oldest[:200]:
                    self.discovered.pop(e["mac"], None)

    def _pick_location(self, emp: int, cp_id: str, rssi: int, ts: float) -> str:
        with self._mem_lock:
            cur = self._location.get(emp)
            if (cur is None or cur[0] == cp_id or rssi > cur[1] + LOCATION_HYSTERESIS_DB
                    or ts - cur[2] > LOCATION_STALE_S):
                self._location[emp] = (cp_id, rssi, ts)
                return cp_id
            return cur[0]

    def _touch_session(self, c, emp: int, cp_id: str, rssi: int, ts: float) -> None:
        row = c.execute(
            "SELECT id, start_ts, last_seen_ts FROM sessions "
            "WHERE employee_id = ? AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1",
            (emp,)).fetchone()
        location = self._pick_location(emp, cp_id, rssi, ts)
        if row and ts - row["last_seen_ts"] <= self.s.absence_timeout_s:
            c.execute(
                "UPDATE sessions SET last_seen_ts = MAX(last_seen_ts, ?), checkpoint_id = ?, "
                "best_rssi = MAX(COALESCE(best_rssi, -127), ?) WHERE id = ?",
                (ts, location, rssi, row["id"]))
            return
        start = ts
        if row:
            c.execute("UPDATE sessions SET end_ts = last_seen_ts WHERE id = ?", (row["id"],))
            start = max(ts, row["last_seen_ts"])
        else:
            last = c.execute("SELECT MAX(end_ts) FROM sessions WHERE employee_id = ?",
                             (emp,)).fetchone()[0]
            if last is not None and ts <= last:
                return  # late, out-of-order sighting inside an already closed session
        c.execute(
            "INSERT INTO sessions(employee_id, start_ts, last_seen_ts, checkpoint_id, best_rssi) "
            "VALUES (?,?,?,?,?)", (emp, start, start, location, rssi))

    # ------------------------------------------------------------ housekeeping
    def housekeeping(self, now: float | None = None) -> int:
        """Close sessions of people who left; prune old data. Returns sessions closed."""
        now = time.time() if now is None else now
        with self.db.tx() as c:
            closed = c.execute(
                "UPDATE sessions SET end_ts = last_seen_ts "
                "WHERE end_ts IS NULL AND last_seen_ts < ?",
                (now - self.s.absence_timeout_s,)).rowcount
            if now - self._last_prune > 3600:
                c.execute("DELETE FROM sightings WHERE ts < ?",
                          (now - self.s.retention_days * 86400,))
                self._last_prune = now
        with self._mem_lock:
            for mac in [m for m, e in self.discovered.items()
                        if now - e["last_seen"] > DISCOVERED_KEEP_S]:
                del self.discovered[mac]
        return closed

    # ----------------------------------------------------------------- queries
    def presence(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        rows = self.db.all(
            """SELECT e.id, e.name, e.department,
                      s.start_ts, s.last_seen_ts AS open_last_seen, s.checkpoint_id,
                      cp.name AS checkpoint_name, cp.location AS checkpoint_location,
                      (SELECT MAX(last_seen_ts) FROM sessions x WHERE x.employee_id = e.id)
                          AS last_seen,
                      (SELECT COUNT(*) FROM tags t WHERE t.employee_id = e.id) AS tag_count
               FROM employees e
               LEFT JOIN sessions s ON s.employee_id = e.id AND s.end_ts IS NULL
               LEFT JOIN checkpoints cp ON cp.id = s.checkpoint_id
               WHERE e.active = 1
               ORDER BY e.name COLLATE NOCASE""")
        today = {r["employee_id"]: r for r in self.attendance(date.today(), now)}
        day_start, day_end = day_bounds(date.today())
        today_sessions: dict[int, list] = {}
        for s in self.db.all(
                "SELECT employee_id, start_ts, COALESCE(end_ts, last_seen_ts) AS stop FROM sessions "
                "WHERE start_ts < ? AND COALESCE(end_ts, last_seen_ts) >= ? ORDER BY start_ts",
                (day_end, day_start)):
            today_sessions.setdefault(s["employee_id"], []).append(
                [max(s["start_ts"], day_start), min(s["stop"], day_end)])
        out = []
        for r in rows:
            present = (r["start_ts"] is not None
                       and now - r["open_last_seen"] <= self.s.absence_timeout_s)
            t = today.get(r["id"], {})
            out.append({
                "employee_id": r["id"],
                "name": r["name"],
                "department": r["department"],
                "present": present,
                "since": r["start_ts"] if present else None,
                "location": (r["checkpoint_location"] or r["checkpoint_name"]
                             or r["checkpoint_id"]) if present else None,
                "last_seen": r["last_seen"],
                "tag_count": r["tag_count"],
                "today_seconds": t.get("total_seconds", 0),
                "today_first_in": t.get("first_in"),
                "today_sessions": today_sessions.get(r["id"], []),
            })
        return out

    def attendance(self, d: date, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        day_start, day_end = day_bounds(d)
        employees = self.db.all(
            "SELECT id, name, department FROM employees WHERE active = 1 "
            "ORDER BY name COLLATE NOCASE")
        sessions = self.db.all(
            """SELECT employee_id, start_ts, COALESCE(end_ts, last_seen_ts) AS stop,
                      end_ts IS NULL AS open
               FROM sessions
               WHERE start_ts < ? AND COALESCE(end_ts, last_seen_ts) >= ?
               ORDER BY start_ts""", (day_end, day_start))
        per: dict[int, dict] = {}
        for s in sessions:
            a = max(s["start_ts"], day_start)
            b = min(s["stop"], day_end)
            if b < a:
                continue
            e = per.setdefault(s["employee_id"], {
                "total_seconds": 0.0, "first_in": None, "last_out": None,
                "sessions": 0, "present_now": False})
            e["total_seconds"] += b - a
            e["first_in"] = a if e["first_in"] is None else min(e["first_in"], a)
            e["last_out"] = b if e["last_out"] is None else max(e["last_out"], b)
            e["sessions"] += 1
            if s["open"] and now - s["stop"] <= self.s.absence_timeout_s:
                e["present_now"] = True
        out = []
        for emp in employees:
            e = per.get(emp["id"], {"total_seconds": 0.0, "first_in": None,
                                    "last_out": None, "sessions": 0, "present_now": False})
            out.append({"employee_id": emp["id"], "name": emp["name"],
                        "department": emp["department"], "date": d.isoformat(),
                        **e, "total_seconds": round(e["total_seconds"])})
        return out

    def employee_sessions(self, emp: int, d: date) -> list[dict]:
        day_start, day_end = day_bounds(d)
        return self.db.all(
            """SELECT s.id, s.start_ts, s.last_seen_ts, s.end_ts, s.checkpoint_id,
                      s.best_rssi, cp.name AS checkpoint_name, cp.location
               FROM sessions s LEFT JOIN checkpoints cp ON cp.id = s.checkpoint_id
               WHERE s.employee_id = ? AND s.start_ts < ?
                 AND COALESCE(s.end_ts, s.last_seen_ts) >= ?
               ORDER BY s.start_ts""", (emp, day_end, day_start))

    def _day_sessions(self, emp: int, d: date) -> list[dict]:
        """The employee's sessions on day ``d``, clipped to that day."""
        day_start, day_end = day_bounds(d)
        out = []
        for s in self.employee_sessions(emp, d):
            stop = s["end_ts"] if s["end_ts"] is not None else s["last_seen_ts"]
            a, b = max(s["start_ts"], day_start), min(stop, day_end)
            if b >= a:
                out.append({"start": a, "end": b, "open": s["end_ts"] is None,
                            "location": s["location"] or s["checkpoint_name"] or s["checkpoint_id"]})
        return out

    def movement_records(self, emp: int, t0: float, t1: float) -> list[dict]:
        """Raw sightings condensed into per-checkpoint visits (gaps <= absence timeout merge)."""
        rows = self.db.all(
            """SELECT s.ts, s.checkpoint_id, s.rssi,
                      COALESCE(NULLIF(cp.location, ''), NULLIF(cp.name, ''), s.checkpoint_id)
                          AS location
               FROM sightings s
               JOIN tags t ON t.id = s.tag_id
               LEFT JOIN checkpoints cp ON cp.id = s.checkpoint_id
               WHERE t.employee_id = ? AND s.ts >= ? AND s.ts < ?
               ORDER BY s.ts""", (emp, t0, t1))
        current: dict[str, dict] = {}
        out: list[dict] = []
        for r in rows:
            cur = current.get(r["checkpoint_id"])
            if cur and r["ts"] - cur["last"] <= self.s.absence_timeout_s:
                cur["last"] = r["ts"]
                cur["count"] += 1
                cur["best_rssi"] = max(cur["best_rssi"], r["rssi"])
            else:
                cur = {"checkpoint_id": r["checkpoint_id"], "location": r["location"],
                       "first": r["ts"], "last": r["ts"], "count": 1, "best_rssi": r["rssi"]}
                current[r["checkpoint_id"]] = cur
                out.append(cur)
        out.sort(key=lambda x: x["first"])
        return out

    def movement(self, emp: int, d0: date, d1: date, now: float | None = None) -> dict:
        """Per-day presence, summary, locations visited and raw records for one employee."""
        now = time.time() if now is None else now
        days = []
        d = d0
        while d <= d1:
            ss = self._day_sessions(emp, d)
            total = sum(x["end"] - x["start"] for x in ss)
            days.append({"date": d.isoformat(), "total_seconds": round(total),
                         "first_in": ss[0]["start"] if ss else None,
                         "last_out": ss[-1]["end"] if ss else None,
                         "sessions": ss})
            d += timedelta(days=1)
        total = sum(x["total_seconds"] for x in days)
        present_days = sum(1 for x in days if x["total_seconds"] > 0)
        t0, _ = day_bounds(d0)
        _, t1 = day_bounds(d1)
        records = self.movement_records(emp, t0, t1)
        locations: dict[str, dict] = {}
        for r in records:
            loc = locations.setdefault(r["checkpoint_id"], {
                "checkpoint_id": r["checkpoint_id"], "location": r["location"],
                "seconds": 0.0, "visits": 0})
            loc["seconds"] += r["last"] - r["first"]
            loc["visits"] += 1
        return {
            "days": days,
            "summary": {"total_seconds": total, "present_days": present_days,
                        "daily_average_seconds": round(total / present_days) if present_days else 0,
                        "range_days": len(days)},
            "locations": sorted(locations.values(), key=lambda x: -x["seconds"]),
            "records": records,
            "records_since": now - self.s.retention_days * 86400,
        }

    def discovered_list(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self._mem_lock:
            items = [dict(e) for e in self.discovered.values()
                     if now - e["last_seen"] <= DISCOVERED_KEEP_S]
        return sorted(items, key=lambda e: -e["rssi"])
