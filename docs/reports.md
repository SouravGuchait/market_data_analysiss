# Reporting

`apps/reports` answers questions *about* the data the pipeline produced rather
than serving the rows themselves. It has **no models**: every report is an
aggregate over rows another app owns — `IngestionJob` and `AuditEvent` — so there
is nothing to migrate and nothing that can drift from a schema.

Three reports are implemented; this document covers each in turn.

| # | Report | Endpoint | Access | Status |
|---|---|---|---|---|
| 1 | User analytics | `GET /api/v1/reports/me/` | authenticated, self-scoped | implemented |
| 2 | Platform analytics | `GET /api/v1/reports/platform/` | administrator only | implemented |
| 3 | Audit log | `GET /api/v1/reports/audit-events/` | administrator only | implemented |

The per-user report is deliberately **uncached**: it is read occasionally and is
meant to reflect the newest job the instant it is asked. The small, hot summary
at `/api/v1/auth/usage/` is the endpoint that pays for caching; this one does not.

## 1. User analytics

`GET /api/v1/reports/me/` returns the caller's own ingestion activity, aggregated
in SQL. There is no path parameter and no `user` query parameter — there is no way
to ask it about anyone but yourself. Platform-wide figures are report 2.

### What it measures

| Field | Meaning | SQL |
|---|---|---|
| `totals.jobs` | every job the caller owns | `Count("id")` |
| `totals.successful` | jobs that finished clean | `Count(filter=status="completed")` |
| `totals.partially_successful` | jobs that finished with rejected rows | `Count(filter=status="completed_with_errors")` |
| `totals.failed` | jobs that loaded nothing usable | `Count(filter=status="failed")` |
| `records.processed` | rows the pipeline drew in | `Sum("rows_received")` |
| `records.accepted` | rows written | `Sum("rows_accepted")` |
| `records.rejected` | rows refused | `Sum("rows_rejected")` |
| `durations.measured_jobs` | jobs with both timestamps | `Count(filter=started_at & completed_at set)` |
| `durations.average_seconds` | mean wall-clock run time | `Avg(completed_at - started_at)` |
| `durations.maximum_seconds` | slowest run | `Max(completed_at - started_at)` |
| `most_recent_job` | the newest job by `requested_at` | `order_by("-requested_at").first()` |
| `daily_job_counts` | jobs requested per calendar day | `TruncDate("requested_at")` + `Count` |
| `by_status` | jobs per status | `values("status").annotate(Count)` |
| `by_source` | jobs per source type | `values("source_type").annotate(Count)` |

`pending`, `processing`, and `cancelled` are **not** outcomes, so they are absent
from the three headline buckets but visible in `by_status`. A caller who wants
"how many are still running" reads `by_status.processing`.

### Sample dataset

The fixture below is the one committed in
`apps/reports/tests/test_user_analytics.py`, so the expected results that follow
are asserted by the suite rather than asserted in prose only.

| Job | source | status | requested (UTC) | started | completed | duration | received | accepted | rejected |
|---|---|---|---|---|---|---|---|---|---|
| A | csv | completed | 2024-03-11T10:00:00 | 10:00:00 | 10:00:30 | 30s | 100 | 100 | 0 |
| B | json | failed | 2024-03-11T23:59:00 | 23:59:00 | 2024-03-12T00:00:30 | 90s | 50 | 0 | 50 |
| C | csv | completed_with_errors | 2024-03-12T09:00:00 | 09:00:00 | 09:01:30 | 90s | 20 | 15 | 5 |
| D | api | pending | 2024-03-12T11:00:00 | — | — | — | 0 | 0 | 0 |

Job B is requested at 23:59 and finishes just after midnight, which is what makes
the daily grouping worth checking: the job belongs to **11 March** because that
is when it was *requested*, and the timestamps are UTC.

### Expected aggregate results

