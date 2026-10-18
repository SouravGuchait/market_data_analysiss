# Asynchronous ingestion: Celery, Redis, and the job that will not lie

The synchronous ETL in [ingestion_etl.md](ingestion_etl.md) runs a load inside the
request that asked for it. That is the right shape when a file takes two seconds
and the wrong shape when it takes twenty minutes: the request times out, the
client retries, and *the retry starts a second load*.

This document covers the asynchronous path: what runs where, how a worker is
started and watched from a terminal, the nine failure scenarios and how each is
tested, and the four ideas — idempotency keys, uniqueness constraints,
transaction boundaries, and permanent-vs-transient failures — that make the whole
thing safe.

---

## 1. What runs where

```
client ──POST /api/v1/ingestion/submit/──► API process
                                             │ 1. hash the file       (idempotency key)
                                             │ 2. INSERT the job row  (durable, unique)
                                             │ 3. store the file      (MEDIA_ROOT)
                                             │ 4. commit
                                             │ 5. task.delay(job_id)  ◄── only after 4
                                             ▼
                                         Redis (broker: one list per queue)
                                             │
                                             ▼
                                       Celery worker ──► run_ingestion(...)
                                                          │
                                                          └──► MySQL (the truth)
```

**Redis is the broker only.** There is no result backend. That is a decision, not
an omission:

| Question | Where it is answered |
|---|---|
| Did this load succeed? | `IngestionJob.status` |
| How many rows were accepted? | `IngestionJob.rows_accepted` |
| Which rows were rejected, and why? | `IngestionValidationError` |
| Which rule failed? | `IngestionRuleSummary` |
| Is anything queued right now? | Redis (`LLEN`) — the *only* thing Redis is asked |

A result backend would be a second store of "job status", and two stores of the
same fact eventually disagree. Worse, Redis is commonly configured with an
eviction policy: a cache that drops `job:result:...` under memory pressure would
silently erase the record of a successful load. Keeping business truth in MySQL
means **losing Redis loses no jobs.** A queued job whose message vanished is still
a `pending` row, and `requeue_stale_jobs` (or a resubmission) puts it back.

### The state machine

```
                 submit                    claim_job()                run_ingestion()
   (new) ──────────────► PENDING ──────────────► PROCESSING ──────────────► COMPLETED
                            ▲                        │                       COMPLETED_WITH_ERRORS
                            │                        │                       FAILED
                            │   transient failure    │                       CANCELLED
                            └────────────────────────┘
                              (handed back for retry)

   PENDING ── cancel ─────────────────────────────────────────────────────► CANCELLED
   PROCESSING ── cancel ──► (worker notices at the next chunk boundary) ──► CANCELLED
```

`FAILED` and `CANCELLED` are terminal, as are both `COMPLETED*`. A terminal job is
never claimed again — that is what makes a redelivered message a no-op.

---

## 2. Running a worker from a terminal

Redis must be running. On Windows, `solo` is the default pool because Celery's
`prefork` pool needs `fork()`, which Windows does not have.

### The convenience wrapper

```powershell
# Leave this running in one terminal.
.\scripts\run_worker.ps1

# In another terminal:
.\scripts\run_worker.ps1 -Action status     # broker + database + queue depth + job counts
.\scripts\run_worker.ps1 -Action inspect    # what are the live workers doing?
.\scripts\run_worker.ps1 -Action list       # registered task names
.\scripts\run_worker.ps1 -Action purge      # delete queued messages (job rows are untouched)

.\scripts\run_worker.ps1 -LogLevel debug
.\scripts\run_worker.ps1 -Queue ingestion -Concurrency 8
```

### The raw Celery commands

The wrapper is only a shortcut; these are what it runs.

```powershell
# Development / Windows: one task at a time, in the worker process.
python -m celery -A config worker --loglevel=info --pool=solo

# Linux / production: a pool of child processes.
python -m celery -A config worker --loglevel=info --concurrency=8 --max-tasks-per-child=100

# A second worker consuming only one queue, so a slow queue cannot starve a fast one.
python -m celery -A config worker --loglevel=info -Q ingestion

# Enable debug logging for the task module only.
python -m celery -A config worker --loglevel=info
```

`-A config` resolves through `config/__init__.py` to the app in `config/celery.py`.

