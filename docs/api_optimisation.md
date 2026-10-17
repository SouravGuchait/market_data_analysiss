# Optimising the read APIs: measured ORM work and a read-through cache

This is the record of one pass over the API's read surface. It is written so that
every claim in it can be re-derived: the harness, the dataset, the plans, and the
raw numbers are all in the repository.

The short version:

* **One join was removed** from the price history list, because the serializer
  renders no instrument columns. Measured at the query: **1.475 ms → 0.833 ms**
  per 25-row page, and the statement shrank from **1271 → 592 characters**, on an
  identical access path.
* **One index was added**, on `IngestionJob (created_by, -requested_at)`. The job
  list's plan lost a `Sort row IDs: requested_at DESC` node. Latency at 8 jobs is
  noise; the point is that the ordering became part of the access path instead of
  a sort of everything the owner has ever submitted.
* **A read-through cache** was added for the two endpoints that were genuinely
  expensive or genuinely hot: the usage summary and the analytics snapshot list.
  The usage summary went from **229.73 ms → 0.29 ms** and from **3 queries → 0**.
* **Nothing else was touched**, and section 6 says why for each endpoint that a
  reader might expect to see changed.

Baseline and both "after" runs are in [`docs/benchmarks/`](benchmarks/):
`api_baseline.json`, `api_after_no_cache.json`, `api_after.json`.

---

## 1. Reproducing

```powershell
# 0. the index migration (already applied on the dev database)
./venv/Scripts/python.exe manage.py migrate

# 1. the baseline was recorded against the unmodified code
./venv/Scripts/python.exe scripts/bench_api.py baseline

# 2. the ORM changes, with cached reads switched off
./venv/Scripts/python.exe scripts/bench_api.py after_no_cache --no-cache

# 3. everything, cached reads on
./venv/Scripts/python.exe scripts/bench_api.py after

# remove the benchmark rows again
./venv/Scripts/python.exe scripts/bench_api.py after --reset
```

Each run writes `docs/benchmarks/api_<label>.json`: per case, the query count on
the first request and on the immediate repeat, the `X-Cache` header, median and
p95 latency over 15 timed requests, the response size, the captured SQL, and its
`EXPLAIN` (tree format, so the chosen index and row estimate are recorded rather
than inferred). Every statement issued by a case is kept, not just the one the
report prints.

The harness seeds its own dataset and reuses it across runs, so the three runs
above measure the same 250,000 rows.

## 2. Environment

Measured on the development machine, MySQL 26.7.0 (`SELECT VERSION()`, MySQL
Community Server) with a 128 MiB InnoDB buffer pool, Python 3.12.0, Django 5.2.18,
DRF, single process, no Redis running.

Cached reads in run 3 therefore used Django's in-process `LocMemCache`. The cache
layer speaks to Redis through the same `django.core.cache` API and the same code
path — `CACHES["default"]` only changes the backend — but the Redis case is not
claimed here as measured; what *is* tested is the behaviour when the backend
fails (section 5.3.5).

## 3. The dataset

`python scripts/bench_api.py <label>` builds, once and idempotently:

| | |
|---|---|
| Exchange | `BENCH` |
| Instruments | 100 (`BENCH0000`…`BENCH0099`, a few inactive so the filter has work) |
| Trading days | 500 weekdays |
| Prices | 50,000 (one row per instrument-day) |
| Snapshots | 200,000 (100 instruments × 8 metric/lookback combinations × 250 days) |
| Jobs | 8, owned by `bench-owner@example.test` |
| Seed | `20261017` |

Rows are generated from the seed alone — each instrument's walk is seeded by its
symbol — so the numbers are reproducible and the "reused" path is genuinely the
same data.

## 4. Baseline

Recorded before any change, so no cache existed and `cold` = `warm` for every
case. Seventeen cases, 41 queries per full pass.

