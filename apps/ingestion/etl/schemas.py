"""The canonical price schema and the normaliser that maps any source onto it.

Parsing answers "what did the file literally say"; normalisation answers "what
does it mean for our model". Adapters hand back raw dicts keyed by whatever the
vendor chose, so this module resolves aliases, coerces types, and records a
structural problem for anything it could not read.

Domain rules (is the price positive, is the high above the low) are deliberately
*not* applied here. They live in :mod:`apps.ingestion.etl.rules` so that a rule
written once can be reused across CSV, JSON, XLSX, and API sources and tested in
isolation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .errors import RuleName

# The columns a price batch must carry, in the order we write them back out.
CANONICAL_FIELDS = (
    "symbol",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "source",
)

# Without these a row cannot become a PriceRecord.
REQUIRED_FIELDS = ("symbol", "trading_date", "open", "high", "low", "close", "volume")
OPTIONAL_FIELDS = ("adjusted_close", "source")
PRICE_FIELDS = ("open", "high", "low", "close")

# The price columns the model stores as Decimal(18, 8).
PRICE_DECIMAL_PLACES = 8

# Vendor spellings seen in the wild. Keys are canonical names; values are the
# header spellings we accept for them. Matching ignores case and punctuation.
ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker", "instrument", "code", "isin"),
    "trading_date": ("trading_date", "date", "price_date", "as_of", "asof", "day"),
    "open": ("open", "open_price", "opening", "o"),
    "high": ("high", "high_price", "h"),
    "low": ("low", "low_price", "l"),
    "close": ("close", "close_price", "closing", "last", "last_price", "c"),
    "adjusted_close": ("adjusted_close", "adj_close", "adjclose", "adjusted", "close_adj"),
    "volume": ("volume", "vol", "volume_traded", "shares", "total_volume"),
    "source": ("source", "feed", "vendor", "provider", "dataset"),
}


@dataclass(frozen=True)
class Outcome:
    """One rule evaluated against one row and its finding.

    ``rule`` is a :class:`~apps.ingestion.etl.errors.RuleName` value, ``field``
    names the offending column (empty when the finding is about the whole row),
    and ``detail`` is a short, safe explanation that never echoes more of the
    row than the offending value.
    """

    rule: str
    field: str = ""
    detail: str = ""


@dataclass
class NormalizedRow:
    """One source row after alias resolution and type coercion.

    A field that could not be read is ``None`` and has a matching entry in
    ``issues``; the value is never guessed. ``raw`` is a sanitised copy of the
    original row, kept so a rejected row can be shown back to an operator.
    """

    row_number: int
    symbol: str | None
    trading_date: date | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    adjusted_close: Decimal | None
    volume: int | None
    source: str
    raw: dict[str, Any]
    issues: list[Outcome] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str] | None:
        """The (symbol, iso-date) identity used for duplicate detection."""
        if self.symbol and self.trading_date:
            return (self.symbol.strip().upper(), self.trading_date.isoformat())
        return None

    @property
    def is_clean(self) -> bool:
        return not self.issues

    @property
    def prices(self) -> tuple[Decimal | None, ...]:
        return (self.open, self.high, self.low, self.close)


def _normalise_header(name: str) -> str:
    """Collapse a header to lowercase letters and digits for alias matching."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# Pre-computed so alias matching is a dict lookup, not a scan per row.
_NORMALISED_ALIASES: dict[str, tuple[str, ...]] = {
    canonical: tuple(_normalise_header(alias) for alias in names)
    for canonical, names in ALIASES.items()
}


def resolve_columns(header: Sequence[str]) -> dict[str, str | None]:
    """Map each canonical field to the actual header spelling, or ``None``.

    Contract
    --------
    in : the header row of a tabular source, in file order.
    out: ``{canonical_field: original_header_or_None}``.

    Business purpose: let one set of rules run against ``ticker`` from one feed
    and ``symbol`` from another without a per-source branch.
    """
    lookup: dict[str, str] = {}
    for name in header:
        if name is None:
            continue
        lookup.setdefault(_normalise_header(name), name)

    resolved: dict[str, str | None] = {}
    for canonical, aliases in _NORMALISED_ALIASES.items():
        resolved[canonical] = next(
            (lookup[alias] for alias in aliases if alias in lookup), None
        )
    return resolved


def missing_required_columns(columns: dict[str, str | None]) -> list[str]:
    """Return the required canonical fields that no header satisfied."""
    return [name for name in REQUIRED_FIELDS if columns.get(name) is None]


def quantize_price(value: Decimal | None) -> Decimal | None:
    """Round a price onto the model's 8-decimal grid, or pass ``None`` through.

    Every price is put on the same grid before comparison so two values that
    look equal are equal, and before storage so the database never truncates.
    """
    if value is None:
        return None
    return value.quantize(
        Decimal(1).scaleb(-PRICE_DECIMAL_PLACES), rounding=ROUND_HALF_UP
    )


def _text(value: Any) -> str | None:
    """Return trimmed text, or ``None`` for anything that is effectively absent."""
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    return text or None