```text
totals.jobs                 4
totals.successful           1     (A)
totals.partially_successful 1     (C)
totals.failed               1     (B)

records.processed         170     (100 + 50 + 20 + 0)
records.accepted          115     (100 + 0 + 15 + 0)
records.rejected           55     (0 + 50 + 5 + 0)

durations.measured_jobs     3     (A, B, C; D never started)
durations.average_seconds  70.0   ((30 + 90 + 90) / 3)
durations.maximum_seconds  90.0

daily_job_counts           [{2024-03-11, 2}, {2024-03-12, 2}]
by_status                  {completed: 1, failed: 1, completed_with_errors: 1, pending: 1}
by_source                  {csv: 2, json: 1, api: 1}
most_recent_job            D — api, pending, requested 2024-03-12T11:00:00Z
```

### API examples

```powershell
# The authenticated caller's own report.
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/reports/me/ `
  -Headers @{ Authorization = "Bearer $($tokens.access)" }
```

```json
{
  "user": { "id": 1, "email": "analyst@example.com" },
  "totals": { "jobs": 4, "successful": 1, "partially_successful": 1, "failed": 1 },
  "records": { "processed": 170, "accepted": 115, "rejected": 55 },
  "durations": { "measured_jobs": 3, "average_seconds": 70.0, "maximum_seconds": 90.0 },
  "most_recent_job": {
    "id": "8f0c2e7a-1c4b-4f1a-9c2f-3a5b6d7e8f90",
    "source_type": "api",
    "status": "pending",
    "requested_at": "2024-03-12T11:00:00Z",
    "completed_at": null,
    "rows_received": 0,
    "rows_accepted": 0,
    "rows_rejected": 0
  },
  "daily_job_counts": [
    { "date": "2024-03-11", "count": 2 },
    { "date": "2024-03-12", "count": 2 }
  ],
  "by_status": { "completed": 1, "failed": 1, "completed_with_errors": 1, "pending": 1 },
  "by_source": { "csv": 2, "json": 1, "api": 1 }
}
```

An anonymous request is `401`. A new user with no jobs gets `200` with zeroes and
empty groupings, not a `404`.

## Design notes (interview-ready)

### Conditional aggregation, not three queries

The three outcome buckets are one `aggregate()` call:

```python
Count("id", filter=Q(status="completed"))
```

Django compiles each of these to `SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0
END)`, so all three come back from a single scan of the caller's jobs. Three
separate `.filter(status=...).count()` calls would scan the table three times.

### Why joining two one-to-many tables is wrong

`IngestionJob` has three children: `files`, `validation_errors`, and
`rule_summaries`. If one aggregate joins two of them, the rows multiply *before*
the count:

```python
# One job with 2 files and 3 validation errors.
IngestionJob.objects.aggregate(
    jobs=Count("id"),
    files=Count("files"),
    errors=Count("validation_errors"),
)
# jobs = 9 (not 4), files = 6 (not 2), errors = 6 (not 3)
```

The job contributes a 2 × 3 grid of six rows, and the three childless jobs
contribute one row each. `distinct=True` repairs the counts but not `Sum`, which
is summed over the same duplicated rows. The report therefore **never joins a
child**: it reads the counters stored on the job row and groups one dimension at a
time. `test_joining_job_children_does_not_inflate_the_counts` writes the naive
query and asserts it is wrong.

### `rows_received`, not `rows_processed`

`rows_processed` is the worker's monotonic progress counter and is only written by
the asynchronous path; the synchronous pipeline writes `rows_received` and leaves
`rows_processed` at `0`. Summing `rows_processed` would therefore report
`processed: 0` for every synchronous load. The report sums `rows_received` — the
one counter both paths maintain — and the field name in the response says
"processed" because that is the question the report answers, not the column it
reads.

### Durations are NULL, never zero

A job that never started has no `started_at`/`completed_at`, so
`completed_at - started_at` is `NULL` and both `Avg` and `Max` skip it. Treating
it as zero would drag the average toward zero and understate the slowest run. The
subtraction is done by the database (`TIMESTAMPDIFF` on MySQL, native duration
arithmetic on SQLite) — Django 5.2 compiles temporal subtraction per backend — so
no timestamps cross the wire to be subtracted in Python.

### Dates are UTC calendar days

`TruncDate("requested_at")` groups in the database under `USE_TZ`, so the buckets
are UTC dates. A job requested at 23:59Z belongs to that day even if it finishes
after midnight. That is why job B in the sample sits on 11 March.

### Isolation is a query filter

The report is built from `IngestionJob.objects.filter(created_by=request.user)`.
Scoping in the query — rather than filtering a fetched list in Python — means
another user's jobs are never selected, let alone aggregated, and a later change
cannot accidentally reveal them. There is no parameter that names a user to look
at, so there is no request that could ask for one.

### Empty datasets return zeroes, not errors

SQL `SUM`/`AVG` over zero rows return `NULL`, not `0`. The service coerces the
counters to `0`, leaves the two duration figures as `null` (an absent average is
not `0`), returns `{}` for the groupings, `[]` for the daily counts, and `null`
for the most recent job.

### Cost is constant, and the index is the grouping

The report is five queries regardless of how many jobs the user has: one
aggregate, one grouped read per dimension, and the latest job. It is asserted with
`assertNumQueries(5)`.

The `by_status` grouping and the conditional buckets key on `status`, so migration
`ingestion/0005` adds:

```python
models.Index(fields=["created_by", "status"], name="idx_job_owner_status")
```

with the owner fixed, the status is already ordered inside the index, so the
`GROUP BY` is served by the access path instead of a sort over every row the owner
has. Inspected on the SQLite test backend:

```text
SEARCH ingestion_ingestionjob USING INDEX idx_job_owner_status (created_by_id=?)
```

No `USE TEMP B-TREE FOR GROUP BY` node accompanies it. The owner-only aggregate
uses the existing `idx_job_owner_requested` on `(created_by, -requested_at)`.
`by_source` and the daily counts are *not* covered by a dedicated index: they sort
the owner's (small) job set, and a second and third composite index would cost
write throughput on every job for a read that is already bounded by the owner.

## 2. Platform analytics

`GET /api/v1/reports/platform/` returns the same measurements as report 1, taken
over **every** job rather than one owner's, plus the two questions only a platform
view can answer: who is generating the work, and what is failing. It is guarded by
`IsAuthenticated, IsAdministrator`.

### What it measures

Everything report 1 measures — outcome buckets, record totals, run times,
`by_status`, `by_source`, `daily_job_counts` — plus:

| Field | Meaning | SQL |
|---|---|---|
| `totals.users_with_jobs` | distinct owners with at least one job | `Count("created_by", distinct=True)` |
| `totals.active` | jobs in flight or waiting | `Count(filter=status in {pending, processing})` |
| `top_users[]` | owners ranked by job count | `values("created_by").annotate(Count, conditional Count)` |
| `failures.failed_jobs` | the failed total, restated for the failures block | `Count(filter=status="failed")` |
| `failures.recent[]` | the newest failed jobs, newest first | `filter(status="failed").order_by("-requested_at")[:10]` |
| `failures.top_error_codes[]` | validation rules ranked by rows rejected | `IngestionValidationError.values("error_code").annotate(Count)` |

`top_users` and `failures.recent` are capped at ten (the `TOP_USERS` and
`FAILURE_SAMPLE` constants). A report is a summary, not an export; the full set is
the paginated audit log.

### Sample dataset

The platform fixture in `apps/reports/tests/test_platform_analytics.py`. Two
owners, five jobs, and six validation findings.

| Job | owner | source | status | requested (UTC) | duration | received | accepted | rejected |
|---|---|---|---|---|---|---|---|---|
| a1 | alice | csv | completed | 03-11T10:00 | 30s | 100 | 100 | 0 |
| a2 | alice | csv | completed | 03-11T11:00 | 20s | 80 | 80 | 0 |
| a3 | alice | json | failed | 03-12T08:00 | 10s | 10 | 0 | 10 |
| b1 | bob | xlsx | completed_with_errors | 03-12T09:00 | 60s | 20 | 15 | 5 |
| b2 | bob | api | pending | 03-12T10:00 | — | 0 | 0 | 0 |

Validation findings: **a3** → `missing_symbol` ×2; **b1** →
`non_positive_price` ×4.

### Expected aggregate results

```text
totals.jobs                  5
totals.users_with_jobs       2
totals.successful            2     (a1, a2)
totals.partially_successful  1     (b1)
totals.failed                1     (a3)
totals.active                1     (b2 — pending)

