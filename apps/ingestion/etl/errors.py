"""Exceptions the ETL raises, and the vocabulary of error codes it reports.

Three failure modes are distinguished on purpose:

* :class:`UnsafeFileError` — rejected before a byte is parsed (bad extension,
  oversize, path traversal). The upload never reached the parser.
* :class:`SourceFormatError` — the bytes are not the declared format (CSV with
  no header, truncated JSON, an XLSX with no sheets).
* :class:`SchemaError` — the format is fine but a required column is absent, so
  no row can be interpreted. Carries the column names so the pipeline records
  one batch-level finding per column rather than one per row.
"""

from enum import StrEnum


class IngestionError(Exception):
    """Base class for every recoverable ETL failure."""


class UnsafeFileError(IngestionError):
    """The file failed a safety or size check and was never read."""


class SourceFormatError(IngestionError):
    """The payload is not the format it claims to be."""


class SchemaError(IngestionError):
    """A required column is absent, so the batch cannot be interpreted."""

    def __init__(self, missing_columns):
        self.missing_columns = list(missing_columns)
        joined = ", ".join(self.missing_columns)
        super().__init__(f"missing required column(s): {joined}")


class RuleName(StrEnum):
    """Row-level data-quality rules.

    These strings are the ``error_code`` values stored on a validation error and
    the ``rule_name`` values on a rule summary. They are deliberately stable:
    ``scripts/generate_dummy_market_data.py`` hard-codes the same names in its
    expected-results contract, and tests assert against them.
    """

    MISSING_SYMBOL = "missing_symbol"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_DATE = "invalid_date"
    FUTURE_DATE = "future_date"
    NON_POSITIVE_PRICE = "non_positive_price"
    HIGH_BELOW_LOW = "high_below_low"
    OPEN_OUTSIDE_RANGE = "open_outside_range"
    CLOSE_OUTSIDE_RANGE = "close_outside_range"
    NEGATIVE_VOLUME = "negative_volume"
    DUPLICATE_RECORD = "duplicate_record"
    UNKNOWN_INSTRUMENT = "unknown_instrument"
    MISSING_TRADING_DATE = "missing_trading_date"


class BatchError(StrEnum):
    """Job-level failures that have no single source row.

    Stored in the same ``error_code`` column, with ``row_number = 0``, so a
    caller reading a job's errors sees a complete picture.
    """

    MISSING_COLUMN = "missing_column"
    MALFORMED_SOURCE = "malformed_source"
    UNSAFE_FILE = "unsafe_file"
    EMPTY_SOURCE = "empty_source"
    ABORTED_ON_INVALID = "aborted_on_invalid"


# Row rules, in the order the pipeline reports them. Batch errors are excluded
# because they are not evaluated per row and so have no pass/fail count.
ROW_RULES = tuple(rule.value for rule in RuleName)