A healthy worker prints the task registry as it starts:

```
[tasks]
  . ingestion.process_job
  . config.celery.debug_task
```

If `ingestion.process_job` is missing from that list, `apps/ingestion/tasks.py`
failed to import — the traceback is printed just above.

### Inspecting a running worker

```powershell
python -m celery -A config inspect active      # jobs executing right now
python -m celery -A config inspect reserved    # prefetched but not started
python -m celery -A config inspect stats       # per-worker totals and pool size
python -m celery -A config inspect ping        # is a worker listening at all?
```

> **`inspect` asks workers, not the broker.** With no worker running it prints an
> empty reply, which looks exactly like an empty queue. That is why
> `ingestion_status` reads the broker's list length for queue depth and asks
> MySQL for job counts, and only uses `inspect` for "what is a worker doing".

### Queue and job state in one command

```powershell
python manage.py ingestion_status
python manage.py ingestion_status --json      # for a monitoring probe
```

```
database : up
broker   : up
queue    : 0
jobs     :
  completed                       12
  completed_with_errors            2
  pending                          1
stale    : 0 processing job(s) with a cold heartbeat
```

Exit code is `2` when the database is unreachable — the one failure that makes the
numbers above it meaningless.

### Recovering abandoned jobs

```powershell
python manage.py requeue_stale_jobs                 # requeue, or fail after too many attempts
python manage.py requeue_stale_jobs --dry-run       # report only
python manage.py requeue_stale_jobs --no-requeue    # reset state, do not publish
python manage.py requeue_stale_jobs --older-than 60 # treat 60s without a heartbeat as stale
```

Run it on a schedule (every few minutes). It is idempotent: the second run finds
nothing, because the first one moved the jobs out of `processing`.

### Running tasks inline, without a broker

```powershell
# Run the load in-process instead of queueing it — handy for a demo.
$env:CELERY_TASK_ALWAYS_EAGER = "1"
python manage.py migrate
```

---

## 3. Submitting work

### Over HTTP

```powershell
$tokens = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/auth/login/ `
    -Method Post -ContentType application/json `
    -Body (@{ email = "analyst@example.com"; password = "drf-portfolio-42!" } | ConvertTo-Json)

# Submit: returns 202 immediately. Nothing has been loaded yet.
$job = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/ingestion/submit/ `
    -Method Post -Headers @{ Authorization = "Bearer $($tokens.access)"; "Idempotency-Key" = "my-op-001" } `
    -Form @{ file = Get-Item .\sample_data\synthetic\batches\prices_batch_001.csv }

$job.job.id    # poll this
$job.created   # $true the first time, $false on a replay

# Poll status while a worker chews through it.
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ingestion/$($job.job.id)/" `
    -Headers @{ Authorization = "Bearer $($tokens.access)" }

# Once it is terminal, read the data-quality verdict.
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ingestion/$($job.job.id)/report/" `
    -Headers @{ Authorization = "Bearer $($tokens.access)" }

# Ask it to stop.
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ingestion/$($job.job.id)/cancel/" `
    -Method Post -Headers @{ Authorization = "Bearer $($tokens.access)" }
```

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/ingestion/submit/` | store, record, and **queue** a file. `202` created, `200` replay |
| `POST /api/v1/ingestion/upload/` | store, record, and **run** a file in the request. `201` |
| `GET /api/v1/ingestion/<uuid>/` | status and progress |
| `GET /api/v1/ingestion/<uuid>/report/` | the data-quality report |
| `POST /api/v1/ingestion/<uuid>/cancel/` | cooperative cancellation |

### From Python

```python
from apps.ingestion import services

job, created = services.submit_upload(
    user=user,
    uploaded_file=request.FILES["file"],
    policy=IngestionPolicy(on_invalid="skip", on_existing="update"),
    idempotency_key=request.headers.get("Idempotency-Key"),
)

services.enqueue_ingestion_job(job)   # only after the job row is committed
services.cancel_job(job)
```

---

## 4. The nine scenarios

Each row below is a real test. Run them all with:

```powershell
python -m pytest apps/ingestion/tests/test_tasks.py apps/ingestion/tests/test_async_api.py apps/ingestion/tests/test_failures.py -v
```