records.processed          210
records.accepted           195
records.rejected            15

durations.measured_jobs      4
durations.average_seconds   30.0   ((30 + 20 + 10 + 60) / 4)
durations.maximum_seconds   60.0

daily_job_counts           [{2024-03-11, 2}, {2024-03-12, 3}]
by_status                  {completed: 2, failed: 1, completed_with_errors: 1, pending: 1}
by_source                  {csv: 2, json: 1, xlsx: 1, api: 1}

top_users                  [alice: jobs 3, successful 2, failed 1]
                           [bob:   jobs 2, successful 0, failed 0]

failures.failed_jobs       1
failures.recent            [a3 — alice, "ValueError: bad header"]
failures.top_error_codes   [non_positive_price: 4, missing_symbol: 2]
```

### API examples

```powershell
# Administrator only: 200 for an administrator, 403 for an analyst, 401 anonymous.
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/reports/platform/ `
  -Headers @{ Authorization = "Bearer $($admin_tokens.access)" }
```

```json
{
  "totals": {
    "jobs": 5, "users_with_jobs": 2, "successful": 2,
    "partially_successful": 1, "failed": 1, "active": 1
  },
  "records": { "processed": 210, "accepted": 195, "rejected": 15 },
  "durations": { "measured_jobs": 4, "average_seconds": 30.0, "maximum_seconds": 60.0 },
  "by_status": { "completed": 2, "failed": 1, "completed_with_errors": 1, "pending": 1 },
  "by_source": { "csv": 2, "json": 1, "xlsx": 1, "api": 1 },
  "daily_job_counts": [
    { "date": "2024-03-11", "count": 2 },
    { "date": "2024-03-12", "count": 3 }
  ],
  "top_users": [
    { "user_id": 1, "email": "alice@example.com", "jobs": 3, "successful": 2, "failed": 1 },
    { "user_id": 2, "email": "bob@example.com", "jobs": 2, "successful": 0, "failed": 0 }
  ],
  "failures": {
    "failed_jobs": 1,
    "recent": [
      {
        "id": "c1d2e3f4-0000-0000-0000-000000000000",
        "created_by_email": "alice@example.com",
        "source_type": "json",
        "status": "failed",
        "requested_at": "2024-03-12T08:00:00Z",
        "completed_at": "2024-03-12T08:00:10Z",
        "rows_received": 10, "rows_accepted": 0, "rows_rejected": 10,
        "last_error": "ValueError: bad header",
        "error_summary": ""
      }
    ],
    "top_error_codes": [
      { "error_code": "non_positive_price", "count": 4 },
      { "error_code": "missing_symbol", "count": 2 }
    ]
  }
}
```

