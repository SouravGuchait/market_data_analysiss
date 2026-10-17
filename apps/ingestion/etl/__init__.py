"""Synchronous ETL for market-data price batches.

The engine is split into single-purpose modules so each can be tested without
the others:

    sources.py        read a CSV / JSON / XLSX / API payload  -> raw dicts
    schemas.py        raw dicts -> one typed row (normalisation)
    rules.py          the reusable data-quality rules
    validation.py     run the rules over a row, apply precedence
    transform.py      pandas: sort, de-duplicate, type for storage
    persistence.py    Django ORM bulk upsert inside a transaction
    storage.py        safe file validation, size limits, checksum, placement
    reporting.py      rule-level PASS/FAIL report for a job
    pipeline.py       the orchestrator that ties the above together

Nothing here imports Celery. ``services.py`` is the thin entry point other code
should call; the modules above are implementation.

Reading guide for the data flow: a *source* yields :class:`RawRow` values; each
becomes one :class:`~apps.ingestion.etl.schemas.NormalizedRow` carrying typed
values plus any structural problem; :func:`apps.ingestion.etl.validation.validate_row`
turns that into a list of :class:`~apps.ingestion.etl.schemas.Outcome`; a row
with no outcomes is transformed and persisted, a row with outcomes is recorded
and skipped.
"""