| Case | Queries | Median | p95 | Bytes |
|---|---|---|---|---|
| `instruments.list.p1` | 2 | 2.16 ms | 2.72 ms | 3,665 |
| `instruments.list.p3` | 2 | 2.02 ms | 2.67 ms | 3,767 |
| `instruments.list.filter` | 2 | 2.36 ms | 2.85 ms | 3,720 |
| `instruments.list.search` | 2 | 2.48 ms | 3.01 ms | 3,742 |
| `instruments.list.order` | 2 | 2.77 ms | 3.98 ms | 3,745 |
| `instruments.detail` | 1 | 1.10 ms | 1.47 ms | 288 |
| `exchanges.list` | 1 | 0.79 ms | 0.91 ms | 556 |
| `prices.p1` | 3 | 2.90 ms | 3.71 ms | 4,686 |
| `prices.p20` | 3 | 3.54 ms | 4.29 ms | 4,687 |
| `prices.range` | 3 | 3.14 ms | 4.47 ms | 4,729 |
| `prices.order_close` | 3 | 3.70 ms | 5.21 ms | 4,701 |
| `analytics.p1` | 3 | 3.42 ms | 31.34 ms | 5,361 |
| `analytics.metric` | 3 | 2.87 ms | 3.49 ms | 5,285 |
| `analytics.range` | 3 | 3.10 ms | 3.77 ms | 5,327 |
| `analytics.order_value` | 3 | 9.45 ms | 15.98 ms | 5,483 |
| `auth.usage` | 3 | **256.89 ms** | 275.39 ms | 226 |
| `jobs.list` | 2 | 1.99 ms | 3.34 ms | 3,147 |

(p95 here is the 19th 20-quantile cut of 15 samples — effectively near the
maximum, which is why `analytics.p1`'s single 31 ms outlier stands out. The medians
are what the rest of this document reasons about.)

### 4.1 The three findings that decided the work

**The usage summary is the whole problem.** 256.89 ms for 226 bytes, against
0.8–9.5 ms for everything else. `EXPLAIN` shows why: a nested loop from the owner's 8
jobs into `marketdata_pricerecord.ingestion_job_id` (~56,760 estimated rows) and an
aggregate over all of them. It is inherently O(rows this user owns) — there is no
index that makes counting 6,250 rows per job cheaper than counting them. Caching
is the only lever, and it is the one case where a cache is clearly the right
answer rather than a guess.

**The analytics list is hot and stable.** 200,000 snapshots, 2,000 per instrument,
and `EXPLAIN` already uses `uniq_snapshot_grain` in reverse with no filesort
(`cost=700 rows=2000`). The rows are append-only reference data, identical for
every authenticated user. Fast to read, rarely written, read far more often than
written: the textbook cache candidate.

**Everything else is already fine.** Instruments, exchanges, and price history are
all under 4 ms with no filesort except where the client explicitly asks for one
(`ordering=-close`). Three of these have query-count assertions in the existing
suite, which is the point at which caching stops being a performance change and
starts being a change to what the tests mean.

## 5. Changes

### 5.1 Price history: an unjustified join, removed

`get_price_queryset()` joined `instrument` and `instrument__exchange` for every
price read. `PriceRecordSerializer` renders OHLC/volume/source — no symbol, no
exchange code. The join shipped two tables' columns that nothing read.

`get_prices_for_instrument()` now uses a lean sibling, `get_price_list_queryset()`,
and `get_price_queryset()` is kept for callers that do render the symbol (its
query count is pinned by an existing test).

Measured in the same process, alternating the two forms so neither gets a warmer
connection:

| | Per 25-row page | Statement |
|---|---|---|
| with the joins | 1.475 ms | 1271 chars |
| without them | **0.833 ms** | **592 chars** |
| plan | `Limit: 25 (cost=175 rows=25) → Index lookup on marketdata_pricerecord using uniq_price_instrument_trading_date (instrument_id = 13) (cost=175 rows=500)` — **identical both ways** |

The plan is unchanged because the filter already names the instrument, so the same
unique index is chosen. The saving is the join itself: two fewer table reads per
row, and less than half the SQL to parse and send. The A/B is recorded in both
`after` runs under `price_query_ab` (the baseline predates that addition to the
harness), so the claim does not depend on this table.

`select_related` remains where it is load-bearing: `Instrument.__str__` reads
`exchange`, so the instrument list and detail querysets keep `instrument__exchange`
and `select_related("exchange")` respectively.

### 5.2 Ingestion jobs: an index that removes a sort

The job list is "this owner's jobs, newest first". The baseline plan was:

```
-> Limit: 8 row(s)  (cost=1.35 rows=8)
    -> Sort row IDs: ingestion_ingestionjob.requested_at DESC, limit input to 8 row(s) per chunk
        -> Index lookup on ingestion_ingestionjob using uniq_job_owner_idempotency_key (created_by_id = 14)
```

