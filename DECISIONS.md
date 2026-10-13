# DECISIONS.md

1. **Indexes.** `employees`: a **unique** index on `emp_code` (business key + race-safe
   creates), `(department, emp_code)` for the filtered/sorted list, and `(department,
   joined_on)` / `joined_on` for headcount (R9). `attendance_logs`: a **unique**
   `(emp_code, date)` (one record per day, race-safe punch-in, and all emp-scoped reads),
   plus `(date, emp_code)` for the attendance list's `date desc, emp_code asc` sort and the
   month-range analytics scans. I considered an index on `status` but rejected it: it is
   low-cardinality and always paired with a date range the compound index already serves.

2. **Punch-in race.** I do **not** check-then-insert. Both requests build the day's document
   and call `insert_one`; the unique `(emp_code, date)` index lets exactly one commit, the
   other raises `DuplicateKeyError` which I map to 409. So one 201, the rest 409.

3. **Ties.** Ranking uses `$rank` over `total_late_minutes` only, so tied employees share a
   rank and the next rank is skipped (1, 2, 2, 4). `limit` is applied **after** ranking as
   `rank <= limit`, so a tie straddling the cutoff returns *all* tied rows — the response can
   exceed `limit`. Rows are ordered `total_late_minutes` desc, then `emp_code` asc.

4. **Headcount.** The department summary starts from `employees` (not the logs), matches
   `joined_on <= month end` (R9), `$lookup`s the month's logs and `$unwind`s with
   `preserveNullAndEmptyArrays`, so zero-log employees survive and are counted via
   `$addToSet` of `emp_code`.

5. **For 100× the data.** I'd move the per-day headcount in the trend out of a correlated
   `$lookup` into a precomputed daily `(department, date) → headcount` rollup (or a
   materialized view), so the hot trend/summary paths read a small aggregated collection
   instead of fanning out over `employees` for every day.
