# Public API (`/stocks/`, `/jobs/`, `/me/`)

A flat, resource-oriented surface over the same services the domain endpoints
already use. It lives in `apps/api/` and adds **no models**: every view resolves
the request, calls one service, and serializes the result. The domain routes
(`/instruments/`, `/market-data/`, `/ingestion/`, `/analytics/`, `/auth/`) are
unchanged and remain the internal surface.

| URL | What it is |
|---|---|
| `/api/v1/stocks/` | stocks = instruments |
| `/api/v1/jobs/` | jobs = ingestion jobs |
| `/api/v1/me/` | the caller's own account |

Everything requires `Authorization: Bearer <access>` except the auth endpoints
listed under "Existing routes".

## The final routes

Facade (new, `apps/api/urls.py`, `app_name = "api"`):

| # | Method | Endpoint | Access | Purpose |
|---|---|---|---|---|
| 1 | `GET` | `/api/v1/stocks/` | authenticated | list stocks, filter and search |
| 2 | `GET` | `/api/v1/stocks/{symbol}/` | authenticated | retrieve one stock by symbol |
| 3 | `GET` | `/api/v1/stocks/{symbol}/history/` | authenticated | price history, optional date range |
| 4 | `GET` | `/api/v1/stocks/{symbol}/analytics/` | authenticated | stored metric snapshots |
| 5 | `GET` | `/api/v1/stocks/{symbol}/quality/` | authenticated | price-coverage review |
| 6 | `POST` | `/api/v1/jobs/etl/` | authenticated | queue an ETL load, return the job id |
| 7 | `GET` | `/api/v1/jobs/{job_id}/` | authenticated, **owner** | job status and progress |
| 8 | `GET` | `/api/v1/jobs/{job_id}/quality/` | authenticated, **owner** | data-quality report |
| 9 | `GET` | `/api/v1/me/` | authenticated | the caller's profile |
| 10 | `GET` | `/api/v1/me/analytics/` | authenticated | the caller's activity summary |