| # | Scenario | What must happen | Where it is tested |
|---|---|---|---|
| 1 | **A worker crashes halfway through** | The committed chunks survive; the row is left `processing` with a cold heartbeat; `requeue_stale_jobs` returns it to `pending`; the retry resumes from `checkpoint.row_offset` instead of restarting | `test_a_crashed_attempt_resumes_from_its_checkpoint`, `test_stale_jobs_are_only_those_with_a_cold_heartbeat`, `test_requeue_stale_jobs_returns_an_abandoned_job_to_pending` |
| 2 | **The same file is uploaded twice** | One job. The SHA-256 of the bytes is the idempotency key, so the second submission is recognised by a unique constraint, not by a `SELECT` | `test_the_same_file_submitted_twice_is_one_job` |
| 3 | **A task executes more than once** | The second delivery is a no-op. First because a terminal job is never claimed; then because `claim_job`'s conditional `UPDATE` only matches `pending` | `test_a_redelivered_message_for_a_finished_job_does_nothing`, `test_only_one_delivery_can_claim_a_pending_job` |
| 4 | **An external API fails temporarily** | Classified transient, retried with backoff, **not** failed | `test_a_vendor_api_timeout_reaches_the_retry_path`, `test_a_temporary_api_failure_is_transient` |
| 5 | **MySQL becomes unavailable** | Classified transient; the job is handed back to `pending`; if the bookkeeping write fails too, the original error still propagates rather than being masked | `test_a_transient_failure_hands_the_job_back_and_asks_for_a_retry`, `test_a_database_outage_does_not_stop_the_retry_decision` |
| 6 | **Redis becomes unavailable** | No job is lost: the row is already committed before `delay()` is called, so the submission is recoverable by resubmitting or by `requeue_stale_jobs` | `test_a_broker_outage_leaves_a_durable_job_behind`, `test_a_broker_outage_is_recoverable_by_resubmitting`, `test_requeue_stale_jobs_survives_the_broker_being_down` |
| 7 | **A job runs for 20 minutes** | Chunked commits, so no single transaction is held for the duration; `heartbeat_at` advances so the sweeper does not mistake a busy worker for a dead one; soft and hard time limits bound the attempt | `test_a_chunked_load_commits_each_chunk_and_records_progress` |
| 8 | **Only some rows are valid** | Not a failure. The good rows load, each bad row is recorded, the job ends `completed_with_errors` | `test_a_partly_valid_file_completes_with_errors` |
| 9 | **A client times out and retries** | With an `Idempotency-Key` the retry maps to the **same** job even if the bytes differ; without one, identical bytes still collapse | `test_a_client_idempotency_key_makes_a_retry_safe`, `test_a_client_supplied_idempotency_key_governs_the_replay` |

A tenth, unglamorous case is also covered because it is the one people forget:
**a job with no file** (`test_a_job_with_no_source_file_is_failed_without_reading_anything`)
and **an API source submitted to the queue**
(`test_an_api_source_is_refused_by_the_worker_with_a_reason`). Both fail
permanently with a message that says what is wrong, instead of hanging.

---

## 5. Idempotency keys and unique constraints

Three different "don't do it twice" problems, solved by three different
mechanisms. Conflating them is the usual source of duplicate rows.

### 5.1 The submission key — "is this the same request?"

```python
# apps/ingestion/services.py
digest = storage.checksum_of_upload(uploaded_file)
key = (idempotency_key or f"file:{digest}")[:128]
```

* **Client-supplied** (`Idempotency-Key: my-op-001`): the client knows it is
  retrying the *same operation*. This survives the bytes changing in transit.
* **Derived** (SHA-256 of the upload): the fallback when the client says nothing.
  Identical bytes are the same submission.

Enforced by the database:

```python
class Meta:
    constraints = [
        models.UniqueConstraint(
            fields=["created_by", "idempotency_key"],
            name="uniq_job_owner_idempotency_key",
        ),
    ]
```

Three things about that constraint are deliberate:

1. **It is a constraint, not a check.** A read-then-write `if exists(...)` leaves
   a window in which two concurrent requests both see "no existing job" and both
   insert. The `INSERT` is the arbiter; the loser catches `IntegrityError` and
   looks up the winner:

   ```python
   try:
       with transaction.atomic():
           job = IngestionJob.objects.create(...)
       return job, True
   except IntegrityError:
       return IngestionJob.objects.get(created_by=..., idempotency_key=...), False
   ```

