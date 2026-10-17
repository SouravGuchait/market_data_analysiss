# Synchronous ingestion ETL

One pipeline loads a price batch from a CSV, JSON, XLSX, or API source into
`PriceRecord`, records a data-quality verdict per rule, and updates an
`IngestionJob`. It runs in-process — no Celery — and is driven three ways:

* from the HTTP API (upload a file, then read the report);
* from code, via `apps.ingestion.services`;
* from the shell:

```powershell
python manage.py ingest_prices `
  --file sample_data/synthetic/batches/prices_batch_001.csv `
  --email analyst@example.test
```

## Entry points

`apps/ingestion/services.py` is the only module other layers call; everything
under `etl/` is implementation.

| Function | In → out | Purpose |
|---|---|---|
| `infer_source_type(name)` | a filename → a `SourceType` | Pick the adapter from the extension. Raises `ValueError` for an unknown one. |
| `ingest_file(user, path, …)` | owner + path → `IngestionResult` | Validate the file, create the job, register the file, run the ETL. |
| `ingest_upload(user, file, …)` | owner + `UploadedFile` → `IngestionResult` | The HTTP path: store + checksum the bytes, then run the ETL on the stored copy. |
| `run_job(job, source_type, …)` | an existing job → `IngestionResult` | Run the engine for a job that was created separately. |
| `report_for(job)` | a job → a plain dict | The data-quality report (counts, rule table, failure sample). |
| `create_job`, `list_jobs_for_user`, `get_job_for_user` | — | The job read/create surface the CRUD API uses. |

### HTTP surface

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/ingestion/` | authenticated | create a `pending` job (no file) |
| `POST` | `/api/v1/ingestion/upload/` | authenticated | upload a file and run the load; returns the job and its report |
| `GET` | `/api/v1/ingestion/<uuid>/report/` | owner | the data-quality report |
| `GET` | `/api/v1/ingestion/<uuid>/rule-summaries/` | owner | one PASS/FAIL row per rule |

`POST /upload/` is `multipart/form-data`: `file` is required; `source_type`
(defaults to the file's extension), `sheet_name`, `on_invalid`, and `on_existing`
are optional. The file is rejected up front — a bad extension or an oversize
upload is a `400` and no job is created.

## Modules and their contracts

Each stage is a separate module so it can be tested without the others. All of
them are under `apps/ingestion/etl/`.

| Module | Input → output | Purpose |
|---|---|---|
| `sources.py` | a path or a fetcher callable → `SourceReader` (header + lazy `RawRow` stream) | Read the file's own values, untyped. CSV/JSON/XLSX/API only differ here. |
| `schemas.py` | a `RawRow` + resolved columns → `NormalizedRow` | Resolve vendor aliases, coerce types, and record a structural problem instead of raising. |
| `rules.py` | a `NormalizedRow` + `RuleContext` → `list[Outcome]` | The reusable data-quality rules. |
| `validation.py` | rows + context → outcomes, while tallying per-rule pass/fail | One place decides a row's fate. |
| `transform.py` | accepted rows → pandas frame → storage-ready dicts | Sort, de-duplicate, and quantise onto the model's grid. |
| `persistence.py` | record dicts → `PersistResult` | Bulk upsert inside the caller's transaction; the only writer. |
| `storage.py` | an `UploadedFile` or path → `IngestionFile` | Name/extension/size checks and checksum before a byte is parsed. |
| `reporting.py` | a finished job → plain dict | The rule table and the failure sample. |
| `pipeline.py` | a job + a source + a policy → `IngestionResult` | Ties the stages together and owns the transaction. |

## Data flow

```text
source ──▶ RawRow ──▶ NormalizedRow ──▶ outcomes ──┬─ empty ─▶ transform ─▶ upsert
                                                   └─ non-empty ─▶ IngestionValidationError
```

A row with no outcomes is accepted and persisted. A row with outcomes is
rejected: its reasons are stored (one `IngestionValidationError` per rule) and
the row is skipped.

## The two explicit policy decisions

`IngestionPolicy` states both answers instead of leaving them to chance.

**`on_invalid` — what a partly-bad batch means**

- `skip` (default): keep every valid row, report the rest, finish **partial**.
- `abort`: one bad row rolls the whole batch back and the job finishes **failed**.

**`on_existing` — what a repeated `(instrument, trading_date)` means**

- `update` (default): refresh the stored row. Re-ingesting a file is idempotent.
- `skip`: leave the stored row untouched.
- `fail`: treat the pre-existing row as a `duplicate_record` and write nothing.

Duplicate handling is therefore explicit, and the
`uniq_price_instrument_trading_date` constraint turns a repeat into an update
rather than an error.

## Transactions

The read/validate/flush phase runs inside a single `transaction.atomic()` block,
so **a job that ends `failed` has loaded nothing**. Chunking bounds memory, not
the transaction. Validation errors, the per-rule summaries, and the job's final
counts are written *after* that block in their own transaction, so they survive a
rollback and explain the failure.

## Rules

| Rule | Meaning |
|---|---|
| `missing_symbol` | `symbol` is blank |
| `missing_required_field` | a required OHLC field or `volume` is absent or unreadable |
| `invalid_date` | `trading_date` cannot be parsed |
| `future_date` | `trading_date` is after today |
| `non_positive_price` | any of open/high/low/close is ≤ 0 |
| `high_below_low` | `high < low` |
| `open_outside_range` / `close_outside_range` | open/close outside `[low, high]` |
| `negative_volume` | `volume < 0` |
| `duplicate_record` | the same `(symbol, date)` appears twice in one batch |
| `unknown_instrument` | the symbol matches no `Instrument` |
| `missing_trading_date` | a calendar trading day has no price row (batch-level) |

A rule only fires when its inputs are sound. An open of `0` is reported once, as
`non_positive_price`, not again as an out-of-range open. Structural failures
(`missing_column`, `malformed_source`, `unsafe_file`, `empty_source`,
`aborted_on_invalid`) are recorded at `row_number = 0`.

## Rule-level PASS/FAIL

`IngestionRuleSummary` stores `(passed, failed)` per rule per job — the rule-level
verdict. Per-row detail for a failure is the `IngestionValidationError`; a row
with no error for a rule passed it, so pass rows are counted, not stored.

## Running against the sample dataset

```powershell
python manage.py ingest_prices --file sample_data/synthetic/batches/prices_batch_001.csv --email analyst@example.test
python manage.py ingest_prices --file sample_data/synthetic/dirty/prices_dirty.csv --email analyst@example.test
```

The six clean batches load 360 rows and finish `succeeded`. The dirty batch
finishes `partial` with `received=15`, `accepted=2`, `rejected=13`, one finding
per rule as listed in `sample_data/synthetic/EXPECTED_RESULTS.md`.