Existing routes, kept as-is and reused by the facade:

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/auth/register/` | anonymous, 5/hr | create an account |
| `POST` | `/api/v1/auth/login/` | anonymous, 10/min | exchange credentials for tokens |
| `POST` | `/api/v1/auth/logout/` | authenticated | blacklist the refresh token |
| `POST` | `/api/v1/auth/token/refresh/` | anonymous | rotate a refresh token |
| `GET` | `/api/v1/auth/me/` | authenticated | current user profile |
| `GET` | `/api/v1/auth/usage/` | authenticated | activity statistics |
| `GET` | `/api/v1/instruments/` | authenticated | list instruments |
| `GET` | `/api/v1/instruments/{id}/` | authenticated | one instrument |
| `GET` | `/api/v1/instruments/exchanges/` | authenticated | exchanges (unpaginated) |
| `GET` | `/api/v1/market-data/instruments/{id}/prices/` | authenticated | price history |
| `POST` | `/api/v1/ingestion/` | authenticated | create a job |
| `POST` | `/api/v1/ingestion/upload/` | authenticated | upload and load **synchronously** |
| `POST` | `/api/v1/ingestion/submit/` | authenticated | upload and **queue** the load |
| `GET` | `/api/v1/ingestion/` | authenticated | the caller's jobs |
| `GET` | `/api/v1/ingestion/{uuid}/` | authenticated, owner | job status |
| `POST` | `/api/v1/ingestion/{uuid}/cancel/` | authenticated, owner | request cancellation |
| `GET` | `/api/v1/ingestion/{uuid}/report/` | authenticated, owner | data-quality report |
| `GET` | `/api/v1/ingestion/{uuid}/validation-errors/` | authenticated, owner | rejected rows |
| `GET` | `/api/v1/ingestion/{uuid}/rule-summaries/` | authenticated, owner | rule-level PASS/FAIL |
| `GET` | `/api/v1/analytics/instruments/{id}/metrics/` | authenticated | stored metric snapshots |
| `GET` | `/api/v1/analytics/instruments/{id}/series/` | authenticated | one metric computed on demand |
| `GET` | `/api/v1/analytics/rankings/` | authenticated | instruments ranked by one measure |

**Where the facade and the domain surface differ.** The facade keys stocks by
`{symbol}` (case-insensitive) rather than by integer id, and returns the *stored*
snapshots at `.../analytics/` — the on-demand engine stays at
`/analytics/instruments/{id}/series/` and `/rankings/`. `.../quality/` is new: it
has no domain twin, and reads the trading calendar (`apps.analytics.series`) plus
the price table.

## Query parameters

The facade follows the project's conventions: unknown parameters are ignored
(with the `metric_type` exception below), `page_size` is capped at 100, and an
out-of-range `page` is a `404`.

### `GET /api/v1/stocks/`

| Parameter | Match | Accepted values |
|---|---|---|
| `symbol` | exact, case-insensitive | any ticker |
| `exchange` | exact, case-insensitive | exchange `code` |
| `asset_type` | exact, case-sensitive | `equity`, `etf`, `crypto`, `forex`, `index`, `future` |
| `is_active` | boolean | `true` / `false` (any other value is ignored) |
| `search` | substring of symbol or name | any text |
| `ordering` | sort | `symbol`, `name`, `asset_type`, `created_at` (`-` to reverse) |
| `page`, `page_size` | pagination | `page_size` ≤ 100 |

Default order is `symbol` ascending.

### `GET /api/v1/stocks/{symbol}/history/`

| Parameter | Match | Accepted values |
|---|---|---|
| `start_date` | inclusive lower bound | ISO date |
| `end_date` | inclusive upper bound | ISO date |
| `ordering` | sort | `trading_date`, `close`, `volume` (`-` to reverse) |
| `page`, `page_size` | pagination | `page_size` ≤ 100 |

A malformed date or an inverted range (`start_date` after `end_date`) is a `400`
under `date_range`.

### `GET /api/v1/stocks/{symbol}/analytics/`

| Parameter | Match | Accepted values |
|---|---|---|
| `metric_type` | exact | `return`, `moving_average`, `volatility`, `drawdown` |
| `start_date` / `end_date` | inclusive bounds | ISO date |
| `ordering` | sort | `as_of_date`, `metric_type`, `value` (`-` to reverse) |
| `page`, `page_size` | pagination | `page_size` ≤ 100 |

Unlike the rest of the facade, an **unknown `metric_type` is a `400`** rather
than a silently empty page: a typo asking for a metric that does not exist should
not look like "no snapshots". Default order is `as_of_date` descending.

### `GET /api/v1/stocks/{symbol}/quality/`

| Parameter | Match | Accepted values |
|---|---|---|
| `start_date` | inclusive lower bound | ISO date; defaults to the first price |
| `end_date` | inclusive upper bound | ISO date; defaults to the last price |

## Examples

### List stocks — `GET /api/v1/stocks/?asset_type=equity&ordering=-symbol&page_size=10`

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    { "id": 2, "symbol": "MSFT", "name": "Microsoft Corporation",
      "asset_type": "equity", "exchange_code": "NASDAQ", "currency": "USD",
      "is_active": true }
  ]
}
```

### Retrieve one stock — `GET /api/v1/stocks/aapl/`

```json
{
  "id": 1, "symbol": "AAPL", "name": "Apple Inc.", "asset_type": "equity",
  "exchange": { "id": 1, "code": "NASDAQ", "name": "Nasdaq",
                "timezone": "America/New_York" },
  "currency": "USD", "is_active": true,
  "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"
}
```

### Price history — `GET /api/v1/stocks/AAPL/history/?start_date=2024-03-12&end_date=2024-03-14`

```json
{
  "count": 3,
  "results": [
    { "trading_date": "2024-03-12", "open": "102.00000000", "high": "107.00000000",
      "low": "97.00000000", "close": "102.00000000", "adjusted_close": null,
      "volume": 1000, "source": "test" }
  ]
}
```

### Stored analytics — `GET /api/v1/stocks/AAPL/analytics/?metric_type=return`

```json
{
  "count": 1,
  "results": [
    { "symbol": "AAPL", "metric_type": "return", "as_of_date": "2024-03-15",
      "lookback_days": 30, "value": "0.08000000", "metadata": { "method": "simple" },
      "calculated_at": "2026-01-01T00:00:00Z" }
  ]
}
```

### Data quality — `GET /api/v1/stocks/AAPL/quality/`