The owner filter was indexed; the ordering was not, so MySQL read the owner's jobs
and sorted them. The plan after adding `idx_job_owner_requested` on
`(created_by, -requested_at)`:

```
-> Limit: 8 row(s)  (cost=1.35 rows=8)
    -> Index lookup on ingestion_ingestionjob using idx_job_owner_requested (created_by_id = 14)
```

The sort node is gone. Latency is not claimed: at 8 rows a filesort is free, and the
after-runs measured 3.87 ms and 4.02 ms against the baseline's 1.99 ms — while
untouched endpoints drifted upward by the same factor (`instruments.list.p1` went
2.16 → 3.67 ms with no change at all; see §7). What the index buys is that the cost
stops growing with the number of jobs the owner has ever submitted, which is exactly
the shape of the query.

Migration: `apps/ingestion/migrations/0004_ingestionjob_idx_job_owner_requested.py`.

### 5.3 A read-through cache for the two endpoints that need one

`apps/common/cache.py` is the whole cache layer: versioned keys, JSON payloads,
a stampede lock, and a deliberate policy that a cache fault is a miss and never an
error. `apps/common/views.py` adds the one mixin both cached endpoints use.

The cache surface is deliberately small: **the usage summary** (per user) and
**the analytics snapshot list**. Both are read far more often than they change and
both have a real invalidation hook. Nothing else is cached — see section 6.

#### 5.3.1 Keys

Keys are readable, because a key you can read in `redis-cli` is a key you can
reason about before deleting it, and because the requirement that a key name its
instrument, date range, metric configuration, and access scope is then verifiable
by eye rather than by argument:

```
market_data:1:analytics:v1:role:analyst:host=testserver:instrument=13:\
metric_type=return:lookback_days=90:from=2024-01-01:to=2024-03-14:page=1
market_data:1:usage:v1:user:14
```