### Design notes (interview-ready)

**The permission is the scope.** Report 1 scopes with
`filter(created_by=request.user)`; this report deliberately has no owner filter,
because a platform report that silently scoped itself would not be one. What makes
it safe is the guard: `permission_classes = [IsAuthenticated, IsAdministrator]`.

**401 before 403.** A view's `permission_classes` *replace* the global default, so
`IsAuthenticated` is listed explicitly beside `IsAdministrator`. Without it, an
anonymous request would fail the role check and earn a `403` when the accurate
answer is `401`. With both: anonymous → `401`, analyst → `403`, administrator →
`200`.

**A many-to-one join is safe; a one-to-many join is not.** The leaderboard groups
by `created_by`, which is a foreign key *to* the user — every job has exactly one
owner, so the join cannot multiply rows. This is the mirror image of the report-1
trap: the rule is not "never join", it is "never fan out across a one-to-many
relation in the same aggregate". Grouping by the owner's `jobs=Count("id")` is
therefore correct, and `Count("created_by", distinct=True)` at platform level
counts *users*, not jobs.

**The failure summary is three facts, not one.** `failed_jobs` is the size (the
same number as `totals.failed`), `recent` names the jobs with their error lines,
and `top_error_codes` says which rule is doing the rejecting. "Three jobs failed"
is only the start of the investigation.

