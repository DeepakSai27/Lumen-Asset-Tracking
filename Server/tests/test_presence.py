"""Tests for the presence engine and API. Run: python -m pytest -q"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.presence import PresenceEngine, norm_mac, norm_uuid

TAG_MAC = "AA:BB:CC:00:00:01"
CP1 = "10:20:30:40:50:01"
CP2 = "10:20:30:40:50:02"


def at(hh: int, mm: int = 0, ss: int = 0) -> float:
    """Epoch seconds for today at local hh:mm:ss."""
    t = date.today()
    return datetime(t.year, t.month, t.day, hh, mm, ss).timestamp()


@pytest.fixture
def engine(tmp_path):
    s = Settings(db_path=tmp_path / "t.db", absence_timeout_s=180)
    db = Database(s.db_path)
    with db.tx() as c:
        c.execute("INSERT INTO employees(id, name, created_ts) VALUES (1, 'Asha', 0)")
        c.execute("INSERT INTO tags(employee_id, mac, created_ts) VALUES (1, ?, 0)", (TAG_MAC,))
    yield PresenceEngine(db, s)
    db.close()


def seen(engine, ts, cp=CP1, rssi=-60, mac=TAG_MAC):
    return engine.ingest(cp, "", "", "", [{"mac": mac, "rssi": rssi, "age_ms": 0}], now=ts)


def test_normalisers():
    assert norm_mac("aabbcc000001") == TAG_MAC
    assert norm_mac("aa-bb-cc-00-00-01") == TAG_MAC
    assert norm_mac("xyz") is None
    assert norm_uuid("E2C56DB5DFFB48D2B060D0F5A71096E0") == "e2c56db5-dffb-48d2-b060-d0f5a71096e0"


def test_continuous_presence_is_one_session(engine):
    for i in range(0, 3600, 10):          # seen every 10 s from 09:00 to 10:00
        seen(engine, at(9) + i)
    engine.housekeeping(now=at(11))        # left at ~10:00
    row = next(r for r in engine.attendance(date.today(), now=at(11)) if r["employee_id"] == 1)
    assert row["sessions"] == 1
    assert row["first_in"] == at(9)
    assert row["last_out"] == at(9) + 3590
    assert row["total_seconds"] == 3590
    assert not row["present_now"]


def test_short_gap_is_bridged_long_gap_splits(engine):
    seen(engine, at(9))
    seen(engine, at(9, 2))                 # 120 s gap < 180 s timeout -> same session
    seen(engine, at(9, 30))                # 28 min gap -> new session
    seen(engine, at(9, 32))
    engine.housekeeping(now=at(12))
    row = engine.attendance(date.today(), now=at(12))[0]
    assert row["sessions"] == 2
    assert row["total_seconds"] == 120 + 120


def test_presence_and_timeout(engine):
    seen(engine, at(9))
    p = engine.presence(now=at(9, 1))[0]
    assert p["present"] and p["since"] == at(9)
    p = engine.presence(now=at(9, 10))[0]   # 10 min without a sighting
    assert not p["present"]
    assert engine.housekeeping(now=at(9, 10)) == 1


def test_weak_signal_ignored(engine):
    seen(engine, at(9), rssi=-99)
    assert engine.attendance(date.today(), now=at(9, 1))[0]["sessions"] == 0


def test_location_hysteresis(engine):
    seen(engine, at(9), cp=CP1, rssi=-60)
    seen(engine, at(9, 0, 5), cp=CP2, rssi=-62)   # weaker: stay at CP1
    assert engine.presence(now=at(9, 0, 6))[0]["location"] == CP1
    seen(engine, at(9, 0, 10), cp=CP2, rssi=-50)  # clearly stronger: move
    assert engine.presence(now=at(9, 0, 11))[0]["location"] == CP2


def test_unknown_tags_are_discovered_not_logged(engine):
    out = seen(engine, at(9), mac="11:22:33:44:55:66", rssi=-40)
    assert out["whitelist"] == [TAG_MAC]
    assert engine.discovered_list(now=at(9))[0]["mac"] == "11:22:33:44:55:66"
    assert engine.db.one("SELECT COUNT(*) n FROM sightings")["n"] == 0


def test_ibeacon_match(engine):
    with engine.db.tx() as c:
        c.execute("INSERT INTO employees(id, name, created_ts) VALUES (2, 'Ben', 0)")
        c.execute("INSERT INTO tags(employee_id, ib_uuid, ib_major, ib_minor, created_ts) "
                  "VALUES (2, 'e2c56db5-dffb-48d2-b060-d0f5a71096e0', 1, 7, 0)")
    engine.ingest(CP1, "", "", "", [{"mac": "01:02:03:04:05:06", "rssi": -70,
                                     "ib": {"uuid": "E2C56DB5DFFB48D2B060D0F5A71096E0",
                                            "major": 1, "minor": 7}}], now=at(9))
    ben = next(p for p in engine.presence(now=at(9)) if p["name"] == "Ben")
    assert ben["present"]


def test_session_across_midnight_is_split_by_day(engine):
    d = date.today()
    midnight = datetime(d.year, d.month, d.day).timestamp()
    for i in range(-3600, 3601, 60):       # 23:00 yesterday .. 01:00 today
        seen(engine, midnight + i)
    engine.housekeeping(now=midnight + 7200)
    assert engine.attendance(d, now=midnight + 7200)[0]["total_seconds"] == 3600


# ------------------------------------------------------------------ API
@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "api.db", api_token="tok"), run_background=False)
    with TestClient(app) as c:
        yield c


def test_api_flow(client):
    r = client.post("/api/checkpoint/report", json={"checkpoint": CP1, "sightings": []})
    assert r.status_code == 401
    emp = client.post("/api/employees", json={"name": "Asha", "department": "R&D"}).json()["id"]
    assert client.post("/api/tags", json={"employee_id": emp, "mac": "aabbcc000001"}).status_code == 201
    assert client.post("/api/tags", json={"employee_id": emp, "mac": "AA:BB:CC:00:00:01"}).status_code == 409
    r = client.post("/api/checkpoint/report", headers={"Authorization": "Bearer tok"},
                    json={"checkpoint": CP1, "name": "Front door", "fw": "0.1.0",
                          "sightings": [{"mac": TAG_MAC, "rssi": -55, "age_ms": 500}]})
    assert r.status_code == 200 and r.json()["whitelist"] == [TAG_MAC]
    people = client.get("/api/presence").json()["people"]
    assert people[0]["present"] and people[0]["location"] == "Front door"
    cps = client.get("/api/checkpoints").json()
    assert cps[0]["online"] and cps[0]["name"] == "Front door"
    csv_text = client.get("/api/attendance.csv").text
    assert csv_text.splitlines()[0].startswith("date,employee")
    assert "Asha" in csv_text
    assert client.get("/").status_code == 200


def test_admin_password(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "a.db", admin_password="pw"), run_background=False)
    with TestClient(app) as c:
        assert c.get("/api/presence").status_code == 401
        assert c.get("/api/presence", auth=("admin", "pw")).status_code == 200
        assert c.get("/").status_code == 200          # page loads so it can show the login screen


def test_login_session(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "l.db", admin_user="admin", admin_password="pw"), run_background=False)
    with TestClient(app) as c:
        assert c.get("/api/me").status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "nope"}).status_code == 401
        assert c.post("/api/login", json={"username": "other", "password": "pw"}).status_code == 401
        r = c.post("/api/login", json={"username": "Admin", "password": "pw", "remember": True})
        assert r.status_code == 200 and r.json() == {"auth": True, "user": "admin"}
        assert "lumen_session" in r.cookies and "httponly" in r.headers["set-cookie"].lower()
        assert c.get("/api/me").json()["user"] == "admin"
        assert c.get("/api/presence").status_code == 200
        tok = r.cookies["lumen_session"]
        c.cookies.clear()
        c.cookies.set("lumen_session", tok.replace("|", "|9", 1))      # tampered expiry
        assert c.get("/api/presence").status_code == 401
        c.cookies.clear()
        c.cookies.set("lumen_session", tok)
        c.post("/api/logout")
        c.cookies.clear()
        assert c.get("/api/presence").status_code == 401
        assert c.post("/api/checkpoint/report", json={"checkpoint": CP1}).status_code == 401  # still token-only
    # Sessions survive a restart (key kept beside the DB) but not a password change.
    app2 = create_app(Settings(db_path=tmp_path / "l.db", admin_password="pw"), run_background=False)
    with TestClient(app2) as c:
        c.cookies.set("lumen_session", tok)
        assert c.get("/api/me").status_code == 200
    app3 = create_app(Settings(db_path=tmp_path / "l.db", admin_password="new"), run_background=False)
    with TestClient(app3) as c:
        c.cookies.set("lumen_session", tok)
        assert c.get("/api/me").status_code == 401


def test_login_lockout(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "k.db", admin_password="pw"), run_background=False)
    with TestClient(app) as c:
        for _ in range(5):
            assert c.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 429


def test_no_password_means_open(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "o.db"), run_background=False)
    with TestClient(app) as c:
        assert c.get("/api/me").json() == {"auth": False, "user": None}
        assert c.get("/api/presence").status_code == 200


def test_movement(engine):
    for i in range(0, 1800, 10):                 # 09:00-09:30 at CP1
        seen(engine, at(9) + i, cp=CP1)
    for i in range(0, 600, 10):                  # 14:00-14:10 at CP2
        seen(engine, at(14) + i, cp=CP2, rssi=-50)
    engine.housekeeping(now=at(18))
    m = engine.movement(1, date.today(), date.today(), now=at(18))
    assert m["summary"]["present_days"] == 1
    assert m["summary"]["total_seconds"] == 1790 + 590
    assert len(m["days"][0]["sessions"]) == 2
    assert [r["checkpoint_id"] for r in m["records"]] == [CP1, CP2]
    # raw sightings are thinned to one per 15 s, so "last" is within 15 s of the true last sighting
    assert m["records"][0]["first"] == at(9)
    assert at(9) + 1775 <= m["records"][0]["last"] <= at(9) + 1790
    assert {l["checkpoint_id"] for l in m["locations"]} == {CP1, CP2}


def test_movement_api_and_email(client):
    emp = client.post("/api/employees", json={"name": "Pat", "email": "pat@example.com"}).json()["id"]
    r = client.get(f"/api/employees/{emp}/movement")
    assert r.status_code == 200
    body = r.json()
    assert body["employee"]["email"] == "pat@example.com"
    assert body["summary"]["present_days"] == 0
    assert client.get("/api/employees/999/movement").status_code == 404
    assert client.get(f"/api/employees/{emp}/movement?start=2026-01-10&end=2026-01-01").status_code == 400


def test_sightings_are_thinned_but_presence_is_not(engine):
    for i in range(0, 60, 5):                      # 12 sightings in a minute
        seen(engine, at(9) + i)
    n = engine.db.one("SELECT COUNT(*) n FROM sightings")["n"]
    assert n == 4                                   # 0, 15, 30, 45 s
    p = engine.presence(now=at(9, 1))[0]
    assert p["present"] and p["today_sessions"] == [[at(9), at(9) + 55]]