2. **It is scoped to the owner.** Idempotency is a property of a *client's*
   submission, not of the bytes. Two users uploading the same file on the same
   second must get two jobs; anything else leaks one user's job id to another.

3. **`NULL` means "no dedupe".** `POST /api/v1/ingestion/` creates a job with no
   file and so nothing to deduplicate. SQL treats each `NULL` as distinct in a
   unique index (MySQL and SQLite both), so those rows do not collide.

### 5.2 The file checksum — "is this the same bytes for this job?"

`IngestionFile` carries `UniqueConstraint(fields=["job", "checksum"])`. Re-uploading
identical bytes to the same job is a duplicate file, not a second artifact.

### 5.3 The row upsert — "is this the same (instrument, date)?"

`PriceRecord` is unique on `(instrument, trading_date)`. Re-processing a chunk —
which is exactly what a retry does — updates the row rather than inserting a
second one:

```python
# PostgreSQL / SQLite need a conflict target; MySQL's ON DUPLICATE KEY UPDATE
# takes none. The kwargs are chosen from the connection's capabilities.
kwargs = upsert_kwargs(supports_conflict_target=connection.features.supports_update_conflicts_with_target)
```

This is the layer that makes a *retry* safe even when the retry restarts from the
beginning of the file. That is the fallback when a resume is not possible.

---

## 6. Transaction boundaries

There are two shapes, and which one is in use is the interesting part.

### Synchronous (default): one transaction for the whole load

```
BEGIN ── read ── validate ── transform ── upsert ──┐
                                                   ├── COMMIT  (or ROLLBACK, all of it)
                              errors, summaries ───┘   (written after, so they survive a rollback)
```

One rule, easy to state: **a job that ends `failed` has loaded nothing.**
`on_invalid="abort"` (all-or-nothing) depends on this, and it is only implementable
this way.

### Asynchronous: one transaction per chunk

```
BEGIN ── flush chunk 1 ── write its errors ── checkpoint ── COMMIT
BEGIN ── flush chunk 2 ── write its errors ── checkpoint ── COMMIT
...
```

`IngestionPolicy(commit_each_chunk=True)`, which the worker always sets (unless
the submitter asked for `abort`). The reasons:

* **Lock duration.** A 20-minute load holding one write transaction blocks
  writers and grows a large undo log. Per-chunk commits keep each transaction
  short.
* **Crash cost.** A crash loses at most the uncommitted chunk, and the job row
  records which chunk that was.
* **Progress.** `heartbeat_at` and `rows_processed` advance with each commit, so
  progress is *observed*, not guessed.

The checkpoint is written **inside the same transaction as the rows it describes**:

```python
with transaction.atomic():
    persisted = _merge(persisted, _flush(job, buffer, symbols, policy))
    chunks += 1
    _checkpoint(job, checkpoint_payload(), received, tally)   # same commit
    IngestionValidationError.objects.bulk_create(errors)
```

That ordering matters. A checkpoint that commits separately could claim progress
that a rollback then erased — and the next attempt would skip rows that were never
written, silently losing them.

### The two shapes are mutually exclusive with `abort`, on purpose

```python
if self.commit_each_chunk and self.on_invalid == "abort":
    raise ValueError("commit_each_chunk=True cannot be combined with on_invalid='abort'")
```

"Abort" promises the batch is all-or-nothing. By the time a bad row is seen in a
chunked load, earlier chunks are committed. Quietly weakening one promise is worse
than refusing the combination, so the code refuses it and `tasks.policy_for()`
honours `abort` by falling back to a single transaction.

---

## 7. Permanent versus transient

The rule, stated once:

> **Transient means the same work could succeed if attempted again.**
> **Permanent means it will fail the same way next time.**

| Exception | Verdict | Why |
|---|---|---|
| `OperationalError("MySQL server has gone away")` | transient | the connection dropped; a new one works |
| `OperationalError("Deadlock found…")` | transient | the other transaction finished |
| `redis.exceptions.RedisError` | transient | a broker blip |
| `requests.exceptions.ConnectTimeout` | transient | the vendor was slow |
| `IntegrityError` | **permanent** | a duplicate will be a duplicate again |
| `SourceFormatError`, `UnsafeFileError` | permanent | the file will not change |
| `ValueError`, `KeyError` | permanent | a bug or a bad row |