**No new index, measured, not assumed.** Requirements can tempt you to add an index
per report; the right answer is to check. On the SQLite test backend the platform
report's reads are served by indexes that already exist:

```text
by_status       SCAN ingestion_ingestionjob USING INDEX idx_job_status_heartbeat
top_users       SCAN ingestion_ingestionjob USING INDEX ...created_by_id...   (FK index)
recent failures SEARCH ingestion_ingestionjob USING INDEX idx_job_status_heartbeat (status=?)
top_error_codes SCAN ingestion_validationerror USING COVERING INDEX idx_validation_error_code
```

The status grouping and the failure filter use `(status, heartbeat_at)`; the
leaderboard groups on the `created_by` foreign key, which is indexed; and the
error-code ranking is a **covering index** read — it never touches the table. The
one sort left is `recent failures` ordering failed rows by `requested_at`, which a
`(status, -requested_at)` index would remove. It is not added: failed jobs are the
exception on a healthy platform, the existing index already narrows to them, and a
second index on `status` costs a write on every job for an occasional
administrator read. The cost is reported as a constant **seven queries** and
asserted with `assertNumQueries(7)`.

## 3. Audit log

`GET /api/v1/reports/audit-events/` is the one report that returns **stored rows**
rather than an aggregate: a paginated, filterable page of `AuditEvent`s. It is
administrator only, and it is where the events the rest of the application
*writes* are read back.

### What is recorded

The vocabulary is `AuditAction` in `apps/audit/events.py` — deliberately short,
because an audit event is a decision or a privileged action, not a heartbeat:

| Action | Written when | Metadata |
|---|---|---|
| `ingestion.job_created` | `POST /api/v1/ingestion/` creates a job | `source_type` |
| `ingestion.job_queued` | `POST /api/v1/ingestion/submit/` queues one (on creation only) | `source_type`, `on_invalid`, `on_existing` |
| `ingestion.upload_loaded` | `POST /api/v1/ingestion/upload/` loads a file synchronously | `source_type`, `status`, `on_invalid`, `on_existing`, `rows_received/accepted/rejected` |
| `ingestion.job_cancelled` | `POST /api/v1/ingestion/<uuid>/cancel/` actually stops a live job | `source_type` |
| `accounts.role_changed` | Django admin changes a user's role | `email`, `from_role`, `to_role` |

Each row names the actor (`user`, nullable), the action, the target
(`object_type` + `object_id`), the `request_id`, the peer `ip_address`, and the
metadata above. A replay — a `submit/` that returns `created: false` — and a
cancel of an already-terminal job are **not** recorded: neither is a new decision,
and logging them would let a retry loop fill the trail with copies of one action.

### What is never recorded

Audit rows are kept forever, so anything written into one is kept forever too.
Two things enforce the never-log rule rather than trusting the author:

* `NEVER_LOGGED_KEYS` — a case-insensitive deny-list (`password`, `token`,
  `authorization`, `secret`, `api_key`, `session`, `csrf`, …) applied at **every
  level** of a nested payload by `sanitize_metadata`.
* `MAX_STRING_LENGTH` (500) — every string is truncated, so a value that slipped
  past the deny-list still cannot carry a whole token.

