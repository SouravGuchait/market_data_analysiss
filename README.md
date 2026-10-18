# Market Data Intelligence & Analytics Pipeline

## Bootstrap

Create and activate a virtual environment, then install the project dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the local configuration file and replace the placeholders with local-only
values:

```powershell
Copy-Item .env.example .env
```

Create a MySQL database and a least-privilege local user. Run the following in a
MySQL client after replacing the angle-bracket placeholders; never put these
values in source control.

```sql
CREATE DATABASE market_data CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'market_data_user'@'127.0.0.1' IDENTIFIED BY '<local-password>';
GRANT ALL PRIVILEGES ON market_data.* TO 'market_data_user'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Apply framework migrations, verify configuration/database connectivity, start
the development server, and run the smoke test:

```powershell
python manage.py migrate
python manage.py check --database default
python manage.py runserver
```

## Running tests

```powershell
python manage.py test
python -m pytest            # both run the same suite via pytest.ini
```

1020 tests run against in-memory SQLite with a fast MD5 password hasher, so no
MySQL server is needed and the whole suite finishes in a few seconds.
Development and production stay on MySQL — only `config/settings.py` sees the
swap, gated on whether tests are running.

The asynchronous tests need no broker: the API handoff is mocked and the worker
task is exercised with `.apply()`. See
[docs/celery_async.md](docs/celery_async.md#10-testing).

## Authentication

The API authenticates with JWT bearer tokens and authorizes with a role column
on the user. Tokens are sent in the `Authorization: Bearer <access>` header,
never in a cookie, so the JSON API carries no CSRF surface; `CsrfViewMiddleware`
remains in place because `/admin/` still uses Django sessions.

```powershell
# Create a user and receive an access/refresh token pair.
$body = @{ email = "analyst@example.com"; password = "drf-portfolio-42!" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/auth/register/ -Method Post -Body $body -ContentType application/json

# Exchange credentials for tokens; the response also carries the user profile.
$tokens = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/auth/login/ -Method Post -Body $body -ContentType application/json

# Call a protected endpoint with the returned access token.
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/auth/me/ -Headers @{ Authorization = "Bearer $($tokens.access)" }
```

Login uses **email**, not username — `User.USERNAME_FIELD = "email"` and the
`username` field does not exist.

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/auth/register/` | anonymous, 5/hr | create an account, return tokens |
| `POST` | `/api/v1/auth/login/` | anonymous, 10/min | exchange credentials for tokens |
| `POST` | `/api/v1/auth/logout/` | authenticated | blacklist the refresh token |
| `POST` | `/api/v1/auth/token/refresh/` | anonymous | rotate a refresh token |
| `GET` | `/api/v1/auth/me/` | authenticated | the current user's profile |
| `GET` | `/api/v1/auth/usage/` | authenticated | the current user's activity statistics |

Every response body — success or failure — uses one envelope. Errors are shaped
as `{"error": {"code": ..., "message": ..., "fields": {...}}}`.

Passwords go through Django's `AUTH_PASSWORD_VALIDATORS` and are stored hashed.
Refresh tokens are rotated on use and blacklisted on logout, so a stolen refresh
token is rejected on its next use. Access tokens expire after 30 minutes
(`JWT_ACCESS_MINUTES`; `settings.py` defaults to 30 if unset).

Roles live on the `User.role` column, not in Django `Group`s. The values are
`analyst` and `administrator`, and an administrator is created by
`createsuperuser`. Registration **always** produces an analyst and rejects a
`role` or `roles` field outright, because otherwise anyone could self-assign
administrator. Roles are edited in the Django admin user change form.

## REST API

Every endpoint under `/api/v1/` requires `Authorization: Bearer <access>`
except `register`, `login`, and `token/refresh`. Each app owns its URL namespace
(`app_name` in `urls.py`), so routes are referenced as
`reverse("instruments:list")` rather than by hardcoded path.

A flatter public surface — `/api/v1/stocks/`, `/api/v1/jobs/`, `/api/v1/me/` —
sits on top of these same services, keyed by symbol for stocks and returning a
job id for the asynchronous ETL. It adds no models and changes none of the
routes below; see [docs/public_api.md](docs/public_api.md) for the full route
table, examples, and the synchronous-versus-asynchronous discussion.

| # | Method | Endpoint | Access | Purpose |
|---|---|---|---|---|
| 1 | `GET` | `/api/v1/instruments/` | authenticated | list instruments, filter and search |
| 2 | `GET` | `/api/v1/instruments/<id>/` | authenticated | retrieve one instrument |
| 3 | `GET` | `/api/v1/instruments/exchanges/` | authenticated | list exchanges (unpaginated) |
| 4 | `GET` | `/api/v1/market-data/instruments/<id>/prices/` | authenticated | price history, optional date range |
| 5 | `POST` | `/api/v1/ingestion/` | authenticated | create an ingestion job |
| 6 | `POST` | `/api/v1/ingestion/upload/` | authenticated | upload a file and run the load synchronously |
| 7 | `POST` | `/api/v1/ingestion/submit/` | authenticated | upload a file and **queue** the load (`202`) |
| 8 | `GET` | `/api/v1/ingestion/` | authenticated | list **your own** jobs |
| 9 | `GET` | `/api/v1/ingestion/<uuid>/` | authenticated, **owner** | job status and progress |
| 10 | `POST` | `/api/v1/ingestion/<uuid>/cancel/` | authenticated, **owner** | request cancellation |
| 11 | `GET` | `/api/v1/ingestion/<uuid>/report/` | authenticated, **owner** | data-quality report for a job |
| 12 | `GET` | `/api/v1/ingestion/<uuid>/rule-summaries/` | authenticated, **owner** | rule-level PASS/FAIL for a job |
| 13 | `GET` | `/api/v1/ingestion/<uuid>/validation-errors/` | authenticated, **owner** | rejected rows for a job |
| 14 | `GET` | `/api/v1/analytics/instruments/<id>/metrics/` | authenticated | metric snapshots for an instrument |
| 15 | `GET` | `/api/v1/analytics/instruments/<id>/series/` | authenticated | one metric computed on demand for an instrument |
| 16 | `GET` | `/api/v1/analytics/rankings/` | authenticated | instruments ranked by one measure |
| 17 | `GET` | `/api/v1/auth/usage/` | authenticated | activity statistics |
| 18 | `GET` | `/api/v1/reports/me/` | authenticated | the caller's own ingestion analytics |
| 19 | `GET` | `/api/v1/reports/platform/` | **administrator** | platform-wide ingestion analytics |
| 20 | `GET` | `/api/v1/reports/audit-events/` | **administrator** | paginated audit log |

Instruments, prices, and analytics are **read-only** through the API — those
rows are written by the ingestion pipeline, never by a client. There are two
ways to write: `POST /ingestion/upload/` records the job *and* loads the rows
behind it in one request; `POST /ingestion/submit/` records and stores the file,
then hands it to a worker and returns immediately with the job id to poll.
The asynchronous path — the queue, the worker, and the recovery commands — is
documented in [docs/celery_async.md](docs/celery_async.md).

### Query parameters

All parameters are optional and combine with `&`. An **unknown** parameter is
always ignored — with one documented exception, the two computed analytics
endpoints (`.../series/` and `/rankings/`), which reject one rather than silently
computing something the client did not ask for. An invalid *value* usually gives a
`400` — with two further exceptions noted under each table (`is_active` silently
ignoring a non-boolean, and `page` returning `404` when out of range).

**`GET /api/v1/instruments/`**

| Parameter | Match | Accepted values | Example |
|---|---|---|---|
| `symbol` | exact, **case-insensitive** | any ticker | `?symbol=aapl` |
| `exchange` | exact, **case-insensitive** | exchange `code` | `?exchange=NASDAQ` |
| `asset_type` | exact, **case-sensitive** | `equity`, `etf`, `crypto`, `forex`, `index`, `future` | `?asset_type=equity` |
| `is_active` | boolean | `true` or `false`, case-insensitive. **Any other value is silently ignored** | `?is_active=true` |
| `search` | substring of symbol **or** name, case-insensitive | any text | `?search=Apple` |
| `ordering` | sort | `symbol`, `name`, `asset_type`, `created_at`; prefix `-` to reverse. An unknown field is ignored | `?ordering=-symbol` |
| `page`, `page_size` | pagination | `page_size` ≤ 100; a non-numeric or out-of-range `page` returns **404** | `?page_size=50` |

Default order is `symbol` ascending.

> **`is_active` accepts only `true` and `false`.** `?is_active=1`, `?is_active=yes`,
> and `?is_active=` are all silently *ignored*, returning every instrument rather
> than the active ones. That is `django-filter`'s documented `BooleanFilter`
> contract, but it is a trap: a client asking for active-only gets no error and
> no hint that its filter did nothing. Worth knowing before you publish this
> endpoint to a frontend.

**`GET /api/v1/market-data/instruments/<id>/prices/`**

| Parameter | Match | Accepted values | Example |
|---|---|---|---|
| `start_date` | inclusive lower bound | ISO date `YYYY-MM-DD` | `?start_date=2024-03-12` |
| `end_date` | inclusive upper bound | ISO date `YYYY-MM-DD` | `?end_date=2024-03-14` |
| `ordering` | sort | `trading_date`, `close`, `volume`; prefix `-` to reverse | `?ordering=-trading_date` |
| `page`, `page_size` | pagination | `page_size` ≤ 100 | `?page_size=100` |

Default order is `trading_date` ascending. A malformed date or an inverted
range (`start_date` after `end_date`) is a `400`.

**`GET /api/v1/ingestion/`**

| Parameter | Match | Accepted values | Example |
|---|---|---|---|
| `status` | exact | `pending`, `processing`, `completed`, `completed_with_errors`, `failed`, `cancelled` | `?status=processing` |
| `source_type` | exact | `csv`, `json`, `xlsx`, `api`, `manual` | `?source_type=csv` |
| `ordering` | sort | `requested_at`, `status`, `rows_received`; prefix `-` to reverse | `?ordering=-requested_at` |
| `page`, `page_size` | pagination | `page_size` ≤ 100 | `?page_size=10` |

Default order is `requested_at` descending — newest first.

**`GET /api/v1/ingestion/<uuid>/validation-errors/`**

| Parameter | Match | Accepted values | Example |
|---|---|---|---|
| `error_code` | exact | any code the pipeline records | `?error_code=negative_volume` |
| `ordering` | sort | `row_number`, `error_code`; prefix `-` to reverse | `?ordering=-row_number` |
| `page`, `page_size` | pagination | `page_size` ≤ 100 | `?page_size=100` |

Default order is `row_number` ascending, so rejections read in file order.

**`GET /api/v1/ingestion/<uuid>/rule-summaries/`**

| Parameter | Match | Accepted values | Example |
|---|---|---|---|
| `ordering` | sort | `rule_name`, `passed`, `failed`; prefix `-` to reverse | `?ordering=-failed` |

Unpaginated — a job has one row per rule. Default order is `rule_name` ascending.

**`GET /api/v1/analytics/instruments/<id>/metrics/`**

| Parameter | Match | Accepted values | Example |
|---|---|---|---|
| `metric_type` | exact | `return`, `moving_average`, `volatility`, `drawdown` | `?metric_type=return` |
| `start_date` | inclusive lower bound | ISO date `YYYY-MM-DD` | `?start_date=2024-01-01` |
| `end_date` | inclusive upper bound | ISO date `YYYY-MM-DD` | `?end_date=2024-03-31` |
| `ordering` | sort | `as_of_date`, `metric_type`, `value`; prefix `-` to reverse | `?ordering=-as_of_date` |
| `page`, `page_size` | pagination | `page_size` ≤ 100 | `?page_size=50` |

Default order is `as_of_date` descending, then `metric_type`.

**`GET /api/v1/analytics/instruments/<id>/series/`**

One metric of one instrument, computed on demand from stored prices. `metric`
selects which one; `start_date`, `end_date` and `backend` apply to all of them.

| Parameter | Match | Accepted values | Default when omitted |
|---|---|---|---|
| `metric` | exact | `returns`, `cumulative_returns`, `moving_averages`, `volatility`, `drawdown`, `extremes`, `volume`, `percentage_change` | `returns` |
| `start_date` | inclusive lower bound | ISO date `YYYY-MM-DD` | the instrument's first session |
| `end_date` | inclusive upper bound | ISO date `YYYY-MM-DD` | the instrument's last session |
| `backend` | exact | `sql`, `python`, `pandas` — but only where the metric has one: `cumulative_returns` and `percentage_change` take `python` or `pandas` | the metric's own (`sql` where it exists, else `python`) |
| `window` | integer ≥ 2 | `volatility` only | `30` |
| `names` | comma-separated | `moving_averages` only: `moving_average_7`, `moving_average_30`, `moving_average_90` | all three |
| `periods` | JSON array | `percentage_change` only: `[{"label": "q1", "start_date": "...", "end_date": "..."}]`; `label` is optional | one period spanning `start_date`..`end_date` |

Unpaginated deliberately: a windowed metric cannot be paged without recomputing
its warm-up for every page. The body is one object whose `points` are the series,
beside `padding` (warm-up rows read but not answered), `undefined` (points the
metric could not compute) and the metric's own summary. A `window` on any other
metric, or any other parameter these do not take, is a `400`.

**`GET /api/v1/analytics/rankings/`**

Instruments compared to one another over one window — no instrument in the path,
because the answer is *about* symbols.

| Parameter | Match | Accepted values | Default when omitted |
|---|---|---|---|
| `measure` | exact | `return`, `volume` | `return` |
| `instrument_ids` | comma-separated ids | any instrument primary keys | every instrument priced in the window |
| `start_date` | inclusive lower bound | ISO date `YYYY-MM-DD` | the window's first session |
| `end_date` | inclusive upper bound | ISO date `YYYY-MM-DD` | the window's last session |
| `lookback` | integer ≥ 1 | sessions, **per instrument** | the whole bounded range |
| `descending` | boolean | `true`, `false`, `1`, `0`, `yes`, `no` | `true` (best first) |
| `limit` | integer ≥ 0 | how many rows to return | every ranked instrument |
| `backend` | exact | `python`, `pandas` | `python` |

`limit` truncates **after** ranking, so the rows that come back keep their real
places: with `limit=1` the one row is still rank `1`, and `truncated` says how many
were left out.

> **These two endpoints reject an unknown query parameter** (`400`) instead of
> ignoring it as the rest of the API does — including a parameter belonging to a
> *different* metric, such as `?metric=moving_averages&window=7`. Ignoring it would
> return a three-series answer that looks like it honoured the window.

**`GET /api/v1/reports/audit-events/`**

| Parameter | Match | Accepted values | Example |
|---|---|---|---|
| `action` | exact | any recorded action, e.g. `ingestion.upload_loaded` | `?action=ingestion.upload_loaded` |
| `object_type` | exact | e.g. `IngestionJob`, `User` | `?object_type=IngestionJob` |
| `object_id` | exact | the target's id | `?object_id=8f0c2e7a-…` |
| `user` | exact, **case-insensitive email** | an actor's email | `?user=analyst@example.com` |
| `request_id` | exact | a request id | `?request_id=req-9` |
| `since`, `until` | inclusive bounds | ISO 8601 datetime | `?since=2024-03-12T00:00:00Z` |
| `ordering` | sort | `created_at`, `action`, `object_type`; prefix `-` to reverse | `?ordering=created_at` |
| `search` | substring | `action`, `object_type`, `object_id`, `request_id`, actor email | `?search=upload` |
| `page`, `page_size` | pagination | `page_size` ≤ 100 | `?page_size=50` |

Default order is `created_at` descending — newest first. Administrator only.

**Endpoints with no query parameters**

These take path or body parameters only. A `GET` sent with a query string still
returns `200` — the parameters are simply ignored.

| Endpoint | Parameters that apply |
|---|---|
| `GET /api/v1/auth/me/` | none |
| `GET /api/v1/auth/usage/` | none |
| `POST /api/v1/auth/register/` | body: `email`, `password`, `first_name`, `last_name` |
| `POST /api/v1/auth/login/` | body: `email`, `password` |
| `POST /api/v1/auth/logout/` | body: `refresh` |
| `POST /api/v1/auth/token/refresh/` | body: `refresh` |
| `POST /api/v1/ingestion/` | body: `source_type` |
| `POST /api/v1/ingestion/upload/` | multipart body: `file` (required), `source_type`, `sheet_name`, `on_invalid`, `on_existing` |
| `POST /api/v1/ingestion/submit/` | same body as `upload/`, plus an optional `Idempotency-Key` **header** |
| `POST /api/v1/ingestion/<uuid>/cancel/` | path: `uuid` — no body |
| `GET /api/v1/ingestion/<uuid>/report/` | path: `uuid` — the report has no parameters |
| `GET /api/v1/instruments/<id>/` | path: `id` — instrument primary key |
| `GET /api/v1/instruments/exchanges/` | none — and deliberately **unpaginated** |
| `GET /api/v1/reports/me/` | none — the report is about the caller, so there is nothing to name |
| `GET /api/v1/reports/platform/` | none — administrator only, and it aggregates everything |

**Pagination on every list.** `PageNumberPagination`, `page_size=25` by default,
`max_page_size=100`, overridable with `?page_size=`. Responses are always
`{"count", "next", "previous", "results"}` — `next` and `previous` are URLs or
`null`. The exceptions are `/instruments/exchanges/` and
`/ingestion/<uuid>/rule-summaries/`, which set `pagination_class = None` because
a small, fixed set of rows reads better without a page wrapper.

### Verified filter behaviour

Every row below was checked against a running server with four instruments
(three active, one inactive) rather than asserted from memory:

| Query | Result | Why |
|---|---|---|
| `(no params)` | `200`, 4 rows | default ordering by `symbol` |
| `?symbol=AAPL` | `200`, 1 row | exact match |
| `?symbol=aapl` | `200`, 1 row | `iexact` — case does not matter |
| `?symbol=AAPLX` | `200`, 0 rows | no match, not an error |
| `?exchange=NASDAQ` | `200`, 2 rows | joins on `exchange.code` |
| `?exchange=nasdaq` | `200`, 2 rows | case-insensitive |
| `?asset_type=equity` | `200`, 3 rows | valid choice |
| `?asset_type=EQUITY` | **`400`** | case-**sensitive**; uppercase is an invalid choice |
| `?is_active=true` | `200`, 3 rows | works |
| `?is_active=false` | `200`, 1 row | works, case-insensitive |
| `?is_active=1` | `200`, **4 rows** | ⚠️ silently ignored — filter not applied |
| `?is_active=maybe` | `200`, **4 rows** | ⚠️ silently ignored |
| `?search=Inc` | `200`, 4 rows | substring on symbol **or** name |
| `?ordering=-symbol` | `200`, 4 rows | reversed |
| `?ordering=nope` | `200`, 4 rows | unknown field ignored |
| `?bogus_param=1` | `200` | unknown parameter ignored |
| `?page=1` | `200` | first page |
| `?page=999` | **`404`** | past the last page is not-found, not empty |
| `?page=abc` | **`404`** | non-numeric page |
| `?page_size=abc` | `200` | ignored, falls back to 25 |
| `?page_size=1000` | `200` | clamped to `max_page_size` of 100 |

### Request and response examples

**List instruments** — `GET /api/v1/instruments/?asset_type=equity&exchange=NASDAQ&search=Apple&ordering=-symbol&page_size=10`

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "symbol": "MSFT",
      "name": "Microsoft Corporation",
      "asset_type": "equity",
      "exchange_code": "NASDAQ",
      "currency": "USD",
      "is_active": true
    }
  ]
}
```

**Retrieve one instrument** — `GET /api/v1/instruments/1/`

```json
{
  "id": 1,
  "symbol": "AAPL",
  "name": "Apple Inc.",
  "asset_type": "equity",
  "exchange": { "id": 1, "code": "NASDAQ", "name": "Nasdaq", "timezone": "America/New_York" },
  "currency": "USD",
  "is_active": true,
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z"
}
```

**Price history** — `GET /api/v1/market-data/instruments/1/prices/?start_date=2024-03-12&end_date=2024-03-14`

```json
{
  "count": 3,
  "results": [
    {
      "trading_date": "2024-03-12",
      "open": "102.00000000",
      "high": "107.00000000",
      "low": "97.00000000",
      "close": "102.00000000",
      "adjusted_close": null,
      "volume": 1000,
      "source": "test"
    }
  ]
}
```

**Create a job** — `POST /api/v1/ingestion/` with body `{"source_type": "csv"}`

```json
{
  "id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
  "source_type": "csv",
  "status": "pending",
  "requested_at": "2026-10-12T03:57:22Z",
  "started_at": null,
  "completed_at": null,
  "rows_received": 0,
  "rows_accepted": 0,
  "rows_rejected": 0,
  "error_summary": "",
  "created_by_email": "analyst@example.com"
}
```

`source_type` is one of `csv`, `json`, `xlsx`, `api`, `manual`. Only that one field is
writable — `status`, timestamps, and row counts are absent from the input
serializer entirely, so sending them is ignored rather than trusted. An unknown
value is a `400` with `{"source_type": ["\"x\" is not a valid choice."]}`.

**Upload and run a load** — `POST /api/v1/ingestion/upload/` with
`multipart/form-data` and a `file` part

```json
{
  "job": {
    "id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
    "source_type": "csv",
    "status": "completed",
    "rows_received": 360,
    "rows_accepted": 360,
    "rows_rejected": 0,
    "created_by_email": "analyst@example.com"
  },
  "report": {
    "job_id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
    "status": "completed",
    "rows_received": 360,
    "rows_accepted": 360,
    "rows_rejected": 0,
    "rules": [
      { "rule": "non_positive_price", "passed": 360, "failed": 0, "status": "pass" }
    ],
    "sample_failures": []
  }
}
```

The file is checked before the job exists: an unsupported extension or an
oversize upload is a `400` naming the `file` field, and no job is created.
`source_type` may be omitted — it is inferred from the extension.

**Data-quality report** — `GET /api/v1/ingestion/<uuid>/report/`

```json
{
  "job_id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
  "status": "completed_with_errors",
  "rows_received": 15,
  "rows_accepted": 2,
  "rows_rejected": 13,
  "error_summary": "received=15; accepted=2; rejected=13; failures: non_positive_price=2, high_below_low=1",
  "rules": [
    { "rule": "non_positive_price", "passed": 13, "failed": 2, "status": "fail" },
    { "rule": "high_below_low", "passed": 14, "failed": 1, "status": "fail" }
  ],
  "sample_failures": [
    { "row_number": 5, "rule": "non_positive_price", "field": "open", "detail": "open=0.00 must be greater than zero" }
  ]
}
```

**Rule verdicts** — `GET /api/v1/ingestion/<uuid>/rule-summaries/?ordering=-failed`
(unpaginated)

```json
[
  { "rule_name": "non_positive_price", "passed": 13, "failed": 2, "status": "fail" },
  { "rule_name": "high_below_low", "passed": 14, "failed": 1, "status": "fail" },
  { "rule_name": "missing_symbol", "passed": 15, "failed": 0, "status": "pass" }
]
```

**List your own jobs** — `GET /api/v1/ingestion/?status=processing&source_type=csv`

```json
{
  "count": 1,
  "results": [
    {
      "id": "2043cca3-ec4f-4817-8fa0-40ad78ac08e7",
      "source_type": "csv",
      "status": "processing",
      "rows_received": 100,
      "rows_accepted": 90,
      "rows_rejected": 10,
      "created_by_email": "analyst@example.com"
    }
  ]
}
```

**Validation errors** — `GET /api/v1/ingestion/<uuid>/validation-errors/?error_code=negative_volume`

```json
{
  "count": 1,
  "results": [
    {
      "id": 5,
      "row_number": 7,
      "field_name": "volume",
      "error_code": "negative_volume",
      "raw_row": { "row": 7, "close": "abc" }
    }
  ]
}
```

**Analytics metrics** — `GET /api/v1/analytics/instruments/1/metrics/?metric_type=return`

```json
{
  "count": 1,
  "results": [
    {
      "symbol": "AAPL",
      "metric_type": "return",
      "as_of_date": "2024-03-15",
      "lookback_days": 30,
      "value": "0.08000000",
      "metadata": { "method": "simple" },
      "calculated_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

That endpoint reads *stored* snapshots. The metrics engine itself — daily returns,
cumulative returns, 7/30/90-day moving averages, rolling volatility, maximum
drawdown, price extremes, volume summaries, percentage change over a period, and
cross-sectional rankings by return and by volume — is served by the two endpoints
below, which compute on demand and cache nothing (a computed series depends on
price rows, and their writes do not invalidate the analytics cache).

**Daily returns, computed on demand** — `GET /api/v1/analytics/instruments/1/series/?metric=returns&start_date=2024-03-12`

```json
{
  "symbol": "AAPL",
  "backend": "sql",
  "start_date": "2024-03-12",
  "end_date": null,
  "padding": 1,
  "undefined": 0,
  "points": [
    {
      "trading_date": "2024-03-12",
      "close": "102.00000000",
      "previous_close": "100.00000000",
      "value": "0.02000000"
    },
    {
      "trading_date": "2024-03-13",
      "close": "101.00000000",
      "previous_close": "102.00000000",
      "value": "-0.00980392"
    },
    {
      "trading_date": "2024-03-14",
      "close": "103.00000000",
      "previous_close": "101.00000000",
      "value": "0.01980198"
    },
    {
      "trading_date": "2024-03-15",
      "close": "104.00000000",
      "previous_close": "103.00000000",
      "value": "0.00970874"
    }
  ]
}
```

**A cross-sectional ranking** — `GET /api/v1/analytics/rankings/?measure=return&limit=2`

```json
{
  "measure": "return",
  "backend": "python",
  "descending": true,
  "start_date": null,
  "end_date": null,
  "lookback": null,
  "universe": 3,
  "ranked": 3,
  "unranked": 0,
  "truncated": 1,
  "rows": [
    { "rank": 1, "instrument_id": 3, "symbol": "GOOG", "value": "0.08000000", "observations": 5, "first_date": "2024-03-11", "last_date": "2024-03-15" },
    { "rank": 2, "instrument_id": 2, "symbol": "MSFT", "value": "0.05000000", "observations": 5, "first_date": "2024-03-11", "last_date": "2024-03-15" }
  ]
}
```

Every value is a **string**: money and ratios at eight decimal places (the width of
`AnalyticSnapshot.value`), counts whole, and a ranking's `value` at the precision
its measure carries — so the payload does not depend on which database driver
answered it. A measure the engine could not compute is `null`, counted in
`undefined` (or `unranked`, in a ranking).

The engine is documented separately in
[docs/market_analytics.md](docs/market_analytics.md): the formula behind each
measure, how missing prices and zero denominators are handled, trading days versus
calendar days, the SQL-window and Pandas implementations, and the hand-calculated
samples its tests assert.

**Usage statistics** — `GET /api/v1/auth/usage/`

```json
{
  "jobs": {
    "total": 4,
    "by_status": { "pending": 3, "completed": 1 },
    "rows_received": 160,
    "rows_accepted": 140,
    "rows_rejected": 20
  },
  "prices": {
    "records_imported": 137,
    "instruments_covered": 3,
    "earliest_date": "2024-03-11",
    "latest_date": "2024-03-15"
  }
}
```

### Status codes and ownership

| Code | When |
|---|---|
| `200` | list or retrieve succeeded, or a replay of an idempotent submit |
| `201` | job created |
| `202` | job accepted for asynchronous processing (`submit/`) |
| `400` | invalid filter, malformed date, or inverted date range |
| `401` | missing or invalid token |
| `403` | authenticated, but the object belongs to someone else |
| `404` | the object does not exist |
| `429` | rate limited (`register` 5/hr, `login` 10/min) |

Ownership is enforced two ways. **List** endpoints scope the queryset to
`request.user` *before* filtering, so another user's jobs can never match.
**Detail** endpoints use `get_object_or_404` across all jobs and then
`check_object_permissions`, returning **403 rather than 404** — the caller is
authenticated and the resource genuinely exists, so 404 would be a lie.

Views with custom `permission_classes` list `IsAuthenticated` explicitly,
because view-level permissions *replace* the global default. Without it an
anonymous request to a job detail would return 403 instead of the accurate 401.

### Architecture

Each app follows the same four-file shape:

| File | Responsibility |
|---|---|
| `services.py` | querysets and business rules, as plain functions with keyword-only args |
| `serializers.py` | input validation and output representation |
| `views.py` | thin adapters: parse request, call a service, render |
| `filters.py` | query-parameter to ORM lookup mapping (`django-filter`) |

Views never build querysets themselves and never contain business rules. A
Celery task, a management command, or another service can call
`services.list_instruments(symbol="AAPL")` directly without HTTP, which is the
point of the split.

Every list joins its foreign keys through `select_related` in the service layer,
so query counts stay constant rather than growing with page size — asserted in
tests via `assertNumQueries` (3 for instruments, 4 for prices, 2 for job detail).

### Reporting

`apps/reports` answers questions *about* the data the pipeline produced rather
than serving it. It has **no models** — every report is an aggregate over rows
another app owns — and it is read-only and uncached.

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/reports/me/` | authenticated, **self-scoped** | the caller's own ingestion analytics |
| `GET` | `/api/v1/reports/platform/` | **administrator only** | platform-wide aggregates and failure summaries |
| `GET` | `/api/v1/reports/audit-events/` | **administrator only** | paginated audit log |

The per-user report returns the job outcome buckets (successful, partially
successful, failed), the record totals, the average and maximum run time, the most
recent job, daily job counts, and counts grouped by source and status — all from
Django ORM annotations and aggregations, in a constant five queries. There is no
path parameter and no `user` query parameter: the report is built from
`filter(created_by=request.user)`, so another user's jobs are never selected.

The platform report takes the same measurements over every job and adds the user
leaderboard and a failure summary (the failed count, the most recent failures with
their error lines, and the validation rules ranked by rows rejected), in a
constant seven queries. It is guarded by `IsAuthenticated, IsAdministrator`, so an
anonymous caller gets `401` and an analyst `403`.

The audit log is the one report that returns stored rows rather than an aggregate:
a paginated, filterable, administrator-only page of `AuditEvent`s. It is what the
rest of the application *writes* into — an append-only record of a recalled set of
operations (job created/queued, upload loaded, job cancelled, role changed; the
exact vocabulary is in `apps/audit/events.py`) — and it is read back here. Nothing
sensitive can be written to it: `record_event` sanitizes every event's metadata
through a deny-list of credential keys before the row is stored. See
[docs/reports.md](docs/reports.md) for the sample dataset, the expected page, the
API examples, and the design notes (conditional aggregation, one-to-many join
inflation, `rows_received` versus `rows_processed`, and audit events versus logs
versus job status).

## Ingestion ETL

Price batches are loaded synchronously by `apps.ingestion.etl` — one pipeline for
CSV, JSON, XLSX, and API sources, with per-rule PASS/FAIL reporting and a job row
for every run. Drive it three ways: upload a file to
`POST /api/v1/ingestion/upload/`, call `apps.ingestion.services`, or use the shell:

```powershell
python manage.py ingest_prices --file path\to\prices.csv --email analyst@example.test
```

Row rejection, duplicate handling, and the transaction boundary are decided by an
explicit `IngestionPolicy` (`on_invalid`, `on_existing`), which the upload endpoint
exposes as form fields. See [docs/ingestion_etl.md](docs/ingestion_etl.md) for the
module contracts, the rules, and the sample-dataset results.

## Asynchronous ingestion (Celery)

The same pipeline also runs on a worker, for batches too large to hold a request
open. [Celery](https://docs.celeryq.dev/) with **Redis as the broker** carries the
message; the database row is the source of truth for what happened.

```powershell
# Queue a file instead of loading it inline. Returns 202 with the job.
# (-Form needs PowerShell 7+; on Windows PowerShell 5.1 use curl.exe -F.)
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/ingestion/submit/ `
  -Method Post `
  -Headers @{ Authorization = "Bearer $($tokens.access)"; "Idempotency-Key" = "2026-10-12-daily-prices" } `
  -Form @{ file = Get-Item sample_data\synthetic\batches\prices_batch_004.csv }

# Watch it run.
python manage.py ingestion_status --queue

# Start a worker (Windows needs the solo pool).
.\scripts\run_worker.ps1 -Action worker
```

`POST /api/v1/ingestion/submit/` answers `202` for a new job and `200` when the
`Idempotency-Key` (or, by default, the file's SHA-256) matches one already
submitted by the same user — so a client that times out and retries does not
queue the file twice. `POST /api/v1/ingestion/<uuid>/cancel/` asks a running job
to stop; the worker checks between chunks and finishes the job as `cancelled`.

A job moves through `pending` → `processing` → one of `completed`,
`completed_with_errors`, `failed`, or `cancelled`. `attempts`, `rows_processed`,
and `heartbeat_at` are exposed on the job serializer, so a stalled job is visible
as a row that says `processing` with a stale heartbeat rather than a request that
never returns.

Two use cases are deliberately *not* on this path. An `api` source cannot be
queued — its fetcher is not serialisable, and accepting a client-supplied URL
would be an SSRF hole — so API ingestion stays synchronous. And nothing here
assumes a task runs exactly once: delivery is at-least-once, so the job claim and
the row upsert are both idempotent.

See [docs/celery_async.md](docs/celery_async.md) for the state machine, the retry
classification, the nine failure scenarios and how each is tested, the
transaction boundaries, and the measured performance on large files.

## Database schema

The shape is a star schema: `Instrument` is the
dimension, `PriceRecord` is the fact table, and everything else hangs off a job
or a user.

```
Exchange 1 ────< Instrument 1 ────< PriceRecord          (reference data)
                                   ^
                                   │ SET_NULL (history outlives the job)
IngestionJob 1 ────┬──< PriceRecord
                   ├──< IngestionFile
                   ├──< IngestionValidationError
                   └──< IngestionRuleSummary
User 1 ────────────< IngestionJob       (PROTECT, never CASCADE)
User 1 ────────────< AuditEvent         (SET_NULL, the record outlives the user)
Instrument 1 ──────< AnalyticSnapshot   (CASCADE, a snapshot is worthless without it)
```

| Model | Primary key | Notable constraint |
|---|---|---|
| `User` | `BigAutoField` | `email` unique, `USERNAME_FIELD = "email"` |
| `Exchange` | `BigAutoField` | `code` unique |
| `Instrument` | `BigAutoField` | `symbol` unique; `asset_type` indexed |
| `PriceRecord` | `BigAutoField` | unique `(instrument, trading_date)`; `high >= low`; `volume >= 0` |
| `IngestionJob` | `UUID` | non-negative row counts |
| `IngestionFile` | `UUID` | unique `(job, checksum)` — the same bytes twice is a duplicate |
| `IngestionValidationError` | `BigAutoField` | `error_code` indexed |
| `IngestionRuleSummary` | `BigAutoField` | unique `(job, rule_name)` — one PASS/FAIL row per rule |
| `AnalyticSnapshot` | `BigAutoField` | unique `(instrument, as_of_date, metric_type, lookback_days)` |
| `AuditEvent` | `BigAutoField` | `request_id` indexed; `(user, -created_at)` and `(-created_at)` indexed |

**Deletion policy, decided per relationship rather than globally:**

- `PROTECT` where deleting the parent would destroy history no one can recreate
  (an instrument's prices, an exchange's instruments, a user's jobs).
- `SET_NULL` where the child outlives the parent's usefulness (a price record
  survives the job that imported it; an audit row survives its user).
- `CASCADE` only where the child is meaningless alone (a snapshot without its
  instrument, a file without its job).

**Keys.** Surrogate `BigAutoField` everywhere rather than natural keys, so a
foreign reference never has to carry `(symbol, trading_date)` and renaming an
instrument doesn't break anything. 64-bit rather than 32-bit because the price
table is the one that grows without bound: 5,000 instruments x 250 trading days
x 30 years is roughly 37.5M rows and `AutoField` runs out at 2.1B. Jobs and
files use UUID primary keys so they can be referenced in logs and URLs without
leaking a sequence estimate.

**Prices are `DecimalField(18, 8)`, never floats.** Binary floating point cannot
represent `0.1` exactly, so float arithmetic silently drifts on financial
figures. Eight decimal places covers every asset class in scope — crypto quotes
to 8, forex to 5, equities to 2. `volume` is a `BigIntegerField` because
equities trade whole shares; if fractional-volume crypto is added it becomes a
migration to `DecimalField`.

**Nullable means unknown, never zero.** `adjusted_close` is nullable because a
feed may not publish an adjustment; a zero would be a real value and would
corrupt return calculations. `AnalyticSnapshot.metadata` is nullable for the
same reason.

## Creating an administrator

```powershell
python manage.py createsuperuser   # prompts for email and password
```

The role is set to `administrator` automatically. To change another user's role,
edit it in the admin at `/admin/`.

## File ownership

```text
market_data_analysiss/
├── manage.py                 # Django management commands
├── config/                   # Project settings and root routing
├── apps/
│   ├── accounts/             # Custom user model, login, roles
│   ├── analytics/            # AnalyticSnapshot
│   ├── api/                  # Public facade: /stocks/, /jobs/, /me/
│   ├── audit/                # AuditEvent: append-only, records + sanitizes actions
│   ├── common/               # Shared permissions and the error envelope
│   ├── ingestion/            # IngestionJob, IngestionFile, IngestionValidationError, IngestionRuleSummary
│   ├── instruments/          # Exchange, Instrument
│   ├── marketdata/           # PriceRecord, TradingCalendar
│   └── reports/              # Read-only reporting: user, platform, audit log
├── scripts/                  # Local utility scripts, including sample-data generation
├── templates/                # Project-wide templates
├── static/                   # Source static assets
├── media/                    # Local user-upload destination; ignored by Git
├── .env                      # Local secrets/configuration; ignored by Git
├── .env.example              # Safe environment-variable template
├── requirements.txt          # Python dependency constraints
└── .gitignore                # Files Git must not track
```

- `manage.py`: Django management-command entry point.
- `config/`: project-level settings, URL routing, and ASGI/WSGI server entry points.
- `apps/accounts/`: the custom `User` model (email login), JWT endpoints, and the
  analyst/administrator role field.
- `apps/api/`: the public facade (`/stocks/`, `/jobs/`, `/me/`) — thin views with
  no models, delegating to the domain services. Documented in
  [docs/public_api.md](docs/public_api.md).
- `apps/common/`: `IsOwner` for object-level ownership checks and the single
  `{"error": {...}}` response envelope used by every endpoint.
- `apps/instruments/`: `Exchange` and `Instrument`, the reference-data dimension table.
- `apps/marketdata/`: `PriceRecord` (the fact table) and `TradingCalendar`.
- `apps/ingestion/`: `IngestionJob` with its `IngestionFile`,
  `IngestionValidationError` (one per rejected row), and `IngestionRuleSummary`
  (one PASS/FAIL per rule) children.
- `apps/analytics/`: `AnalyticSnapshot`, one metric per instrument, date, and lookback.
- `apps/audit/`: `AuditEvent`, append-only records of privileged actions. The
  write path is `apps.audit.services.record_event` (guarded by the event
  vocabulary and the never-log sanitizer in `apps/audit/events.py`); the read path
  is the audit-log report.
- `apps/reports/`: read-only reporting — the per-user (`/api/v1/reports/me/`),
  platform (`/api/v1/reports/platform/`, administrator only), and audit-log
  (`/api/v1/reports/audit-events/`, administrator only) endpoints documented in
  [docs/reports.md](docs/reports.md). No models: each report aggregates rows
  another app owns, or pages them.
- `templates/`, `static/`, and `media/`: project templates, static assets, and local uploaded media.
- `.env`: ignored local configuration. `.env.example`: safe key-only template.

## Commands Django would normally generate

For a fresh empty directory, Django can generate this same base layout:

```powershell
django-admin startproject config .
New-Item -ItemType Directory apps
New-Item -ItemType File apps\__init__.py
```

Later, create domain apps only when their first endpoint/model is being built:

```powershell
python manage.py startapp accounts apps\accounts
python manage.py startapp instruments apps\instruments
python manage.py startapp ingestion apps\ingestion
python manage.py startapp marketdata apps\marketdata
python manage.py startapp analytics apps\analytics
python manage.py startapp audit apps\audit
python manage.py startapp common apps\common
```
Progress that I've made:
initialized a clean app-based project structure.
Explaind which files Django generates and which folders we create ourselves.
Set up the config package and the initial Django settings.
Configure environment variables using a .env file locally and provide a safe .env.example.
Configured MySQL, static/media settings, logging, and installed apps.
Create a requirements.txt with compatible, deliberately selected dependency versions.
Provide a .gitignore and explain what must never be committed.
Add a minimal smoke test and the commands to run it.
Use small steps. For each file, explain its purpose and provide the complete contents. Do not introduce Celery tasks, business logic, or all database models yet.
Never hardcoded real credentials. Verify that the project starts, the database connection works, and the health check passes before proceeding.