Two details in `apps/ingestion/etl/failures.py`:

* **The whole chain is inspected.** Wrapped errors are the norm: an
  `OperationalError` surfaces inside whatever framework was talking to the
  database. `_chain()` walks `__cause__`/`__context__`.
* **A permanent marker anywhere wins.** An `IntegrityError` wrapped in a
  connection error is still a constraint violation, and retrying it just burns
  the budget and delays the operator learning the truth.

### Retry policy

```python
@shared_task(bind=True, name="ingestion.process_job", acks_late=True, reject_on_worker_lost=True)
```

* `max_retries=5` (`INGESTION_MAX_RETRIES`)
* `countdown = min(cap, base * 2**attempt)` with **full jitter**, a uniform draw
  from `[0, ceiling]` — `base=10s`, `cap=900s`

Jitter is not decoration. When many jobs fail together — a database blip — a
deterministic backoff makes every worker retry in lockstep and re-creates the
spike that caused the failure.

`acks_late` + `reject_on_worker_lost` give **at-least-once** delivery: a worker
that dies hands its message back. Chapter 8 is about what that implies.

### What retrying means for the job row

A transient failure **hands the job back to `pending`** so the retry can claim it
again — `claim_job` only matches `pending`, so leaving it `processing` would make
the retry a silent no-op:

```python
IngestionJob.objects.filter(pk=job.pk).update(status=JobStatus.PENDING, last_error=detail)
raise task.retry(exc=exc, countdown=countdown, max_retries=max_retries)
```

If the database is what failed, that write fails too. It is wrapped in its own
`try/except` and **logged, never re-raised**: masking the original error with a
bookkeeping failure would leave an un-acked message nobody can diagnose. The
message is still in the broker (`acks_late`), and `requeue_stale_jobs` rescues the
row once the database returns.

---

## 8. Why exactly-once cannot simply be assumed

This is the part worth being precise about, because "the queue will handle it" is
how duplicate rows get written.

**A broker gives at-least-once delivery, not exactly-once.** Consider what happens
between the worker finishing the load and the broker recording that it did:

```
worker:  load rows ──► COMMIT ──────────────► ACK ──► broker forgets the message
                                     ▲
                        worker is killed here
                                     │
              the message is never acked, so the broker re-delivers it
```

The load happened; the acknowledgement did not. There is no ordering of "commit
the data" and "acknowledge the message" that avoids this:

* **Ack before committing** → a crash between them loses the work entirely.
  That is *at-most-once*, and losing a load is worse than repeating one.
* **Commit before acking** (what we do) → a crash between them repeats the work.
  That is *at-least-once*.

True exactly-once needs a distributed transaction spanning the database and the
broker, which MySQL and most brokers do not offer. So the only sound engineering
answer is: **make repetition harmless.** Which is what the whole design is for.

Where repetition is absorbed:

| Layer | Mechanism | Absorbs |
|---|---|---|
| Submission | unique `(created_by, idempotency_key)` | the client retrying, the browser resubmitting |
| Claim | conditional `UPDATE … WHERE status='pending'` | two workers racing, one message delivered twice |
| Terminal check | `job.is_terminal` → return immediately | a redelivery of a finished job |
| Rows | upsert on `(instrument, trading_date)` | a retry re-processing rows already written |
| Checkpoint | committed with the rows it counts | a resume double-counting |

Note that the last three are *different* mechanisms for *different* double-executions.
An idempotency key would not stop a redelivered message, and a conditional claim
would not stop a client resubmitting. They are not redundant; they are layered.

---

## 9. Distinguishing broker, cache, and database failure

Three dependencies, three completely different consequences. Reporting them as one
"unhealthy" light is the mistake `apps/ingestion/monitoring.py` exists to avoid.

| Failure | Symptom | What breaks | What does not |
|---|---|---|---|
| **Database down** | 500s, workers die on the first query | everything: no state can be read or written | — nothing can be done until it returns |
| **Broker down** | `submit/` returns an error, jobs pile up `pending` | nothing can be *queued* | nothing is lost: the row is the truth; jobs run after a requeue |
| **Cache down** (Redis as cache) | pages are slower | page-load latency | **no job state changes at all** — none lives in Redis |