```json
{
  "symbol": "AAPL",
  "start_date": "2024-03-11",
  "end_date": "2024-03-13",
  "observations": 2,
  "sessions": 3,
  "missing_sessions": 1,
  "coverage": "0.66666667",
  "first_date": "2024-03-11",
  "last_date": "2024-03-13",
  "missing_dates": ["2024-03-12"]
}
```

`coverage` is `null` when the calendar has no sessions for the window — an
honest "unknown" rather than a fabricated `1.0`. An instrument with no prices
reports zeros and null dates: "no data" is a quality finding, not a `404`.

### Queue an ETL load — `POST /api/v1/jobs/etl/` (multipart)

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/jobs/etl/ `
  -H "Authorization: Bearer $ACCESS" `
  -H "Idempotency-Key: 2026-10-18-daily-prices" `
  -F "file=@sample_data\synthetic\batches\prices_batch_004.csv"
```

`202 Accepted` for a new job; `200 OK` when the same bytes (or the same
`Idempotency-Key`) were already submitted by this user.

```json
{
  "job": {
    "id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
    "source_type": "csv", "status": "pending",
    "requested_at": "2026-10-18T03:57:22Z",
    "rows_received": 0, "rows_accepted": 0, "rows_rejected": 0,
    "created_by_email": "analyst@example.com"
  },
  "created": true
}
```

### Job status — `GET /api/v1/jobs/{job_id}/`

```json
{
  "id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
  "source_type": "csv", "status": "processing",
  "requested_at": "2026-10-18T03:57:22Z", "started_at": "2026-10-18T03:57:23Z",
  "completed_at": null, "rows_received": 100, "rows_processed": 40,
  "rows_accepted": 38, "rows_rejected": 2, "progress_percent": 40,
  "attempts": 1, "heartbeat_at": "2026-10-18T03:57:25Z",
  "celery_task_id": "b1f...", "error_summary": "", "created_by_email": "analyst@example.com"
}
```

### Job quality — `GET /api/v1/jobs/{job_id}/quality/`

```json
{
  "job_id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
  "source_type": "csv", "status": "completed_with_errors",
  "rows_received": 15, "rows_accepted": 2, "rows_rejected": 13,
  "error_summary": "received=15; accepted=2; rejected=13; failures: non_positive_price=2",
  "started_at": "2026-10-18T03:57:23Z", "completed_at": "2026-10-18T03:57:25Z",
  "rules": [
    { "rule": "non_positive_price", "passed": 13, "failed": 2, "status": "fail" }
  ],
  "sample_failures": [
    { "row_number": 5, "rule": "non_positive_price", "field": "open",
      "detail": "open=0.00 must be greater than zero" }
  ]
}
```

### Current user — `GET /api/v1/me/`

```json
{ "id": 1, "email": "analyst@example.com", "first_name": "", "last_name": "",
  "roles": ["analyst"] }
```

### My analytics — `GET /api/v1/me/analytics/`

```json
{
  "jobs": {
    "total": 4, "by_status": { "pending": 3, "completed": 1 },
    "rows_received": 160, "rows_accepted": 140, "rows_rejected": 20
  },
  "prices": {
    "records_imported": 137, "instruments_covered": 3,
    "earliest_date": "2024-03-11", "latest_date": "2024-03-15"
  }
}
```

## Status codes, ownership, and the error envelope

| Code | When |
|---|---|
| `200` | read succeeded, or a replay of an idempotent `jobs/etl/` submit |
| `201` | (domain only) job created by `POST /ingestion/` |
| `202` | job accepted for asynchronous processing (`jobs/etl/`) |
| `400` | invalid filter/choice, malformed date, inverted range, bad or missing file |
| `401` | missing or invalid token |
| `403` | authenticated, but the job belongs to someone else |
| `404` | the symbol, instrument, or job does not exist |
| `429` | rate limited (`register`, `login`) |

Every body — success or failure — is JSON, and every error uses one envelope:

```json
{ "error": { "code": "bad_request", "message": "Request rejected.",
             "fields": { "date_range": ["'11/03/2024' is not a valid ISO date."] } } }
```

`fields` carries the offending parameter name — `date_range` for a bad or
inverted date range, `metric_type` for an unknown metric, `file` for a rejected
upload — and is empty when the failure has no single field.

**Ownership.** Jobs are private. The list endpoints scope the queryset to
`request.user` *before* filtering; the detail and report endpoints look the job
up across everyone's rows and then run `check_object_permissions`, so another
user's job is a `403` (it exists) rather than a `404` (a lie), and an anonymous
request is a `401` rather than a `403`. Tests assert all three.