(`market_data:1:` is Django's own `KEY_PREFIX` and cache version.) Building blocks:

* **namespace + version** — the version is what invalidation changes.
* **scope** — `user:<id>` for the usage summary, because that response *is* the
  caller's data and must never be shared; `role:<role>` for the analytics list,
  where every role currently sees the same rows, recorded anyway so that narrowing
  the filter by role later cannot start serving one role a page cached for another.
* **every request dimension** — instrument, metric type, lookback, date range,
  ordering, page, page size. The view parses these once, into one dict, and builds
  *both* the queryset and the key from it, so a filter that is missing from the key
  is a filter that is missing from the query: they cannot drift apart.
* **host** — `next`/`previous` are absolute URLs, so a payload cached behind one
  host must not be served as another's links.

Absent values are omitted rather than rendered as `None`, because "no metric
filter" and "an empty metric filter" are one question and two keys would halve the
hit rate for nothing. Anything past 200 characters collapses to a digest of the
same string, so the readable form cannot become a payload in its own right.

#### 5.3.2 TTL and invalidation

Every entry carries a TTL (`API_CACHE_USAGE_TTL` 60s, `API_CACHE_ANALYTICS_TTL`
300s) as a backstop, but freshness is maintained by **invalidation**, not by
expiry.

Invalidation is a **version bump**, not a delete. Keys embed a per-namespace
counter; one `INCR` orphans every entry in that namespace in O(1). There is
deliberately no `delete_pattern`: it needs `SCAN`, it walks the entire keyspace,
and on a shared Redis that is a documented way to stall the server. Orphaned
entries are simply never read again and fall out on TTL.

The counter has no TTL, so it should outlive everything — but if Redis ever evicts
it under an `allkeys-lru` policy, "the counter is gone" must not mean "start
counting again", because restarting at 2 would silently re-adopt every live entry
written under version 2. The fallback is a random 122-bit value plus a
process-local record of the last one issued, which makes the guarantee exact
rather than very likely.

Hooks, each at the point where the write actually commits:

| Write | Hook |
|---|---|
| Price rows through the pipeline (`bulk_create`) | `upsert_prices` calls `invalidate_after_commit("usage")` — `bulk_create` emits **no signals**, so this cannot be a receiver |
| A price row written row-at-a-time (admin, shell, command) | `post_save`/`post_delete` receivers in `apps/marketdata/signals.py` |
| A snapshot written or deleted | `post_save`/`post_delete` receivers in `apps/analytics/signals.py` — snapshots have no writer of their own yet, which is why this is a signal: whoever adds one gets invalidation for free |
| A job changing state | `apps/ingestion/signals.py`, which **skips progress-only writes** |

All four go through `transaction.on_commit`, so a write that rolls back does not
throw away a good entry and force a recompute that returns the same answer.

The job receiver is the interesting one. A running job writes its row on every
heartbeat and after every chunk, and none of those writes change anything the
usage summary reads (it groups by `status` and sums `rows_received`). Invalidating
on each would evict the entry several times a minute and make the cache pointless
during exactly the load it exists to survive. So saves confined to
`{checkpoint, rows_processed, rows_accepted, rows_rejected, heartbeat_at}` are
ignored, and the two saves that matter — the claim, which sets `status`, and the
final save, which sets `status` and the totals — invalidate.

#### 5.3.3 A cold key is computed once

Concurrent misses on the same key take a short lock (`cache.add`, which is atomic
on both LocMem and Redis): one request computes, the others wait briefly and
re-read that answer. The wait is bounded (`API_CACHE_STAMPEDE_WAIT_SECONDS`, 0.5s)
and the losers then compute for themselves. The lock is an optimisation and never
a correctness dependency: nothing is ever read *from* the lock, only the entry, and
only after it has been written. A lock that is held costs a duplicated query; it
can never deadlock a request.

#### 5.3.4 A hit is indistinguishable from a miss

The rendered representation is what is stored, so a cached body and a fresh body
are byte-for-byte equal (a test asserts this with `response.content`).

That is not automatic. An early version cached the service's dict, and
`response.data` then carried `date` objects on a miss and ISO strings on a hit:
invisible to a client, poisonous in a test, where the same assertion would pass or
fail depending on whether the cache was warm. The layer now normalises the fresh
result through the same encode/decode it stores with
(`_as_it_will_be_read_back`), so the two paths agree by construction rather than by
luck.

#### 5.3.5 A cache fault is a miss, never an error

Every backend call is wrapped, and any failure — Redis unreachable, a socket
timeout, an undecodable value — is reported to the caller as "not cached", so the
request recomputes from MySQL. The API's availability does not depend on the
cache's.

Two details matter more than the wrapping:

* **An unreachable cache is not waited out.** `_get` returns a three-valued
  "found" flag, so the stampede path can tell "the key is absent" from "the cache
  cannot answer at all" and stop waiting in the second case. A Redis outage costs a
  query, never a timeout.
* **Failures are logged at most once a minute.** A Redis outage that floods the log
  is a second outage.

Responses carry `X-Cache: HIT|MISS`, so the behaviour is observable on the wire —
in a test, in `curl`, or in a production log — without attaching a profiler.

#### 5.3.6 Configuration

| Setting | Default | Purpose |
|---|---|---|
| `REDIS_CACHE_URL` | *(empty)* | Set it (e.g. `redis://127.0.0.1:6379/1`) to use Redis; empty falls back to `LocMemCache` |
| `CACHE_KEY_PREFIX` | `market_data` | Namespaces the keyspace |
| `REDIS_CACHE_TIMEOUT` | 1 | Connect/socket timeout, in seconds |
| `API_CACHE_ENABLED` | `True` | Kill switch; cached reads off, API unchanged |
| `API_CACHE_USAGE_TTL` | 60 | TTL backstop for the usage summary |
| `API_CACHE_ANALYTICS_TTL` | 300 | TTL backstop for metrics pages |
| `API_CACHE_LOCK_SECONDS` | 5 | How long a miss-holder may keep the compute lock |
| `API_CACHE_STAMPEDE_WAIT_SECONDS` | 0.5 | How long a loser waits before computing for itself |

Redis is **opt-in** rather than the default. The Celery broker already owns
logical database 0, so the cache belongs on a different one; and a laptop without
Redis gets a working in-process cache instead of a connect timeout on every cached
request.

Under the test suite the backend is forced to LocMem and `API_CACHE_ENABLED` to
`False`, with the cache tests opting in via `override_settings`. That is not just
convenience: Django does not clear caches between tests, `TestCase` rolls the
database back while primary keys restart, and several existing tests assert exact
query counts that a stray hit would invalidate. Making the cache explicit in the
tests that exercise it keeps both honest.

### 5.4 Analytics: filter the metric configuration in SQL

The service already supported `lookback_days`, and the endpoint did not expose it.
A 30-day and a 90-day moving average share a name but are different series, so
`?lookback_days=` is now a validated query parameter — one filter in SQL instead of
a client-side discard, and one fewer way for the "metric configuration" dimension
of the cache key to be missing. Non-integers and negative values are rejected with
the API's standard error envelope.

## 6. What was deliberately left alone

Each of these was considered against the baseline and rejected, with the evidence
that rejected it:

* **No analytics index.** The plan already uses `uniq_snapshot_grain` in reverse
  with no filesort, so an additional index would add write cost and take space to
  speed up a read that is already an index lookup.
* **No index on `instruments.created_at`.** `?ordering=-created_at` sorts a
  100-row table. An index would be pure write cost for a table that fits in a page.
* **No index for `?ordering=-close`.** The client asked for a sort; serving an
  arbitrary column ordering from an index means one index per column. The default
  ordering (by trading date) is already indexed and is what the endpoint is for.
* **No `select_related` removal anywhere else.** The instrument list's join is
  load-bearing: the serializer renders the exchange code, so removing it re-creates
  an N+1.
* **`?search=` stays a scan.** `icontains` cannot use an index, and at 100 rows a
  full-text index would cost more to maintain than it saves. Changing the search
  semantics to make it indexable would be a product decision, not a query
  optimisation, and is not something to slip into this change.
* **The usage summary keeps its query shape.** No index helps it: it counts the
  rows the user owns, and it must count all of them to be correct. Its 229 ms went
  behind the cache; the query itself is unchanged, and the baseline's plan (`cost`
  21642) and the after-run's (`cost` 21641) are the same plan.
