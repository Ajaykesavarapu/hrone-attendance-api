# Employee Attendance & Analytics API

A small HR attendance service built with **FastAPI + MongoDB** (PyMongo). Employees punch
in/out, managers regularize records with an audit trail, and four analytics endpoints run
as MongoDB aggregation pipelines. Implements the HROne assignment contract v2.0.

All application code is in [`app/main.py`](app/main.py).

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB
#   local MongoDB via Docker:  docker run -p 27017:27017 mongo:7
python sample_seed.py           # optional: load the 9 sample documents
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment (real env vars take priority over
`.env`). All indexes are created at startup, idempotently — no manual steps. MongoDB 6.0+ is
required (`$densify` / `$setWindowFields` / `$rank` are used); tested against 7.0.

Interactive docs: `http://localhost:8000/docs`.

## What's implemented

Everything in the contract:

- `GET /health`, `POST/GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out` (race-safe), `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}` — regularize with history audit trail
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary` (cross-collection)
- `GET /analytics/leaderboard/late` (competition ranking via `$rank`)
- `GET /analytics/departments/{department}/trend` (`$densify` gap-fill + 7-day moving average)
- `GET /admin/explain/{endpoint}` — `executionStats` explain of each endpoint's main query

Business rules R1–R10 are implemented as described in `openapi.yaml`:
- Instants are epoch **milliseconds** at the API boundary, BSON datetimes (UTC) in Mongo,
  truncated to whole seconds (R1). The client is created with `tz_aware=True`.
- IST (UTC+05:30) is used for attendance dates, late (R2) and overtime (R3), including
  overnight shifts.
- `work_hours` (R4) and reported rates (R8) use **half-up** rounding (`Decimal`), not Python's
  banker's rounding.
- Analytics read the **stored** derived values; they do not recompute them from punch times.

Part A findings are in [`REVIEW.md`](REVIEW.md); design answers in [`DECISIONS.md`](DECISIONS.md).

## Nothing is knowingly left out

All endpoints and rules in the contract are implemented. The original candidate kit (which
contains the problem statement) is kept locally for reference but is git-ignored, so it is
not published in the public repository.
