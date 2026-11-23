"""Lumen Asset Tracking server: checkpoint ingest API, attendance API and website."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .presence import PresenceEngine, norm_mac, norm_uuid

log = logging.getLogger("lumen")
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- API models
class IBeacon(BaseModel):
    uuid: str
    major: int = Field(ge=0, le=65535)
    minor: int = Field(ge=0, le=65535)


class Sighting(BaseModel):
    mac: str
    rssi: int
    age_ms: int = 0
    ib: IBeacon | None = None


class Report(BaseModel):
    checkpoint: str
    name: str = ""
    fw: str = ""
    sightings: list[Sighting] = Field(default_factory=list, max_length=512)


class EmployeeIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    department: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=200)


class EmployeePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)


class TagIn(BaseModel):
    employee_id: int | None = None
    mac: str | None = None
    ib_uuid: str | None = None
    ib_major: int | None = Field(default=None, ge=0, le=65535)
    ib_minor: int | None = Field(default=None, ge=0, le=65535)
    label: str = Field(default="", max_length=100)


class TagPatch(BaseModel):
    employee_id: int | None = None
    label: str | None = Field(default=None, max_length=100)


class CheckpointPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)


def parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


# ---------------------------------------------------------------- app factory
def create_app(settings: Settings | None = None, run_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    engine = PresenceEngine(db, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if run_background:
            async def loop():
                while True:
                    await asyncio.sleep(15)
                    try:
                        await asyncio.to_thread(engine.housekeeping)
                    except Exception:  # keep the loop alive
                        log.exception("housekeeping failed")
            task = asyncio.create_task(loop())
        if settings.api_token == "change-me":
            log.warning("LUMEN_API_TOKEN is the default 'change-me' - set your own token")
        yield
        if task:
            task.cancel()
        db.close()

    app = FastAPI(title="Lumen Asset Tracking", lifespan=lifespan)
    app.state.engine = engine
    app.state.db = db
    app.state.settings = settings

    basic = HTTPBasic(auto_error=False)

    def require_admin(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not settings.admin_password:
            return
        if creds is None or not secrets.compare_digest(
                creds.password.encode(), settings.admin_password.encode()):
            raise HTTPException(401, "authentication required",
                                headers={"WWW-Authenticate": 'Basic realm="Lumen"'})

    def require_checkpoint(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {settings.api_token}"
        if not secrets.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "bad checkpoint token")

    # ------------------------------------------------------- checkpoint ingest
    @app.post("/api/checkpoint/report", dependencies=[Depends(require_checkpoint)])
    def checkpoint_report(report: Report, request: Request):
        ip = request.client.host if request.client else ""
        return engine.ingest(report.checkpoint, report.name, report.fw, ip,
                             [s.model_dump() for s in report.sightings])

    @app.get("/api/health")
    def health():
        return {"ok": True, "time": time.time()}

    # ------------------------------------------------------------- website/API
    ui = APIRouter(dependencies=[Depends(require_admin)])

    @ui.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @ui.get("/api/presence")
    def presence():
        return {"timeout_s": settings.absence_timeout_s, "now": time.time(),
                "people": engine.presence()}

    @ui.get("/api/attendance")
    def attendance(date: str | None = None):
        return engine.attendance(parse_day(date))

    @ui.get("/api/attendance.csv")
    def attendance_csv(start: str | None = None, end: str | None = None):
        d0 = parse_day(start)
        d1 = parse_day(end) if end else d0
        if d1 < d0 or (d1 - d0).days > 366:
            raise HTTPException(400, "range must be 1-366 days")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "employee", "department", "first_in", "last_out",
                    "hours_present", "sessions"])
        fmt = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""
        d = d0
        while d <= d1:
            for r in engine.attendance(d):
                w.writerow([r["date"], r["name"], r["department"], fmt(r["first_in"]),
                            fmt(r["last_out"]), f"{r['total_seconds'] / 3600:.2f}",
                            r["sessions"]])
            d += timedelta(days=1)
        name = f"attendance_{d0.isoformat()}_{d1.isoformat()}.csv"
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @ui.get("/api/employees/{emp_id}/sessions")
    def employee_sessions(emp_id: int, date: str | None = None):
        return engine.employee_sessions(emp_id, parse_day(date))

    @ui.get("/api/employees/{emp_id}/movement")
    def employee_movement(emp_id: int, start: str | None = None, end: str | None = None):
        d0 = parse_day(start)
        d1 = parse_day(end) if end else d0
        if d1 < d0 or (d1 - d0).days > 92:
            raise HTTPException(400, "range must be 1-93 days")
        emp = db.one("SELECT id, name, department, email, active FROM employees WHERE id = ?",
                     (emp_id,))
        if not emp:
            raise HTTPException(404, "no such employee")
        emp["tags"] = db.all("SELECT id, mac, ib_uuid, ib_major, ib_minor, label FROM tags "
                             "WHERE employee_id = ? ORDER BY id", (emp_id,))
        return {"employee": emp, "start": d0.isoformat(), "end": d1.isoformat(),
                **engine.movement(emp_id, d0, d1)}

    # Employees
    @ui.get("/api/employees")
    def list_employees():
        emps = db.all("SELECT id, name, department, email FROM employees WHERE active = 1 "
                      "ORDER BY name COLLATE NOCASE")
        tags = db.all("SELECT * FROM tags WHERE employee_id IS NOT NULL ORDER BY id")
        for e in emps:
            e["tags"] = [t for t in tags if t["employee_id"] == e["id"]]
        return emps

    @ui.post("/api/employees", status_code=201)
    def add_employee(body: EmployeeIn):
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO employees(name, department, email, created_ts) VALUES (?,?,?,?)",
                (body.name.strip(), body.department.strip(), body.email.strip(), time.time()))
        return {"id": cur.lastrowid}

    @ui.patch("/api/employees/{emp_id}")
    def edit_employee(emp_id: int, body: EmployeePatch):
        with db.tx() as c:
            if body.name is not None:
                c.execute("UPDATE employees SET name = ? WHERE id = ?", (body.name.strip(), emp_id))
            if body.department is not None:
                c.execute("UPDATE employees SET department = ? WHERE id = ?",
                          (body.department.strip(), emp_id))
            if body.email is not None:
                c.execute("UPDATE employees SET email = ? WHERE id = ?",
                          (body.email.strip(), emp_id))
        return {"ok": True}

    @ui.delete("/api/employees/{emp_id}")
    def remove_employee(emp_id: int):
        # Soft delete: attendance history is kept, tags are freed for reuse.
        with db.tx() as c:
            c.execute("UPDATE employees SET active = 0 WHERE id = ?", (emp_id,))
            c.execute("UPDATE tags SET employee_id = NULL WHERE employee_id = ?", (emp_id,))
            c.execute("UPDATE sessions SET end_ts = last_seen_ts "
                      "WHERE employee_id = ? AND end_ts IS NULL", (emp_id,))
        return {"ok": True}

    # Tags
    @ui.get("/api/tags")
    def list_tags():
        return db.all("SELECT t.*, e.name AS employee_name FROM tags t "
                      "LEFT JOIN employees e ON e.id = t.employee_id ORDER BY t.id")

    @ui.post("/api/tags", status_code=201)
    def add_tag(body: TagIn):
        mac = norm_mac(body.mac) if body.mac else None
        if body.mac and not mac:
            raise HTTPException(400, "MAC must be 12 hex digits, e.g. AA:BB:CC:DD:EE:FF")
        uuid = norm_uuid(body.ib_uuid) if body.ib_uuid else None
        if body.ib_uuid and (uuid is None or body.ib_major is None or body.ib_minor is None):
            raise HTTPException(400, "iBeacon needs a 32-hex-digit UUID, major and minor")
        if not mac and not uuid:
            raise HTTPException(400, "give a MAC address or an iBeacon ID")
        try:
            with db.tx() as c:
                cur = c.execute(
                    "INSERT INTO tags(employee_id, mac, ib_uuid, ib_major, ib_minor, label, "
                    "created_ts) VALUES (?,?,?,?,?,?,?)",
                    (body.employee_id, mac, uuid, body.ib_major if uuid else None,
                     body.ib_minor if uuid else None, body.label.strip(), time.time()))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(409, "this tag is already registered")
            raise
        with engine._mem_lock:
            if mac:
                engine.discovered.pop(mac, None)
        return {"id": cur.lastrowid}

    @ui.patch("/api/tags/{tag_id}")
    def edit_tag(tag_id: int, body: TagPatch):
        fields = body.model_dump(exclude_unset=True)
        with db.tx() as c:
            if "employee_id" in fields:
                c.execute("UPDATE tags SET employee_id = ? WHERE id = ?",
                          (fields["employee_id"], tag_id))
            if fields.get("label") is not None:
                c.execute("UPDATE tags SET label = ? WHERE id = ?", (fields["label"], tag_id))
        return {"ok": True}

    @ui.delete("/api/tags/{tag_id}")
    def remove_tag(tag_id: int):
        with db.tx() as c:
            c.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return {"ok": True}

    # Discovery of new tags
    @ui.get("/api/discovered")
    def discovered():
        return {"discover_until": engine.discover_until, "now": time.time(),
                "items": engine.discovered_list()}

    @ui.post("/api/discover")
    def start_discover(enable: bool = True):
        engine.discover_until = time.time() + settings.discover_window_s if enable else 0
        return {"discover_until": engine.discover_until}

    # Checkpoints
    @ui.get("/api/checkpoints")
    def list_checkpoints():
        now = time.time()
        rows = db.all("SELECT * FROM checkpoints ORDER BY name COLLATE NOCASE, id")
        for r in rows:
            r["online"] = bool(r["last_report_ts"]) and \
                now - r["last_report_ts"] <= settings.checkpoint_offline_s
        return rows

    @ui.patch("/api/checkpoints/{cp_id}")
    def edit_checkpoint(cp_id: str, body: CheckpointPatch):
        with db.tx() as c:
            if body.name is not None:
                c.execute("UPDATE checkpoints SET name = ? WHERE id = ?", (body.name.strip(), cp_id))
            if body.location is not None:
                c.execute("UPDATE checkpoints SET location = ? WHERE id = ?",
                          (body.location.strip(), cp_id))
        return {"ok": True}

    @ui.delete("/api/checkpoints/{cp_id}")
    def remove_checkpoint(cp_id: str):
        with db.tx() as c:
            c.execute("DELETE FROM checkpoints WHERE id = ?", (cp_id,))
        return {"ok": True}

    app.include_router(ui)
    return app