* **Price history is not cached.** 500 rows per instrument, ~4 ms, and — more to
  the point — the existing suite asserts an exact query count for a price page.
  Caching it would turn "does this endpoint avoid an N+1?" into "did the cache
  happen to be warm?", which is a worse test than the one it would speed up.
* **Instrument lists are not cached.** ~2 ms, small cardinality, and the same
  query-count assertions.

## 7. Results

Three runs, same dataset, same machine, minutes apart. Absolute latency drifted
between the baseline and the later runs — the machine was busier — which is why
the cached endpoints are compared against `after_no_cache` (the same code
measured minutes earlier) and why the uncached control cases are in the table: they
all moved together by 1.5–2×, and that drift is the reason the baseline's absolute
latency is not used as the denominator for the cache's effect.

| Case | Baseline | After (cache off) | After (cache on) |
|---|---|---|---|
| `instruments.list.p1` | 2 q, 2.16 ms | 2 q, 3.89 ms | 2 q, 3.67 ms |
| `instruments.list.p3` | 2 q, 2.02 ms | 2 q, 3.92 ms | 2 q, 3.87 ms |
| `instruments.list.filter` | 2 q, 2.36 ms | 2 q, 4.24 ms | 2 q, 3.59 ms |
| `instruments.list.search` | 2 q, 2.48 ms | 2 q, 4.32 ms | 2 q, 4.57 ms |
| `instruments.list.order` | 2 q, 2.77 ms | 2 q, 4.13 ms | 2 q, 3.79 ms |
| `instruments.detail` | 1 q, 1.10 ms | 1 q, 1.92 ms | 1 q, 1.77 ms |
| `exchanges.list` | 1 q, 0.79 ms | 1 q, 1.09 ms | 1 q, 1.19 ms |
| `prices.p1` | 3 q, 2.90 ms | 3 q, 4.33 ms | 3 q, 4.45 ms |
| `prices.p20` | 3 q, 3.54 ms | 3 q, 5.32 ms | 3 q, 5.63 ms |
| `prices.range` | 3 q, 3.14 ms | 3 q, 4.46 ms | 3 q, 4.63 ms |
| `prices.order_close` | 3 q, 3.70 ms | 3 q, 5.61 ms | 3 q, 5.32 ms |
| `analytics.p1` | 3 q, 3.42 ms | 3 q, 6.12 ms | **3 q miss / 0 q hit, 0.40 ms** |
| `analytics.metric` | 3 q, 2.87 ms | 3 q, 5.62 ms | **3 q / 0 q, 0.40 ms** |
| `analytics.range` | 3 q, 3.10 ms | 3 q, 5.49 ms | **3 q / 0 q, 0.37 ms** |
| `analytics.order_value` | 3 q, 9.45 ms | 3 q, 11.85 ms | **3 q / 0 q, 0.35 ms** |
| `auth.usage` | 3 q, 256.89 ms | 3 q, 229.73 ms | **3 q / 0 q, 0.29 ms** |
| `jobs.list` | 2 q, 1.99 ms | 2 q, 4.02 ms | 2 q, 3.87 ms |
| **Total queries, full pass** | **41 cold / 41 warm** | **41 / 41** | **41 cold / 26 warm** |