`sanitize_metadata` also coerces a non-JSON value (a model, a `Decimal`, a `UUID`)
to its string form, so a caller that passes something unexpected fails *down* to a
string rather than *up* into a serialization error inside an audit write. Never
written, ever: request bodies, file contents, raw ingested rows, passwords,
tokens, API keys, session cookies — and `X-Forwarded-For`, which is
client-controllable, which is why `ip_address` is the direct peer's `REMOTE_ADDR`.

### What is *not* recorded

Reads. A `GET` is not an audit event, and neither is a job moving from `pending`
to `processing` — the pipeline mutates that status many times per job, and each
mutation is **ingestion-job status**, not a decision anyone made. The boundary is
drawn in the section below; the short version: audit events record *actions*, job
status records *state*, application logs record *diagnostics*.

### Sample dataset

The fixture in `apps/reports/tests/test_audit_events.py`: three events over twenty
minutes on 2024-03-12.

| # | actor | action | target | request_id | created_at |
|---|---|---|---|---|---|
| 1 | alice | `ingestion.job_created` | `IngestionJob` / 1 | `req-1` | 00:00:00Z |
| 2 | bob | `ingestion.upload_loaded` | `IngestionJob` / 2 | `req-2` | 00:10:00Z |
| 3 | — (deleted) | `auth.denied` | — | `req-3` | 00:20:00Z |

Event 2 carries `metadata = {"status": "completed", "rows_accepted": 5}`. Event 3
has a **null actor**, which is what `SET_NULL` produces: the event outlives the
account it names and is still reported rather than dropped.

### Expected page

```text
count                                   3
results (newest first)                  [3, 2, 1]
                                          3  actor null,  action auth.denied
                                          2  actor bob,   action ingestion.upload_loaded
                                          1  actor alice, action ingestion.job_created

?action=ingestion.job_created           count 1
?user=ALICE@example.com                 count 1   (email match is case-insensitive)
?since=…T00:05:00Z&until=…T00:15:00Z    count 1   (event 2)
?search=upload                          count 1   (substring of the action)
?page_size=2                            two rows, next set
```

### API examples

```powershell
# Administrator only: 200 for an administrator, 403 for an analyst, 401 anonymous.
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/reports/audit-events/?action=ingestion.upload_loaded&page_size=10" `
  -Headers @{ Authorization = "Bearer $($admin_tokens.access)" }
