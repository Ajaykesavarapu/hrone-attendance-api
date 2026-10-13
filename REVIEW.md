# REVIEW.md

Review of the starter `app/main.py`. Line numbers refer to the original starter file. More
than ten defects were found, spread across the helpers and the endpoints.

| # | Where (function / line) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (33–37) | Builds the shift-start instant with `punch_in.replace(hour,minute)` on a **UTC** datetime, so lateness is measured against the UTC wall clock, not IST (R1/R2). | Punch-in at 10:05 IST reports `late_minutes` = 0 instead of 35. | Convert the stored UTC instant to IST and compare against the shift start built on the record's attendance date. |
| 2 | `compute_late_minutes` (37) | Threshold `minutes > 10` is wrong: R2 is *strictly more than* 10:00 (600 s), and `late_minutes` is **floor of minutes since shift_start**. `int(delta/60)` also truncates toward zero. | 09:40:00 → should be 0 (ok by luck); 09:40:01 → returns 0, must be 10. | Compare elapsed **seconds** `> 600`; return `floor(seconds/60)`. |
| 3 | `compute_work_hours` (40–41) | `round()` is banker's rounding, not half-up (R4). | A value like 9.125 rounds to 9.12 instead of 9.13. | `Decimal(...).quantize(..., ROUND_HALF_UP)`. |
| 4 | `compute_overtime` (44–47) | Builds `end` as IST-**aware** but `punch_out` is naive UTC; subtracting them raises `TypeError` → 500. | Any punch-out crashes with a 500. | Work entirely in tz-aware datetimes (client is `tz_aware=True`). |
| 5 | `compute_overtime` (47) | No 30-minute minimum (R3); returns any positive overtime. | 5 minutes over shift reports `overtime_minutes` = 5, must be 0. | Count only when `floor(minutes) >= 30`, else 0. |
| 6 | `compute_overtime` (46) | For an overnight shift the shift **ends on the next calendar day**; using `date_str` puts the end 24 h early. | Overnight worker shows a huge bogus overtime. | Add one day to the end instant when `shift_end <= shift_start`. |
| 7 | all helpers | Instants are never truncated to whole seconds (R1). | Stored/returned millis keep sub-second noise; late/overtime off by a minute at boundaries. | Truncate to whole seconds on the way in. |
| 8 | `create_employee` (82–85) | `datetime.now()` is **naive local** time; it is also returned as a datetime, not epoch ms. | `created_at` is wrong by the server's UTC offset and serialized as an ISO string. | `datetime.now(timezone.utc)`, returned as epoch ms by an explicit serializer. |
| 9 | `create_employee` (79–80) | Uniqueness via `find_one` then `insert_one` is a check-then-insert **race**: two concurrent creates both pass the check. | Parallel duplicate `emp_code` creates both succeed. | Unique index on `emp_code`; catch `DuplicateKeyError` → 409. |
| 10 | `create_employee` (77–85) | No check that `shift_start != shift_end`. | Equal shift times are accepted (contract: 422). | Reject equal values with 422. |
| 11 | `list_employees` (93) | `skip = page * page_size` skips the **first** page (page is 1-based, R10). | `page=1` returns page 2's rows. | `skip = (page-1) * page_size`. |
| 12 | `list_employees` (94) | `total = count_documents({})` ignores the `department` filter (R10: total reflects filters). | Filtered list reports the whole-collection total. | Count with the same filter. |
| 13 | `list_employees` (95) | Not sorted by `emp_code` ascending as the contract requires; no `page_size` max. | Order is arbitrary; `page_size=10000` is honoured. | `.sort("emp_code")`; `page_size` capped at 100 via query validation. |
| 14 | `punch_in` (101) | `emp` is never checked for `None`; an unknown employee hits `emp["shift_start"]` → `KeyError` → 500. | Punch-in for a missing employee returns 500 (must be 404). | 404 when the employee is absent. |
| 15 | `punch_in` (102–103) | `datetime.fromtimestamp(punched_at/1000)` is naive **local** time, and `ts.date()` uses the server-local date — ignores IST and the overnight rule (R1). | Attendance `date` is wrong near midnight / for overnight shifts. | Convert epoch ms to UTC, derive the attendance date in IST per R1. |
| 16 | `punch_in` (104–105) | Duplicate detection is a check-then-insert race. | Two simultaneous punch-ins both create a record. | Unique `(emp_code, date)` index; `DuplicateKeyError` → 409. |
| 17 | `punch_in` (100, 109) | `status` is not restricted to `PRESENT/WFH/ON_DUTY`; `punched_at` is not validated as epoch ms (seconds/floats/strings slip through). | `status="ABSENT"` or a seconds timestamp is accepted. | `Literal` status; strict int with the epoch-ms range → 422. |
| 18 | `punch_in` (119–121) | Returns an `id` field (stringified `_id`), which the contract forbids — attendance records have no id. | Response contains `id`. | Serialize explicitly; never expose `_id`. |
| 19 | `list_attendance` (144–147) | Loads the **entire** filtered collection into memory, then sorts and slices in Python — unusable at ~100k docs. | Slow/at-scale memory blowup; not index-backed. | DB-side `.sort().skip().limit()` backed by an index, with `count_documents` for `total`. |
| 20 | `list_attendance` (145) | Sort is `date` only; the contract requires `date` desc **then** `emp_code` asc. | Same-day rows are in arbitrary order. | Compound sort `[(date,-1),(emp_code,1)]`. |
| 21 | `list_attendance` (124–150) | No `date_from > date_to` validation (→ 422); exposes `_id` as `id`; returns punch times as datetimes, not epoch ms; no `page_size` max. | Bad range returns 200; `id` leaks; wrong time format. | Validate the range (422); explicit epoch-ms serializer; capped pagination. |
| 22 | module (22) | `MongoClient` is not `tz_aware`, so PyMongo returns **naive** datetimes → latent timezone bugs throughout. | Mixing naive/aware datetimes raises or miscomputes. | Create the client with `tz_aware=True`. |
| 23 | startup | No indexes are created, though the contract requires them at startup. | List/analytics do collection scans. | Create all indexes idempotently in a startup hook. |
| 24 | `health` (72–74) | Always returns `ok` without pinging MongoDB (contract: 503 when the DB is down). | Health is green while the DB is unreachable. | `db.command("ping")`; 503 on failure. |

## Looked at, decided **not** a defect

- **`IST` constant (line 25)** — `timezone(timedelta(hours=5, minutes=30))` is the correct IST
  offset; India has no DST, so a fixed offset is right. I reused it.
- **`EmployeeIn` defaults `09:30` / `18:30` (58–59)** — these match the contract defaults; kept.
- **`/employees` returning a `page`/`page_size` echo** — matches the `EmployeePage` schema; kept.
- **The helper functions existing at all** — the idea is fine; they just needed correct IST,
  rounding, overnight and threshold logic, so I rewrote their bodies rather than removing them.
