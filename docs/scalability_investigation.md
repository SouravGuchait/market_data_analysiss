# Controlled scalability investigation

Date: 2026-10-18 (Asia/Calcutta)

## Scope and evidence rules

This is an investigation record, not a production-capacity claim. A number is
reported only when it came from a saved benchmark artifact or from a command
run during this investigation. The requested live matrix could not be run:
the first database operation failed with `django.db.utils.OperationalError:
(2002, "Can't connect to server on '127.0.0.1' (10013)")`. `mysql` and
`redis-cli` are also not on PATH. All live 1,000/100,000/1,000,000-row,
concurrent, ingestion, and worker results below are therefore **unverified**.

## What was inspected

The project already has a deterministic read harness at
`scripts/bench_api.py`. It seeds a named `BENCH` exchange, captures cold/warm
query counts, latency, response bytes, and `EXPLAIN`, and writes JSON. It is a
read benchmark: it uses `bulk_create` and does not measure ingestion.

The saved artifacts in `docs/benchmarks/` describe a 100-instrument,
50,000-price, 200,000-snapshot MySQL run with 15 timed repetitions. They are
historical evidence from 2026-10-17, not a result of this run.

## Hypotheses and experiment matrix

| Experiment | Hypothesis | Controlled change | Status / measurement |
|---|---|---|---|
| 1,000 records | Indexed point/range reads remain stable; serialization becomes a larger share of latency | dataset size only | **Unverified**; run harness with a fixed seed and repeat count |
| 100,000 records | Composite `(instrument, trading_date)` access avoids a fact-table scan for instrument history | dataset size only | **Unverified** |
| 1,000,000 records | Index and buffer-pool behavior, not ORM code, becomes dominant; memory/ingestion time may become material | dataset size only | **Unverified / practical limit unknown** |
| 10+ concurrent API clients | Independent reads scale until DB connections, CPU, or lock/cache contention saturate | concurrency only | **Unverified** |
| repeated analytics | Stored snapshot endpoints benefit from cache hits; computed series are not generally cached | repeat/cache state only | Historical artifacts show cache hits on the configured cached cases; Redis concurrency is **unverified** |
| chunked CSV | Fixed-size chunks bound Python memory and commit duration, at the cost of more commits/checkpoints | chunk size only | **Unverified live**; implementation/docs inspected |
| background jobs + API | Worker writes compete for DB connections/IO and can increase API tail latency | worker concurrency only | **Unverified** |

For every run, record: dataset seed and row counts; Python/Django/DB/Redis
versions; machine CPU/RAM; worker count; connection settings; chunk size;
request mix; warm-up; sample count; median/p95/p99 latency; requests/sec;
HTTP errors; Django query count; ingestion seconds and rows/sec; process RSS;
queue depth and oldest-job age. Keep the same database, schema, cache state,
request mix, and duration between A/B runs, changing one variable only.

## Evidence already present in the repository

The saved 100-instrument benchmark's `price_query_ab` reports the same MySQL
access path for joined and lean price queries: an index lookup using
`uniq_price_instrument_trading_date`, estimated 500 rows, followed by a limit.
The recorded per-query timings were 1.475 ms with the join and 0.833 ms
without it over 300 rounds. This supports keeping `select_related` only when
the representation needs the related object; it does not establish behavior
at one million rows.

The saved API artifacts report 2 queries for the instrument list cases and
show a table scan plus sort for the small instrument list. The saved cache-on
artifact marks the five cached cases as `HIT`; the cache-off artifact marks
them `MISS`. The cache implementation has a stampede lock and treats backend
errors as misses. These are local-memory-cache observations; Redis latency,
cross-process stampede behavior, and cache memory are **unverified**.

The model has a unique composite constraint on
`(instrument, trading_date)` and a separate `trading_date` index for
cross-sectional queries. The analytics snapshot service uses
`select_related("instrument")`. These are code-level facts, not scale results.

## Repeatable procedure when services are available

1. Start MySQL and Redis, verify the schema with `python manage.py migrate`,
   and record `SELECT VERSION()`, `SHOW VARIABLES LIKE 'max_connections'`,
   and `SHOW VARIABLES LIKE 'innodb_buffer_pool_size'`.
2. Run the harness for each dataset size with a new label and fixed seed. For
   example:

   ```powershell
   .\venv\Scripts\python.exe scripts\bench_api.py scale-1000 --instruments 20 --trading-days 50 --repeat 30 --reset
   .\venv\Scripts\python.exe scripts\bench_api.py scale-100000 --instruments 200 --trading-days 500 --repeat 30 --reset
   .\venv\Scripts\python.exe scripts\bench_api.py scale-1000000 --instruments 2000 --trading-days 500 --repeat 30 --reset
   ```

   The labels are illustrative; the actual row count is instruments ×
   trading days and must be recorded from the JSON output. Do not claim the
   nominal size unless the output confirms it.
3. For HTTP concurrency, run the Django app under a repeatable server command
   and use Locust or k6 with a fixed request mix. Start with 1, then 5, 10,
   20, and 40 virtual users; use a bounded ramp-up, a warm-up period, and a
   fixed measurement window. Capture client latency/error metrics plus MySQL
   processlist, connection count, CPU, RSS, and slow-query output.
4. For each cached endpoint, run cold-cache, warm-cache, and synchronized
   concurrent-miss phases. Compare query counts and backend calls; do not
   infer Redis behavior from LocMemCache.
5. Generate a large CSV from the project’s deterministic generator where its
   schema matches the ingestion contract. Record SHA-256, rows, bytes, and
   generation command. Submit the same file synchronously and through the
   Celery endpoint. Test at least two chunk sizes, keeping all other variables
   fixed.
6. Run one worker at a time, then a fixed higher concurrency. Submit a fixed
   queue of identical jobs while issuing the same API request mix. Record job
   state transitions, checkpoint offsets, queue depth, commit duration, and
   API p95/p99. Inspect `SHOW ENGINE INNODB STATUS` for deadlocks and long
   transactions.

## Load-testing tool and data generation

Locust is a suitable choice because the workload can be expressed as named
Python tasks, authenticated clients can be modeled, and concurrency/ramp
profiles are reproducible in source control. k6 is equally suitable for a
mostly HTTP workload. The load test must use seeded IDs and fixed request
weights, save its options/configuration, and emit JSON or CSV results. It
must not generate random data during the measurement window.

Use `scripts/bench_api.py` for read datasets because its seed and generated
shape are explicit. For ingestion, generate a CSV once, save its hash, then
reuse that exact file for every A/B run. Never generate a million-row file in
memory just to test chunking; stream rows to disk and verify the final row
count/hash before submission.

## Bottleneck review and limitations

Likely bottlenecks to verify, not assume: index selectivity and sort cost in
`EXPLAIN`; response serialization and page size; accidental ORM relation
loads; Pandas materialization; Redis miss amplification; worker DB write
contention; MySQL connection exhaustion; and transaction/checkpoint duration.

The current evidence cannot answer throughput, error rate, RSS, queue backlog,
Redis concurrent misses, MySQL connection limits under load, or one-million-row
behavior. Local in-process benchmark results also exclude network hops,
multiple web workers, realistic TLS/auth proxies, production data skew, and
production cache/DB topology. Therefore this investigation makes no
production-scale readiness claim.
