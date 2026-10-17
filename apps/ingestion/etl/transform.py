"""Pandas transformation: shape validated rows into storage-ready records.

Everything that reaches this module has already passed validation, so the work
here is mechanical rather than judgemental:

* order the batch by instrument then date, so a partial load is still coherent;
* drop an exact repeat of the same (instrument, date), keeping the last;
* put every price on the model's 8-decimal grid, so nothing is truncated on the
  way into ``DecimalField(18, 8)``;
* guarantee a non-empty ``source`` because the column is not nullable.

Pandas earns its place on the ordering and de-duplication: both are single
vectorised operations over a batch rather than a Python loop per row.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from .schemas import NormalizedRow, quantize_price

FRAME_COLUMNS = (
    "symbol",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "source",
    "row_number",
)


def to_frame(rows: Sequence[NormalizedRow]) -> pd.DataFrame:
    """Build a typed frame from accepted rows.

    Contract
    --------
    in : normalised rows (any row with issues is expected to have been filtered
         out already).
    out: a DataFrame with :data:`FRAME_COLUMNS`.

    Price columns keep ``object`` dtype holding ``Decimal`` rather than becoming
    ``float64``: binary floats cannot hold ``0.1`` exactly, and this is money.
    """
    records = [
        {
            "symbol": row.symbol.strip().upper(),
            "trading_date": row.trading_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "adjusted_close": row.adjusted_close,
            "volume": row.volume,
            "source": row.source,
            "row_number": row.row_number,
        }
        for row in rows
    ]
    frame = pd.DataFrame.from_records(records, columns=list(FRAME_COLUMNS))
    for column in ("open", "high", "low", "close", "adjusted_close"):
        frame[column] = frame[column].map(quantize_price)
    return frame


def prepare_for_load(frame: pd.DataFrame) -> pd.DataFrame:
    """Order, de-duplicate, and finalise a frame for persistence.

    Contract
    --------
    in : the frame from :func:`to_frame`.
    out: the same rows, sorted by (symbol, trading_date), de-duplicated on that
         key keeping the last occurrence, with ``volume`` as ``int`` and
         ``source`` filled if it was blank.

    Business purpose: makes the loaded result deterministic and idempotent
    regardless of the order rows arrived in.
    """
    if frame.empty:
        return frame

    prepared = frame.copy()
    prepared["source"] = prepared["source"].fillna("").replace("", "unknown")
    # Plain Python ints, not numpy int64: the ORM should receive values it can
    # store without a platform-dependent numpy type leaking through.
    prepared["volume"] = prepared["volume"].astype("int64").astype(object)
    prepared = prepared.sort_values(["symbol", "trading_date"], kind="stable")
    prepared = prepared.drop_duplicates(subset=["symbol", "trading_date"], keep="last")
    return prepared.reset_index(drop=True)


def frame_to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Turn a prepared frame back into plain dicts for the ORM layer.

    The ORM is handed primitives, not a DataFrame, so persistence never depends
    on pandas being installed at the call site.
    """
    return frame.to_dict(orient="records")
