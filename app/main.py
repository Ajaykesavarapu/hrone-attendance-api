"""
Employee Attendance & Analytics API  (HROne assignment, contract v2.0)

All application code lives in this one file, as required by the kit.
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real env vars win)

Design notes live in DECISIONS.md and the defect list in REVIEW.md.
Key conventions (see openapi.yaml / DATA_MODEL.md):
  * Employees are identified by `emp_code`; `_id` never crosses the API boundary.
  * Instants are epoch MILLISECONDS in the API and BSON datetimes (UTC) in Mongo.
  * Calendar dates / shift times are strings; shift times and attendance dates are IST.
  * Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are computed
    by this API only, stored, and read as-is by analytics.
"""
import calendar
import os
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # real environment variables take priority over .env (dotenv does not override)

# tz_aware=True => PyMongo returns timezone-aware UTC datetimes instead of naive ones,
# which removes a whole class of timezone bugs when we convert to/from epoch millis.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60          # R2: 10 minute grace
MIN_OVERTIME_MINUTES = 30        # R3: overtime counts only when >= 30
HALF_DAY_THRESHOLD = 4.50        # R5
MAX_PUNCH_SPAN = timedelta(hours=24)

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Indexes  (created once at startup, idempotently)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def ensure_indexes() -> None:
    # emp_code is the business key; a UNIQUE index makes concurrent creates race-safe.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # list employees filtered by department, sorted by emp_code.
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    # department summary (no dept filter) and per-day headcount both filter on joined_on.
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined")

    # At most one record per (emp_code, date); UNIQUE makes punch-in race-safe and
    # serves emp_code-scoped reads (monthly, trend, emp-filtered attendance list).
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    # attendance list default sort (date desc, emp_code asc); also serves month ranges
    # for the department summary / leaderboard / trend scans.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"
    )


# --------------------------------------------------------------------------- #
# Time + rounding helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch millis -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Aware datetime -> epoch millis (whole seconds). None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is None:                      # defensive: treat naive as UTC
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def now_dt() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def round_half_up(value: Optional[float], places: int) -> Optional[float]:
    """Decimal half-up rounding (R4/R8). Python's round() is banker's rounding -> wrong."""
    if value is None:
        return None
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


# --------------------------------------------------------------------------- #
# Business rules R1-R5  (operate on aware UTC datetimes; think in IST)
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: a record's `date` is the IST calendar date of punch_in, except on an
    overnight shift a punch-in earlier (IST) than shift_end belongs to the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        if ist.hour * 60 + ist.minute < hhmm_to_minutes(shift_end):
            day -= timedelta(days=1)
    return day.isoformat()