The broker and the cache often share a server, which is exactly why the distinction
has to be made explicitly. `monitoring.health()` probes each one separately:

```
database : up        ← SELECT 1
broker   : up        ← PING on the broker URL
queue    : 0         ← LLEN on the queue (None, not 0, when it cannot be asked)
jobs     : {…}       ← GROUP BY status
stale    : 0         ← PROCESSING with a cold heartbeat
```

`queue_depth()` returns `None` when the broker cannot be reached, **not `0`**.
"Could not ask" and "nothing waiting" need different responses, and a `0` would
read as healthy.

Queue depth is read from Redis directly rather than through Celery's `inspect`,
because `inspect` broadcasts to *workers*: with no workers it returns nothing,
which is the same answer it gives for an empty queue.

---

## 10. Testing

### Mocked tasks — no broker, no worker process

The API layer is tested by patching the handoff, so a test asserts *that work was
queued* without any Redis:

```python
from unittest import mock
from apps.ingestion import tasks

def queued_task(task_id="task-1"):
    return mock.patch.object(
        tasks.process_ingestion_job, "delay", return_value=mock.Mock(id=task_id)
    )

with queued_task("task-abc") as delay:
    job, created = services.submit_upload(user=user, uploaded_file=upload())
assert delay.call_args.args == (str(job.pk),)
```

The task body is tested with `.apply()`, which runs the **real** pipeline with a
populated `request` — no broker, no worker, but nothing below the task mocked:

```python
outcome = tasks.process_ingestion_job.apply(args=[str(job.pk)]).get()
assert outcome == f"completed:{job.pk}"
```

Retry *decisions* are tested against a small recording stand-in for the bound
task, because driving them through Celery's own retry machinery would use the
thing under test to verify itself:

```python
class _RecordingTask:
    def __init__(self, retries=0):
        self.request = _FakeRequest(retries)
        self.retry_kwargs = None
    def retry(self, **kwargs):
        self.retry_kwargs = kwargs
        raise Retry("retry requested")
```

Note that `CELERY_TASK_ALWAYS_EAGER` is **off** by default: an accidental real
`.delay()` in a test fails loudly instead of quietly running a load inside a
request.

### A real worker, locally

Two terminals.

```powershell
# Terminal 1 — the worker.
.\scripts\run_worker.ps1 -LogLevel debug

# Terminal 2 — submit something and watch it move.
$job = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/ingestion/submit/ `
    -Method Post -Headers @{ Authorization = "Bearer $($tokens.access)" } `
    -Form @{ file = Get-Item .\sample_data\synthetic\batches\prices_batch_001.csv }

Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ingestion/$($job.job.id)/" `
    -Headers @{ Authorization = "Bearer $($tokens.access)" }
```

Terminal 1 shows the task being received and finishing:

```
[INFO/MainProcess] Task ingestion.process_job[8f2c...] received
[INFO/MainProcess] job_id=... finished status=completed received=60 accepted=60 rejected=0 attempts=1
[INFO/MainProcess] Task ingestion.process_job[8f2c...] succeeded in 0.42s: 'completed:...'
```

### Crash the worker on purpose

```powershell
# Terminal 1: start a worker, then submit a large file.
.\scripts\run_worker.ps1

# Terminal 2: kill -9 the worker mid-load (close the window, or):
Get-Process | Where-Object { $_.ProcessName -like "*python*" } | Stop-Process -Force

# Terminal 3: the row is stranded in `processing` with a cold heartbeat.
python manage.py ingestion_status

# Recover it — the job resumes from its checkpoint.
python manage.py requeue_stale_jobs
```

### Stop Redis on purpose

```powershell
# Windows: stop the Redis service; Docker: `docker stop redis`
python manage.py ingestion_status     # broker : down, queue : unknown
# Submissions now fail — but the job row exists and is `pending`.
# Start Redis again and recover:
python manage.py requeue_stale_jobs
```

### The test commands

```powershell
# The whole suite (SQLite, in-memory, a few seconds).
python -m pytest -q

