"""Django ORM persistence: bulk upsert of validated rows inside a transaction.

This module is the only place that writes price rows. It is deliberately narrow:

* it never validates — everything handed to it is already known good;
* it upserts rather than inserts, so re-ingesting a file is idempotent and the
  ``uniq_price_instrument_trading_date`` constraint turns a repeat into an
  update instead of an error;
* it resolves symbols through :class:`SymbolIndex`, one query for the whole
  reference table, because a per-row lookup is the classic N+1.

The transaction boundary lives in the caller (:mod:`apps.ingestion.etl.pipeline`)
so a "reject the whole batch" policy can roll back every chunk at once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.db import connection

from apps.instruments.models import Instrument
from apps.marketdata.models import PriceRecord, TradingCalendar

# Columns a re-ingest refreshes when it meets an existing (instrument, date).
UPSERT_UPDATE_FIELDS = [
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "source",
    "ingestion_job",
]


class SymbolIndex:
    """A case-insensitive symbol -> instrument-id lookup, loaded once.

    Contract: ``load()`` performs a single ``values_list`` query over
    ``Instrument`` and holds only ``{UPPERCASE_SYMBOL: pk}``. The pipeline uses
    it both to answer "is this symbol known?" during validation and to attach the
    foreign key during persistence, so no per-row query is ever needed.
    """

    def __init__(self, mapping: Mapping[str, int] | None = None):
        self._mapping: dict[str, int] = dict(mapping or {})

    @classmethod
    def load(cls) -> SymbolIndex:
        mapping = {
            symbol.upper(): identifier
            for symbol, identifier in Instrument.objects.values_list("symbol", "id")
        }
        return cls(mapping)

    @property
    def known_symbols(self) -> set[str]:
        return set(self._mapping)

    def id_for(self, symbol: str) -> int | None:
        return self._mapping.get(symbol.strip().upper())

    def __len__(self) -> int:
        return len(self._mapping)


@dataclass
class PersistResult:
    """What one upsert batch did, so the job's counts can be reported honestly."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    duplicates: list[tuple[str, str]] = field(default_factory=list)

    @property
    def written(self) -> int:
        return self.created + self.updated


def existing_price_keys(
    instrument_ids: Sequence[int], dates: Sequence[date]
) -> set[tuple[int, date]]:
    """Return the (instrument_id, trading_date) pairs that already exist.

    One query for the whole candidate set, not one per row. The result is used to
    classify each write as an insert or an update and to honour the
    ``on_existing`` policy.
    """
    if not instrument_ids or not dates:
        return set()
    return set(
        PriceRecord.objects.filter(
            instrument_id__in=list(instrument_ids), trading_date__in=list(dates)
        ).values_list("instrument_id", "trading_date")
    )


def upsert_kwargs(*, supports_conflict_target: bool) -> dict[str, Any]:
    """The ``bulk_create`` keyword arguments for a portable upsert.

    PostgreSQL and SQLite require the conflict target (``unique_fields``);
    MySQL's ``ON DUPLICATE KEY UPDATE`` infers it from the table's unique index
    and rejects an explicit target. Passing ``unique_fields`` unconditionally is a
    load that passes against the SQLite test suite and raises on MySQL, so the
    choice is made from the connection's capability flag rather than assumed.
    """
    kwargs: dict[str, Any] = {
        "update_conflicts": True,
        "update_fields": UPSERT_UPDATE_FIELDS,
    }
    if supports_conflict_target:
        kwargs["unique_fields"] = ["instrument", "trading_date"]
    return kwargs


def upsert_prices(
    *,
    job,
    records: Sequence[dict[str, Any]],
    symbols: SymbolIndex,
    on_existing: str = "update",
    batch_size: int = 500,
) -> PersistResult:
    """Insert or update validated price records for one job.

    Contract
    --------
    in : the owning job, storage-ready record dicts (from
         :func:`apps.ingestion.etl.transform.prepare_for_load`), the symbol index,
         and the duplicate policy:

         * ``"update"`` — refresh the existing row (the idempotent default);
         * ``"skip"``   — leave the existing row untouched;
         * ``"fail"``   — treat the pre-existing row as a ``duplicate_record``.
    out: a :class:`PersistResult` with created/updated/skipped counts and any
         rows rejected under the ``"fail"`` policy.

    Business purpose: the single, explicit answer to "what happens when this
    (instrument, date) is already in the table?" — decided once, in one place,
    rather than implied by whether an insert happens to succeed.
    """
    if on_existing not in {"update", "skip", "fail"}:
        raise ValueError(f"unknown on_existing policy: {on_existing!r}")

    pairs: list[tuple[int, date]] = []
    resolved: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        instrument_id = symbols.id_for(record["symbol"])
        if instrument_id is None:  # pragma: no cover - validation should prevent this
            continue
        resolved.append((instrument_id, record))
        pairs.append((instrument_id, record["trading_date"]))

    existing = existing_price_keys(
        [instrument_id for instrument_id, _ in pairs],
        [record["trading_date"] for _, record in resolved],
    )

    to_write: list[PriceRecord] = []
    result = PersistResult()
    for instrument_id, record in resolved:
        key = (instrument_id, record["trading_date"])
        if key in existing:
            if on_existing == "skip":
                result.skipped += 1
                continue
            if on_existing == "fail":
                result.duplicates.append(
                    (record["symbol"], record["trading_date"].isoformat())
                )
                continue
            result.updated += 1
        else:
            result.created += 1

        to_write.append(
            PriceRecord(
                ingestion_job=job,
                instrument_id=instrument_id,
                trading_date=record["trading_date"],
                open=record["open"],
                high=record["high"],
                low=record["low"],
                close=record["close"],
                adjusted_close=record.get("adjusted_close"),
                volume=record["volume"],
                source=record["source"],
            )
        )

    if to_write:
        PriceRecord.objects.bulk_create(
            to_write,
            batch_size=batch_size,
            **upsert_kwargs(
                supports_conflict_target=(
                    connection.features.supports_update_conflicts_with_target
                )
            ),
        )
    return result


def expected_calendar_dates(start: date, end: date) -> set[date]:
    """Return the trading days the calendar marks between two dates, inclusive.

    An empty result means the calendar was never populated, in which case the
    missing-date rule is skipped rather than reporting every day as missing.
    """
    return set(
        TradingCalendar.objects.filter(
            date__gte=start, date__lte=end
        ).values_list("date", flat=True)
    )