## Request lifecycle: middleware to ORM to response

For `GET /api/v1/stocks/AAPL/history/?start_date=2024-03-12`:

1. **Server → middleware.** WSGI/ASGI hands the request to Django's middleware
   chain: `SecurityMiddleware`, `SessionMiddleware`, `CommonMiddleware`,
   `CsrfViewMiddleware`, `AuthenticationMiddleware`, `MessageMiddleware`,
   `XFrameMiddleware`. The facade is JWT-only, so `CsrfViewMiddleware` is inert
   here (it exists for `/admin/`).
2. **URL resolver.** `config/urls.py` matches `api/v1/` against
   `apps.api.urls`, whose `stocks/<str:symbol>/history/` pattern binds
   `symbol="AAPL"`.
3. **DRF `dispatch`.** Parsers and content negotiation run first, then
   `JWTAuthentication` decodes the bearer token into `request.user`, then the
   permission classes run — the global `IsAuthenticated`. A failure here is a
   `401`/`403` before any view code executes.
4. **View.** `StockHistoryAPIView.get_queryset` calls `parse_date_range` (which
   delegates to `apps.marketdata.services`), then `StockScopedMixin.get_stock`
   resolves the symbol with `get_object_or_404(... symbol__iexact="AAPL")`, and
   finally `price_services.get_prices_for_instrument(...)` builds the queryset.
   The view itself contains no query construction.
5. **ORM.** The service filters `PriceRecord` by instrument and the inclusive
   date bounds and orders by `trading_date`; `OrderingFilter` may replace the
   order. One indexed range scan; `select_related` is deliberately absent here
   because the serializer renders no symbol.
6. **Pagination + serialization.** `StandardResultsPagination` runs a `COUNT`
   and slices the page; `PriceRecordSerializer` renders the fields.
7. **Response.** DRF renders `{"count", "next", "previous", "results"}` to JSON.
   If anything raised, `market_data_exception_handler` rewrites it into the
   `{"error": {...}}` envelope first.

## Synchronous vs asynchronous

The rule is: **reads are synchronous, and the one long-running write is not.**

- **Reads** (`stocks`, `history`, `analytics`, `quality`, `me`, and the job
  status/report reads) stay synchronous. Each is bounded by a date range, an
  offset page, or a single job, and is served by an index lookup or an
  aggregate — a request that finishes in milliseconds should not pay for a queue.
- **`POST /api/v1/jobs/etl/` is asynchronous.** An ETL load parses and validates
  a whole file, and can take far longer than any sensible HTTP timeout. Blocking
  the request would hold a worker thread and a database connection for the
  duration, and a client that gave up would leave the server still working on
  work nobody is waiting for. So the endpoint **creates a durable job row and
  returns its identifier** (`202`) rather than the loaded rows: the file is
  stored, the job is committed, and only *then* is the message handed to the
  worker. The database, not the broker, is the source of truth — a Redis outage
  leaves a `pending` job that can be requeued, it does not lose the submission.
  The client polls `GET /api/v1/jobs/{job_id}/` for progress and reads
  `GET /api/v1/jobs/{job_id}/quality/` once the job is terminal.
- **The synchronous alternative still exists** at
  `POST /api/v1/ingestion/upload/`, for small files where a caller wants the
  report in the same round trip.
- **API sources are never queued.** Their fetcher is a Python callable, which
  cannot travel through a queue, and accepting a client-supplied URL would be an
  SSRF hole — so `api` ingestion stays on the synchronous path.

Re-submitting the same bytes is safe: `Idempotency-Key` (or the file's SHA-256)
collapses the retry onto the original job and returns `200` instead of `202`.

## Tests

`apps/api/tests/` covers each group:

- `test_me.py`, `test_stocks.py`, `test_stock_activity.py`,
  `test_stock_quality.py`, `test_jobs.py`.

Every group asserts valid requests, invalid inputs (`400`), anonymous access
(`401`), nonexistent records (`404`), and — for jobs — cross-user access (`403`).
Two tests pin query counts (`assertNumQueries`) so an accidental N+1 or a dropped
`select_related` fails the build rather than a benchmark.

```powershell
python -m pytest apps/api -q     # the facade
python -m pytest -q              # the whole suite
```