# The async layer only.
python -m pytest apps/ingestion/tests/test_tasks.py -v
python -m pytest apps/ingestion/tests/test_async_api.py -v
python -m pytest apps/ingestion/tests/test_failures.py apps/ingestion/tests/test_monitoring.py -v

# One scenario at a time.
python -m pytest -k "resume or stale or crash" -v
python -m pytest -k "transient or permanent or backoff" -v
python -m pytest -k "idempot or duplicate or twice" -v

# With log output, to see the worker's own decisions.
python -m pytest apps/ingestion/tests/test_tasks.py -v --log-cli-level=INFO
```

### One failure at a time

The scenarios are implemented one per test, deliberately. A single test that
"makes everything fail" cannot tell you *which* guarantee broke. Working through
them in order — redelivery, then crash, then database, then broker, then partial
validity — means each test has exactly one reason to fail.

---

## 11. Settings reference

| Setting | Default | Meaning |
|---|---|---|
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Redis as broker (`redis` is the Compose service name) |
| `CELERY_RESULT_BACKEND` | `None` | deliberately unset |
| `CELERY_TASK_IGNORE_RESULT` | `True` | results are not stored anywhere |
| `CELERY_TASK_ACKS_LATE` | `True` | ack after the task returns |
| `CELERY_TASK_REJECT_ON_WORKER_LOST` | `True` | redeliver on worker death |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | one message in flight per process |
| `CELERY_TASK_TIME_LIMIT` | `1800` | hard limit; the worker is killed |
| `CELERY_TASK_SOFT_TIME_LIMIT` | `1500` | raises `SoftTimeLimitExceeded` first |
| `CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP` | `True` | a worker can start while Redis is away |
| `CELERY_TASK_ALWAYS_EAGER` | `False` | off so a stray `.delay()` fails loudly in tests |
| `INGESTION_MAX_RETRIES` | `5` | attempts after the first |
| `INGESTION_RETRY_BACKOFF_SECONDS` | `10` | exponential base |
| `INGESTION_RETRY_BACKOFF_MAX_SECONDS` | `900` | backoff cap |
| `INGESTION_STALE_AFTER_SECONDS` | `900` | no heartbeat for this long ⇒ abandoned |
| `INGESTION_HEARTBEAT_SECONDS` | `5` | how often a running task refreshes progress |

All are environment-overridable through `.env`.

---

## 12. Performance on large files

A load is designed to be **streaming and chunk-bounded**: the only state that
grows with the file is the file itself. Four things make that true, and two of
them were only fixed once they were measured.

### The shape of the work

| Stage | Cost | Notes |
|---|---|---|
| Read | O(1) memory | `sources.py` yields `RawRow` lazily, row by row. Nothing reads the file whole. |
| Validate + normalise | O(1) per row | no cross-row state except the `chunk_size` buffer |
| Upsert | O(chunk) | `bulk_create(batch_size=500, update_conflicts=True)` |
| Errors | O(`max_errors`) | capped, with the overflow *counted* rather than dropped |
| Heartbeat | time-based | one `UPDATE` per `INGESTION_HEARTBEAT_SECONDS` (default 5), not per chunk |
| Symbol lookup | 1 query per run | `SymbolIndex.load()` — the classic N+1, avoided |
| Hashing an upload | O(1) memory | `checksum_of_upload` streams in 64 KB chunks |

### Measured: queries scale with chunks, not rows

```
rows=12,000 chunk=1,000 chunks=  12  1.80s  6,654 rows/s  queries= 80  6.7/chunk  6.67/1000 rows
rows=24,000 chunk=1,000 chunks=  24  3.34s  7,177 rows/s  queries=152  6.3/chunk  6.33/1000 rows
rows=24,000 chunk=  500 chunks=  48  3.37s  7,128 rows/s  queries=248  5.2/chunk 10.33/1000 rows
```

Doubling the rows doubles the *chunks* and therefore the queries; the count per
chunk does not move. Six statements per thousand rows is the number that proves
there is no per-row query: one candidate-existence lookup, the batched upsert,
the checkpoint, and the transaction bookkeeping around them.

The per-chunk candidate lookup passes **distinct** instrument ids and dates. A
thousand-row chunk from twelve instruments would otherwise send MySQL a
thousand-element `IN` list with twelve distinct values in it.

### Measured: the two structures that used to grow with the file

Both were replaced by bounded forms, and the numbers below are from a real 36,000
row run against MySQL (12 symbols × 3,000 days, `chunk_size=1000`):

| Structure | Before | After | Ratio |
|---|---|---|---|
| Checkpoint payload written over the whole run | 8.89 MiB | 10.91 KiB | **835×** |
| In-memory calendar-gap state | ~1.76 MiB (12,000 `date` objects) | 4.4 KiB (bitmap) | **399×** |

The checkpoint number is the more serious of the two, because it is *quadratic*:
a `commit_each_chunk` run writes one checkpoint per chunk, so a payload that
carries the dates makes the total written grow as `rows² / chunk_size`. Ten times
the rows would have meant a hundred times the bytes — roughly 890 MiB for a
360,000-row file, all of it rewritten JSON. Today the payload is a span and a
symbol list, so it is flat.

The calendar state is now a bitmap: one bit per *day* rather than one `date`
object per *record*. A decade of daily data for one instrument is under a
kilobyte. The base is byte-aligned so that a source arriving newest-first grows
the map with a byte-wise prepend instead of a bit shift, and both growth
directions are amortised.

### What was deliberately not traded away

The obvious way to make the gap check cheap was to ask the table "does this
symbol have a row for this expected day?" instead of tracking what the file
contained. That is faster and *wrong*: a day loaded by a different, earlier job
would be reported as present, silently hiding a genuine gap in the file being
loaded. Two tests pin the distinction —
`test_a_gap_is_judged_against_this_file_not_the_whole_table` and
`test_a_resumed_run_still_reports_a_day_it_never_saw`. The second is why a
resumed run re-derives its earlier dates by streaming the rows *this job* wrote
(`_Coverage.seed_from_storage`), rather than starting the check from the resume
point.

### Where the time actually goes

A `cProfile` run over 12,000 rows put **73% of the time in the commit path**, and
inside that, Django's own SQL compilation (`as_sql`, `prepare_value`) cost roughly
twice as much as MySQL executing the statement. That is inherent to
`bulk_create` with `update_conflicts`, not a query pattern problem. If throughput
ever needed to go higher, the next step would be a raw `executemany` or a
driver-level bulk load — at the cost of the portable upsert and the ORM's
type handling. Worth naming as a trade rather than doing pre-emptively.

### Reproducing these numbers

```powershell
# A real run against the dev database, with a generated file of the size you care about.
python -m pytest apps/ingestion/tests/test_pipeline_coverage.py -v
python -m pytest -k "checkpoint or bitmap or resume" -v
```

The invariants are asserted rather than described, so a regression fails a test
instead of quietly making large files impossible:

* `test_the_checkpoint_does_not_grow_with_the_number_of_rows` — bounds *any*
  checkpoint payload on a 400-row/40-chunk run;
* `test_a_decade_of_one_instruments_dates_costs_under_two_kilobytes` — the
  bitmap's memory claim;
* `test_a_resumed_run_still_reports_a_day_it_never_saw` — the resume path;
* `test_a_gap_is_judged_against_this_file_not_the_whole_table` — the semantics.

---

## 13. What is deliberately *not* here

Naming the trade-offs is part of the design.

* **No periodic scheduler (Celery beat).** `requeue_stale_jobs` is a management
  command designed to be run from cron, a systemd timer, or a Windows scheduled
  task. Adding beat means a second long-running process and a schedule stored in
  the database, for one command.
* **No Flower.** `ingestion_status`, `inspect`, and structured logs cover the
  operational questions. Flower is a good answer when someone is actually
  watching a dashboard.
* **No API-source jobs on the queue.** An API `fetcher` is a Python callable and
  cannot travel through a queue — only data can. Accepting a URL from a client
  would be an SSRF hole, so the worker refuses such a job with a message saying
  exactly that, and API ingestion stays on the synchronous path where the caller
  supplies the fetcher in process. Closing this properly means storing a request
  descriptor behind a host allowlist.
* **No mid-chunk resume for a different file.** The checkpoint's `row_offset` is
  a row number, which is only stable if re-reading the source yields rows in the
  same order. That holds for a file and is why only file-backed sources run on a
  worker.
* **No exactly-once.** See chapter 8. It is not available, and pretending
  otherwise is how duplicate rows get written.