def _decimal(value: Any) -> Decimal:
    """Coerce a price to ``Decimal``, raising ``ValueError`` on nonsense.

    Floats go through ``str()`` first: ``Decimal(0.1)`` carries the binary
    representation error, ``Decimal("0.1")`` does not.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("a boolean is not a price")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return Decimal(str(value).strip())


def _integer(value: Any) -> int:
    """Coerce a volume to ``int``, rejecting fractional or non-numeric input."""
    if isinstance(value, bool):
        raise TypeError("a boolean is not a volume")
    if isinstance(value, int):
        return value
    dec = Decimal(str(value).strip())
    if dec != dec.to_integral_value():
        raise ValueError("volume must be a whole number")
    return int(dec)


def _date(value: Any) -> date:
    """Coerce a trading date, accepting ISO and two unambiguous alternatives.

    Value order is dd/mm vs mm/dd ambiguous, so slashed forms other than ISO are
    rejected rather than guessed: a silently swapped day and month is worse than
    a rejected row.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for pattern in ("%Y/%m/%d", "%d-%b-%Y"):
        try:
            # The value is a calendar date, not an instant: it has no time or zone.
            return datetime.strptime(text, pattern).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {text!r}")


def _json_safe(value: Any) -> Any:
    """Reduce one value to something ``JSONField`` can store."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def sanitize_raw(raw: dict[str, Any], *, max_chars: int = 2000) -> dict[str, Any]:
    """Keep a JSON-safe, size-bounded copy of a source row.

    Business purpose: an operator can see exactly what was rejected. The cap
    stops one hostile row from filling the error table, and only primitive
    values are kept so nothing unserialisable reaches the database.
    """
    cleaned = {str(key)[:128]: _json_safe(value) for key, value in raw.items()}
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) > max_chars:
        return {"_truncated": encoded[:max_chars]}
    return cleaned


def normalize_row(
    *,
    row_number: int,
    raw: dict[str, Any],
    columns: dict[str, str | None],
    default_source: str = "",
) -> NormalizedRow:
    """Coerce one raw row into a :class:`NormalizedRow`.

    Contract
    --------
    in : the 1-based data-row number, the raw dict, the resolved column map, and
         a default for the optional ``source`` column.
    out: a typed row whose ``issues`` list is empty when every field was read.

    Business purpose: the single place that knows how a source value becomes a
    model value, so every adapter gets identical typing and identical wording of
    a structural failure. It never raises on bad data — a bad value becomes an
    issue and a ``None``, because the caller must record the failure rather than
    abort the whole file.
    """
    issues: list[Outcome] = []

    def field_value(canonical: str) -> Any:
        header = columns.get(canonical)
        return raw.get(header) if header else None

    symbol = _text(field_value("symbol"))
    if symbol is None:
        issues.append(
            Outcome(RuleName.MISSING_SYMBOL, "symbol", "symbol is blank or absent")
        )

    trading_date: date | None = None
    raw_date = field_value("trading_date")
    if _text(raw_date) is None:
        issues.append(
            Outcome(
                RuleName.MISSING_REQUIRED_FIELD,
                "trading_date",
                "trading_date is blank or absent",
            )
        )
    else:
        try:
            trading_date = _date(raw_date)
        except (TypeError, ValueError, InvalidOperation):
            issues.append(
                Outcome(
                    RuleName.INVALID_DATE,
                    "trading_date",
                    f"unparseable trading date {_text(raw_date)!r}",
                )
            )

    prices: dict[str, Decimal | None] = {}
    for name in PRICE_FIELDS:
        raw_price = field_value(name)
        if _text(raw_price) is None:
            issues.append(
                Outcome(RuleName.MISSING_REQUIRED_FIELD, name, f"{name} is blank or absent")
            )
            prices[name] = None
            continue
        try:
            prices[name] = _decimal(raw_price)
        except (TypeError, ValueError, InvalidOperation):
            issues.append(
                Outcome(
                    RuleName.MISSING_REQUIRED_FIELD,
                    name,
                    f"{name} is not a number: {_text(raw_price)!r}",
                )
            )
            prices[name] = None

    adjusted_close: Decimal | None = None
    raw_adjusted = field_value("adjusted_close")
    if _text(raw_adjusted) is not None:
        try:
            adjusted_close = _decimal(raw_adjusted)
        except (TypeError, ValueError, InvalidOperation):
            issues.append(
                Outcome(
                    RuleName.MISSING_REQUIRED_FIELD,
                    "adjusted_close",
                    f"adjusted_close is not a number: {_text(raw_adjusted)!r}",
                )
            )

    volume: int | None = None
    raw_volume = field_value("volume")
    if _text(raw_volume) is None:
        issues.append(
            Outcome(RuleName.MISSING_REQUIRED_FIELD, "volume", "volume is blank or absent")
        )
    else:
        try:
            volume = _integer(raw_volume)
        except (TypeError, ValueError, InvalidOperation):
            issues.append(
                Outcome(
                    RuleName.MISSING_REQUIRED_FIELD,
                    "volume",
                    f"volume is not a whole number: {_text(raw_volume)!r}",
                )
            )

    source = (_text(field_value("source")) or default_source)[:128]

    return NormalizedRow(
        row_number=row_number,
        symbol=symbol,
        trading_date=trading_date,
        open=prices["open"],
        high=prices["high"],
        low=prices["low"],
        close=prices["close"],
        adjusted_close=adjusted_close,
        volume=volume,
        source=source,
        raw=sanitize_raw(raw),
        issues=issues,
    )