def _ist_instant(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    y, m, d = map(int, date_str.split("-"))
    h, mi = map(int, hhmm.split(":"))
    return datetime(y, m, d, h, mi, tzinfo=IST) + timedelta(days=plus_days)


def compute_late_minutes(punch_in: datetime, attn_date: str, shift_start: str) -> int:
    """R2: late only when strictly > 10:00 after shift_start; then floor(minutes since shift_start)."""
    start = _ist_instant(attn_date, shift_start)
    elapsed = (punch_in.astimezone(IST) - start).total_seconds()
    if elapsed > GRACE_SECONDS:
        return int(elapsed // 60)
    return 0


def compute_overtime_minutes(punch_out: datetime, attn_date: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes from shift_end to punch_out), counted only when >= 30."""
    plus = 1 if is_overnight(shift_start, shift_end) else 0  # overnight shift ends next day
    end = _ist_instant(attn_date, shift_end, plus_days=plus)
    delta = (punch_out.astimezone(IST) - end).total_seconds()
    if delta <= 0:
        return 0
    minutes = int(delta // 60)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, 2 decimals half-up."""
    hours = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_derived_on_punch_out(punch_in, punch_out, attn_date, shift_start, shift_end):
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime_minutes(punch_out, attn_date, shift_start, shift_end)
    half_day = work_hours < HALF_DAY_THRESHOLD            # R5 uses the rounded value
    return work_hours, overtime, half_day


# --------------------------------------------------------------------------- #
# Serialization  (Mongo doc -> API shape; epoch millis, no _id)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


_TIME_FIELDS = {"punch_in", "punch_out"}


def _history_out(entry: dict) -> dict:
    changes = {}
    for field, delta in (entry.get("changes") or {}).items():
        if field in _TIME_FIELDS:
            changes[field] = {"from": dt_to_ms(delta.get("from")), "to": dt_to_ms(delta.get("to"))}
        else:
            changes[field] = {"from": delta.get("from"), "to": delta.get("to")}
    return {"at": dt_to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [_history_out(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Request models  (strict int for epoch millis -> floats/strings/seconds => 422)
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc)          # uniqueness enforced by the DB index (race-safe)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)    # total reflects the filter (R10)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)          # page is 1-based (R10)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    attn_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attn_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, attn_date, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)     # unique (emp_code, date) => exactly one 201 under races
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()

    # Most recent record whose punch_in is at or before punched_at (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    punch_in_dt = rec["punch_in"]
    if ts <= punch_in_dt:
        raise HTTPException(422, "punch-out must be after punch-in")
    if ts - punch_in_dt > MAX_PUNCH_SPAN:
        raise HTTPException(422, "punch-out more than 24 hours after punch-in")

    work_hours, overtime, half_day = compute_derived_on_punch_out(
        punch_in_dt, ts, rec["date"], emp["shift_start"], emp["shift_end"]
    )
    # Guard on punch_out: None so two simultaneous punch-outs yield exactly one success.
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours,
                  "overtime_minutes": overtime, "half_day": half_day}},
    )
    if res.modified_count == 0:
        raise HTTPException(409, "record already punched out")
    return record_out(db.attendance_logs.find_one({"_id": rec["_id"]}))


# --------------------------------------------------------------------------- #
# Attendance: list
# --------------------------------------------------------------------------- #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = _attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])   # index-backed, not in memory
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def _attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Attendance: regularize (PATCH) with audit trail
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    provided = body.model_dump(exclude_unset=True)
    new_status = body.status if "status" in provided else rec["status"]
    new_pin = rec.get("punch_in")
    new_pout = rec.get("punch_out")

    if body.status in ("ABSENT", "LEAVE"):
        # Absence clears both punch times; supplying either is a contradiction.
        if "punch_in" in provided or "punch_out" in provided:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT/LEAVE")
        new_pin = None
        new_pout = None
    else:
        if "punch_in" in provided:
            new_pin = ms_to_dt(body.punch_in)
        if "punch_out" in provided:
            new_pout = ms_to_dt(body.punch_out)

    # Compute derived fields from the final times.
    if new_status in PRESENCE_STATUSES:
        if new_pin is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        late = compute_late_minutes(new_pin, date, emp["shift_start"])
        if new_pout is not None:
            if new_pout <= new_pin:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_pout - new_pin > MAX_PUNCH_SPAN:
                raise HTTPException(422, "punch_out more than 24 hours after punch_in")
            work_hours, overtime, half_day = compute_derived_on_punch_out(
                new_pin, new_pout, date, emp["shift_start"], emp["shift_end"]
            )
        else:
            work_hours, overtime, half_day = None, 0, False
    else:  # ABSENT / LEAVE
        new_pin, new_pout = None, None
        late, work_hours, overtime, half_day = 0, None, 0, False

    final = {
        "status": new_status, "punch_in": new_pin, "punch_out": new_pout,
        "work_hours": work_hours, "late_minutes": late,
        "overtime_minutes": overtime, "half_day": half_day,
    }
    current = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False),
    }
    changes = {k: {"from": current[k], "to": final[k]} for k in final if final[k] != current[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: match on the history length we read; a concurrent edit that
    # already appended makes this update a no-op (409), so no history entry is silently lost.
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "history": {"$size": len(rec.get("history", []))}},
        {"$set": final, "$push": {"history": entry}},
    )
    if res.modified_count == 0:
        raise HTTPException(409, "record was modified concurrently")
    return record_out(db.attendance_logs.find_one({"_id": rec["_id"]}))


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def count_weekdays(start: date, end: date) -> int:
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, rem = divmod(days, 7)
    total = full_weeks * 5
    for i in range(rem):                       # remainder of at most 6 days
        if (start + timedelta(days=i)).weekday() < 5:
            total += 1
    return total