Read as three separate claims:

1. **The ORM work cost nothing.** Query counts are identical before and after
   (41/41), including for the endpoints that were changed. The price page's SQL
   halved and its own cost dropped 43% (§5.1); the job list's plan lost its sort
   (§5.2); no endpoint gained a query.
2. **The cache removed the database from five cases entirely.** A hit is 0
   queries, and the pass total falls from 41 to 26 — exactly the 5 cached cases ×
   3 queries.
3. **The win is concentrated where the baseline said it would be.** The usage
   summary — 229.73 ms against 1–12 ms for every other endpoint in the same run —
   became 0.29 ms, a ~790× reduction (885× against the baseline's 256.89 ms). The
   analytics pages went from 5.5–11.9 ms to 0.35–0.40 ms.

The hit latency floors at ~0.3 ms because it is the API doing no work: response
construction and the `LocMem` lookup. A real Redis hit adds a network round trip
(~0.1–0.3 ms on a local network) to that; the point stands at either number.

One honest caveat on the counts: the harness authenticates with
`force_authenticate`, so the JWT user lookup is not in these numbers. In production
a hit still pays for authentication, which is outside this change and unchanged by
it.

## 8. Tests

486 passed, 1 skipped (a live-Redis probe; no local Redis). The baseline was 429
passed, 1 skipped, so this change adds 57 tests and breaks none.

| Area | File | Covers |
|---|---|---|
| The cache layer | `apps/common/tests/test_cache.py` | hit/miss, dimension and scope separation, JSON round trip, invalidation and version isolation, a lost version counter, readable and bounded keys, 8-thread concurrent miss computing once, a held lock never blocking, an unreachable backend (miss, no exception, no wait, `health()` false), the kill switch |
| Metrics endpoint | `apps/analytics/tests/test_metrics_api.py` | listing, ordering, pagination bound, metric/lookback/date filters, validation errors, 404, authentication |
| Metrics caching | `apps/analytics/tests/test_metrics_cache.py` | hit/miss, miss = 4 queries and hit = 1, byte-identical bodies, one entry per filter/scope/page, invalidation on snapshot write and delete, the key's contents read from the real view, cache-down and cache-off |
| Usage caching | `apps/accounts/tests/test_usage_cache.py` | hit/miss, miss = 4 queries and hit = 1, body equality, **one user's summary is never served to another**, invalidation on row-at-a-time *and* bulk price writes (via `upsert_prices`), invalidation on a job's status change, no invalidation on a heartbeat or checkpoint write, cache-down and cache-off |

Two of these tests exist because they caught real defects rather than to document
intended behaviour: the byte-for-byte body comparison caught the
`date`-vs-string discrepancy in `response.data` (§5.3.4), and the lost-counter
test caught the second-resolution fallback that a coarse system clock made
collide with itself (§5.3.2).

The existing query-count assertions are untouched and still green — including the
one pinning `get_price_queryset()` to a single joined query and the one pinning a
price page to four.

## 9. Limits

* One machine, one dataset, one MySQL instance, no Redis. The absolute numbers are
  a development machine's; the query counts and plans are the portable evidence.
* `p95` from 15 samples is noisy, and the `analytics.*` p95 values in particular
  move by an order of magnitude between runs. Medians are what the tables report.
* Concurrency is tested at the cache layer with real threads, not through the API:
  the test database is SQLite in memory, where a second thread gets a *different*
  database and cannot see the fixture. The proxy — eight simultaneous cold misses
  computing once — is the property that was at risk.
* `LocMemCache` is per process. In a multi-worker deployment it is a per-worker
  cache, which is why `REDIS_CACHE_URL` exists; the numbers above do not include
  cross-process sharing.
* The benchmark rows are still in the development MySQL database. Re-run with
  `--reset` to remove them.