```

```json
{
  "count": 3,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 3,
      "created_at": "2024-03-12T00:20:00Z",
      "user_email": null,
      "action": "auth.denied",
      "object_type": "",
      "object_id": "",
      "request_id": "req-3",
      "ip_address": "203.0.113.7",
      "metadata": null
    },
    {
      "id": 2,
      "created_at": "2024-03-12T00:10:00Z",
      "user_email": "bob@example.com",
      "action": "ingestion.upload_loaded",
      "object_type": "IngestionJob",
      "object_id": "2",
      "request_id": "req-2",
      "ip_address": "203.0.113.7",
      "metadata": { "status": "completed", "rows_accepted": 5 }
    },
    {
      "id": 1,
      "created_at": "2024-03-12T00:00:00Z",
      "user_email": "alice@example.com",
      "action": "ingestion.job_created",
      "object_type": "IngestionJob",
      "object_id": "1",
      "request_id": "req-1",
      "ip_address": "203.0.113.7",
      "metadata": null
    }
  ]
}
```

### Design notes (interview-ready)

**An audit write fails open.** `record_event` guards its insert: a failure logs at
`ERROR` and returns `None` rather than raising. The event describes an operation
that has already been decided — the upload succeeded, the job was created — so
losing the audit row must not turn a `201` into a `500` or invite a client to
retry a load that already happened. The cost is that a lost event is silent at
the call site, which is exactly why the failure is logged loudly, where an
operator can alert on it. Failing closed would trade availability for a guarantee
the write path cannot really make anyway: the business row and the audit row are
separate statements, so an audit failure after a committed load is not something a
`500` can undo.

**`request_id` correlates; `ip_address` does not trust the client.** A caller may
send `X-Request-ID` to tie an audit row to its own logs; otherwise one is
generated, so the indexed `request_id` is never blank. `ip_address` is
deliberately `REMOTE_ADDR` and **not** `X-Forwarded-For`: the header is
client-controlled unless a trusted proxy rewrites it, and letting a caller choose
the IP in a compliance record is worse than recording none.

**The log needed its own index; the two existing ones could not help.** The
administrator's default read is the **whole table, newest first**, with no `user`
filter — and this is the one table in the project that grows without bound. Before
this report the plan sorted every row:

```text
# before
SCAN audit_auditevent USING INDEX idx_audit_user_created
USE TEMP B-TREE FOR ORDER BY
```

`idx_audit_user_created` is `(user, -created_at)`: a leading `user` column is
exactly what an unfiltered listing does not have, so it could not serve the order.
Migration `audit/0002` adds `(-created_at)`, and the filesort is gone:

```text
# after
SCAN audit_auditevent USING INDEX idx_audit_created
SEARCH accounts_user USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
```

The per-actor filter still uses `idx_audit_user_created (user_id=?)`, and the
`(object_type, object_id)` index still answers "what happened to object Y". The
`action` filter is **not** indexed: `action` is a small vocabulary, the
created-at index already narrows the window, and a further index would cost a
write on every event to speed a read that is already bounded.

**A page does not query once per row.** `audit_event_queryset()` applies
`select_related("user")`, so the actor is joined in the page query;
`test_a_page_does_not_query_once_per_row` asserts a single query for a slice, and
the endpoint adds only the paginator's `COUNT`.

**Numbers in the trail are not the report's numbers.** This report *reads* events;
the writing is `apps.audit.services.record_event`, called from the ingestion views
and the admin's `UserAdmin.save_model`. Keeping the write in one function is what
makes the request-id, the peer address, and the sanitizer apply to every event
instead of to the ones whose author remembered them.

## Audit events vs application logs vs ingestion-job status

Three different records answer three different questions, and conflating them is a
common design mistake.

| Record | Question it answers | Who reads it | Retention | Mutability |
|---|---|---|---|---|
| **Ingestion-job status** (`IngestionJob`) | *What is the state of this load right now?* | the submitting user, the pipeline | as long as the job matters | updated constantly by the pipeline |
| **Audit event** (`AuditEvent`) | *Who did what, to which object, and when?* | administrators, auditors | long; outlives the user | append-only, never edited |
| **Application log** | *What did the process do, for debugging?* | operators during an incident | days to weeks | ephemeral, unstructured |

A job row is the **business state machine**: it moves through `pending →
processing → completed | completed_with_errors | failed | cancelled` and is
mutated on every heartbeat. It is the source of truth for "did this load succeed",
and it is what the user-facing endpoints read.

An audit event is a **compliance record of a decision or a privileged action** —
a file upload, a job being started, a configuration change, a role being edited.
It records the actor, the action, the target, and the request id, and it is never
updated or deleted; `AuditEventAdmin` refuses to add or change rows by hand for
exactly that reason. It is *not* a log line: it is structured, queryable, and
retained, and every row names a user and an object.

An application log is a **diagnostic stream**. It may contain stack traces,
timings, and free text, it is sampled or rotated away, and nobody queries it to
answer "what happened to job X" a month later. Audit events must therefore never
carry secrets, tokens, passwords, or full request bodies — the audit row is kept
forever, so anything written into it is kept forever too. Report 3 defines the
event vocabulary (`AuditAction`) and enforces the never-log list in
`sanitize_metadata`, so the prohibition is a check the write path runs rather than
a note a caller is trusted to have read.

One consequence is worth stating plainly: **reads are not audit events here.** A
`GET` leaves no row. Recording every read would multiply the table by orders of
magnitude, would capture the pipeline's own polling as if it were a person, and
would answer no question an administrator actually asks — the compliance question
is "who changed something", and a read changes nothing. If a deployment ever needs
read auditing, it belongs behind a policy decision (which reads matter, and for
how long) rather than bolted onto this trail.