# $dayOfWeek: 1=Sunday..7=Saturday -> Mon..Fri is 2..6
_WEEKDAY_EXPR = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}


def agg_round_half_up(expr, places: int):
    """Half-up rounding expression for non-negative values (rates are >= 0)."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


# --------------------------------------------------------------------------- #
# Analytics: employee monthly
# --------------------------------------------------------------------------- #
def _employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$addFields": {
            "_dow": {"$dayOfWeek": _WEEKDAY_EXPR},
            "_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$_presence", {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    start, end = month_bounds(month)
    agg = list(db.attendance_logs.aggregate(_employee_monthly_pipeline(emp_code, start, end)))
    g = agg[0] if agg else {}

    eff_start = max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(start))  # R7
    working_days = count_weekdays(eff_start, date.fromisoformat(end))
    present_days = g.get("present_days", 0) or 0
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2) if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": g.get("leave_days", 0),
        "late_count": g.get("late_count", 0),
        "total_late_minutes": g.get("total_late_minutes", 0),
        "total_overtime_minutes": g.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary (cross-collection)
# --------------------------------------------------------------------------- #
def _department_summary_match(end: str, department: Optional[str]) -> dict:
    match = {"joined_on": {"$lte": end}}       # R9: counted if joined on/before period end
    if department:
        match["department"] = department
    return match


def _department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    presence = list(PRESENCE_STATUSES)
    return [
        {"$match": _department_summary_match(end, department)},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$ec"]},
                {"$gte": ["$date", start]},
                {"$lte": ["$date", end]},
            ]}}}],
            "as": "logs",
        }},
        # preserve => employees with zero logs still contribute to headcount (R9).
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "_presence": {"$in": [{"$ifNull": ["$logs.status", ""]}, presence]},
            "_dow": {"$dayOfWeek": {"$dateFromString": {
                "dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}, "format": "%Y-%m-%d"}}},
        }},
        {"$addFields": {
            "_present_contrib": {"$cond": [
                {"$and": ["$_presence", {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]},
            # work_hours averaged over records (present-status with a value) -> null otherwise.
            "_wh": {"$cond": [
                {"$and": ["$_presence", {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", None,
            ]},
        }},
        {"$group": {
            "_id": "$department",
            "_emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": "$_present_contrib"},
            "avg_work_hours": {"$avg": "$_wh"},         # $avg ignores nulls
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    start, end = month_bounds(month)
    items = []
    for g in db.attendance_logs.database.employees.aggregate(
        _department_summary_pipeline(start, end, department)
    ):
        items.append({
            "department": g["_id"],
            "headcount": len(g["_emps"]),
            "present_days": g["present_days"] or 0,
            "avg_work_hours": round_half_up(g["avg_work_hours"], 2),
            "late_count": g["late_count"],
            "total_late_minutes": g["total_late_minutes"],
            "leave_count": g["leave_count"],
            "on_duty_count": g["on_duty_count"],
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard (competition ranking)
# --------------------------------------------------------------------------- #
def _late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                    # drops logs whose employee does not exist
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # Rank on total_late_minutes only => ties share a rank; $rank skips the next (1,2,2,4).
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"$expr": {"$lte": ["$rank", limit]}}},   # limit applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
            "department": "$emp.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    items = list(db.attendance_logs.aggregate(_late_leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend (gap-filled daily series + 7-day moving average)
# --------------------------------------------------------------------------- #
def _department_trend_pipeline(dept_codes, department, from_s, to_s, from_dt, upper_dt) -> list:
    presence = list(PRESENCE_STATUSES)
    return [
        {"$match": {"emp_code": {"$in": dept_codes}, "date": {"$gte": from_s, "$lte": to_s}}},
        {"$addFields": {
            "_d": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            "_present": {"$cond": [
                {"$in": ["$status", presence]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]},
            "_late": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
        }},
        {"$group": {"_id": "$_d",
                    "present_count": {"$sum": "$_present"},
                    "late_count": {"$sum": "$_late"}}},
        # Gap-fill every calendar day in range in the DB (no Python loop).
        {"$densify": {"field": "_id",
                      "range": {"step": 1, "unit": "day", "bounds": [from_dt, upper_dt]}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]},
                  "date_str": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
                  "dow": {"$dayOfWeek": "$_id"}}},
        {"$set": {"is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        # headcount = department employees who had joined on/before that day (R9), in the DB.
        {"$lookup": {
            "from": "employees",
            "let": {"dd": "$date_str"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$dd"]}]}}},
                {"$count": "c"},
            ],
            "as": "_hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_hc.c", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            agg_round_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$sort": {"_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                         "window": {"documents": [-6, 0]}}},
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": ["$moving_avg_7d", None]}, None, agg_round_half_up("$moving_avg_7d", 4)]}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def _trend_params(from_s: str, to_s: str):
    from_d, to_d = date.fromisoformat(from_s), date.fromisoformat(to_s)
    if to_d < from_d:
        raise HTTPException(422, "`to` must not be before `from`")
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=UTC)
    upper_dt = datetime(to_d.year, to_d.month, to_d.day, tzinfo=UTC) + timedelta(days=1)
    return from_dt, upper_dt


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    dept_codes = db.attendance_logs.database.employees.distinct("emp_code", {"department": department})
    if not dept_codes:
        raise HTTPException(404, "unknown department")
    from_dt, upper_dt = _trend_params(from_, to)
    items = list(db.attendance_logs.aggregate(
        _department_trend_pipeline(dept_codes, department, from_, to, from_dt, upper_dt)
    ))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain  (proves the main query of each endpoint uses an index)
# --------------------------------------------------------------------------- #
def _require(value, name):
    if value is None:
        raise HTTPException(422, f"missing required parameter: {name}")
    return value


def _explain_command(coll: str, spec: dict, hint):
    if "aggregate" in spec:
        cmd = {"aggregate": coll, "pipeline": spec["pipeline"], "cursor": {}, "hint": hint}
    else:
        cmd = {"find": coll, **spec, "hint": hint}
    out = db.command({"explain": cmd, "verbosity": "executionStats"})
    # Strip BSON types so the raw plan is plain-JSON serializable.
    import json
    from bson import json_util
    return json.loads(json_util.dumps(out))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = _attendance_filter(emp_code, date_from, date_to, status)
        hint = "uniq_emp_date" if emp_code else "date_desc_emp"
        spec = {"filter": q, "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size, "limit": page_size}
        return _result("attendance_logs", endpoint, _explain_command("attendance_logs", spec, hint))

    if endpoint == "employee_monthly":
        start, end = month_bounds(_require(month, "month"))
        _require(emp_code, "emp_code")
        pipe = _employee_monthly_pipeline(emp_code, start, end)
        return _result("attendance_logs", endpoint,
                       _explain_command("attendance_logs", {"aggregate": True, "pipeline": pipe}, "uniq_emp_date"))

    if endpoint == "department_summary":
        start, end = month_bounds(_require(month, "month"))
        # Main collection the aggregation starts from is `employees`.
        spec = {"filter": _department_summary_match(end, department)}
        hint = "dept_joined" if department else "joined"
        return _result("employees", endpoint, _explain_command("employees", spec, hint))

    if endpoint == "late_leaderboard":
        start, end = month_bounds(_require(month, "month"))
        spec = {"filter": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}}
        return _result("attendance_logs", endpoint,
                       _explain_command("attendance_logs", spec, "date_desc_emp"))

    # department_trend
    _require(department, "department")
    start = _require(from_, "from")
    end = _require(to, "to")
    dept_codes = db.attendance_logs.database.employees.distinct("emp_code", {"department": department})
    spec = {"filter": {"emp_code": {"$in": dept_codes}, "date": {"$gte": start, "$lte": end}}}
    return _result("attendance_logs", endpoint, _explain_command("attendance_logs", spec, "uniq_emp_date"))


def _result(collection: str, endpoint: str, explain: dict) -> dict:
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
