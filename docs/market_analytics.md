# Market analytics engine

The engine turns rows in `PriceRecord` into the measures a portfolio report is
made of: returns, moving averages, volatility, drawdown, price extremes, volume
summaries, period change, and cross-sectional rankings. It reads one instrument's
series with one query, computes in exact decimal arithmetic, and exposes a
Pandas implementation of every metric as the reference the SQL path is checked
against.

It reports measurements. It does not rank instruments as *opportunities*, score
them, or suggest an action, and nothing in this document should be read as
investment advice.

Implementation proceeds one metric at a time, each with its hand-checked sample,
its edge-case policy, and its trade-offs written down before the next one starts.

## Status

| # | Metric | State |
|---|---|---|
| 1 | Daily simple returns | implemented |
| 2 | Cumulative returns | next |
| 3 | Moving averages (7/30/90) | planned |
| 4 | Rolling volatility | planned |
| 5 | Maximum drawdown | planned |
| 6 | Highest and lowest prices | planned |
| 7 | Trading-volume summaries | planned |
| 8 | Percentage change over a selected period | planned |
| 9 | Rankings by return and trading volume | planned |

## Where the code is

| Module | Responsibility | Depends on |
|---|---|---|
| `apps/analytics/series.py` | The read boundary. Loads one instrument's prices: one query, only the requested columns, plus the warm-up rows a window needs. Owns the definition of a trading day. | Django ORM, `PriceRecord`, `TradingCalendar` |
| `apps/analytics/metrics.py` | Pure arithmetic. No Django, no I/O, no queryset: a metric is a function from sequences of numbers to numbers, so it can be checked against a hand-computed series without a database. | standard library, Pandas (lazily) |
| `apps/analytics/services.py` | The entry points the API calls. Chooses a loader, a range, and a calculation backend, and returns one result. Also holds the stored-snapshot reads. | `series`, `metrics`, `models` |

The split exists so that "what was read" and "what was computed" are auditable
separately: a wrong number is either the wrong rows or the wrong arithmetic, and
the two live in different files with different tests.

## Conventions

These are fixed once, in `metrics.py` and `series.py`, and every metric inherits
them.

**Money is `Decimal`.** Prices are `DECIMAL(18,8)` in MySQL and stay exact in
Python. `(102 - 100) / 100` is `0.02`, not `0.020000000000000004`.

**Ratios are `Decimal`, quantized to 8 places** (`ROUND_HALF_UP`, ties away from
zero). Eight places is the width of `AnalyticSnapshot.value`
(`DECIMAL(18,8)`), so a return computed on demand and a return read back from a
stored snapshot are the same number at the same precision — one precision in the
system, not two. It is a ten-thousandth of a basis point, far below any market's
tick. Rounding away from zero is symmetric, so a gain and an equal loss round to
the same magnitude, and it is what a reader of a reported percentage expects
(Python's default banker's rounding would take `0.000000025` down to
`0.00000002`; see `test_rounding_is_half_up_away_from_zero_not_bankers`).

**Undefined is `None`, never `0` and never `inf`.** "No value" is a real answer
here — the first day of a series genuinely has no return — and `0` would be a
claim that the price did not move. It is represented as JSON `null` once an
endpoint renders it, and every result counts its undefined points so a caller can
see a hole without reading every row.

**Inputs are normalised through `Decimal(...)`.** What a driver hands back for a
`DECIMAL(18,8)` column is not one type: MySQL returns `Decimal('100.00000000')`,
SQLite returns the same number with different trailing zeros, and a
window-function annotation skips the field's converter on both. Normalising once
per metric is what makes the arithmetic backend-independent. Only ratios are
quantized; money is passed through at whatever precision the store holds,
because rendering a price is the serializer's decision, not a metric's.

### Calendar days versus trading days

Two different questions, two different tables.

* **A trading day is an observation** — a row in `PriceRecord`. Windows count
  observations, not calendar days. A 7-day moving average is the mean of the last
  seven *rows*, so a weekend, a holiday, or a gap in the ingest cannot silently
  end up inside the window as a day with no price. This also makes every window
  gap-tolerant by construction: a gap is simply not an observation.
* **`TradingCalendar` says which dates the exchange was actually open.** It
  distinguishes "no trades happened" from "no data was ingested".
  `missing_trading_dates()` reports the dates that are sessions but have no price
  for the instrument — the audit view. A date in neither table is a market
  holiday and is not reported at all.

Date *filters* (`start_date`, `end_date`) are calendar bounds that select which
observations to return; they never invent an observation. A range that spans a
holiday returns the sessions in it, not a padded series of one row per calendar
day. `test_a_range_bounded_by_calendar_dates_returns_only_trading_days` pins
this down.

### On demand or stored

| | Computed on demand | Stored in `AnalyticSnapshot` |
|---|---|---|
| What | The whole per-instrument series: returns, cumulative returns, moving averages, volatility, drawdown, extremes, volume summaries, period change | The cross-sectional reads and the audit trail |
| Why | O(n) over a series that is already in memory, and the answer depends on the requested range, so caching it by range would multiply the keys while saving microseconds | A ranking needs one comparable number per instrument (which the snapshot's `(instrument, as_of_date, metric_type, lookback_days)` grain already models), and a stored value is the answer to "what did we say on 12 March", which a recomputation cannot reproduce |
| Cost | One query per request, bounded by the date range | A write path, and recalculation after new prices |

Snapshots are written by a recalculation job, never by a request, which is why
the analytics API is read-only (`ListAPIView`).

## Missing prices, zero denominators, duplicate dates, insufficient history

| Situation | Behaviour | Why |
|---|---|---|
| **Missing price** (a session with no row) | Not an observation. Windows close over the gap and span it. Returned rows are the observations only. | A gap is an absence of data, not a price of zero; inserting a zero would invent a 100% loss. `missing_trading_dates()` reports gaps separately for anyone auditing the data. |
| **Zero previous close** | The return is `None`. No exception, no `inf`. | The table permits a zero close (it constrains `high >= low` and `volume >= 0`, not the price), so this is reachable production data. Division by zero has no value, and reporting `0` or a very large number would both be wrong. |
| **Duplicate dates** | Rejected. `PriceSeries.assert_one_row_per_date()` raises rather than deduplicating. In the table it cannot happen: `uniq_price_instrument_trading_date` is unique, and re-ingestion upserts on it (the corrected price replaces the old row). | Deduplication would have to choose which of two contradictory closes for one day is real. Either choice fabricates a price; fixing the source is the only correct response. |
| **Insufficient history** | `None`, never a partial window. Nine observations cannot produce a ten-day measure. | A "7-day average" of three days is a different statistic wearing the same name. A short series returns `None` for the early rows and a value as soon as the window is full. |
| **No history at all** | An empty series, one query, no error. | A newly listed instrument is a normal state, not a failure. |
| **Series shorter than the requested lookback** | The loader still looks back as far as there is anything to look at, so no requested row is ever dropped. | See `_warm_up_boundary`. The alternative — comparing a date against the NULL that a shorter history produces — silently returns an empty response. |

## Metric 1 — daily simple returns

### Business meaning

How much the price moved from one session to the next, as a fraction of where it
started. It is the atom of every performance number: cumulative return is the
product of these, volatility is their standard deviation, and a drawdown is what
compounding them produces. Reported for one instrument over a date range, it
answers "on which sessions did this move, and by how much".

### Formula

```
return(t) = (close(t) - close(t-1)) / close(t-1)
```

`t-1` is the **previous observation**, not the previous calendar day: the session
before, even if it was a week earlier. The return is undefined, and reported as
`null`, when there is no previous close (the first row of a series) or when the
previous close is zero.

### Intermediate calculations

The sample is the standard five-weekday series from
`apps/marketdata/tests/helpers.py`, so these numbers can be checked against the
exercise tests. Rounded to the engine's eight places.

**AAPL — closes 100, 102, 101, 103, 104**

| Trading day | Previous close | Close | Calculation | Return |
|---|---|---|---|---|
| 2024-03-11 | — | 100 | no previous close | `null` |
| 2024-03-12 | 100 | 102 | `(102 - 100) / 100 = 2 / 100` | `0.02000000` |
| 2024-03-13 | 102 | 101 | `(101 - 102) / 102 = -1 / 102 = -0.009803921568…` | `-0.00980392` |
| 2024-03-14 | 101 | 103 | `(103 - 101) / 101 = 2 / 101 = 0.019801980198…` | `0.01980198` |
| 2024-03-15 | 103 | 104 | `(104 - 103) / 103 = 1 / 103 = 0.009708737864…` | `0.00970874` |

**MSFT — closes 200, 190, 195, 205, 210** (a falling series, so the sign handling
and the `5/190` rounding case are covered)

| Trading day | Previous close | Close | Calculation | Return |
|---|---|---|---|---|
| 2024-03-11 | — | 200 | no previous close | `null` |
| 2024-03-12 | 200 | 190 | `-10 / 200` | `-0.05000000` |
| 2024-03-13 | 190 | 195 | `5 / 190 = 0.026315789473…` | `0.02631579` |
| 2024-03-14 | 195 | 205 | `10 / 195 = 0.051282051282…` | `0.05128205` |
| 2024-03-15 | 205 | 210 | `5 / 205 = 0.024390243902…` | `0.02439024` |

**GOOG — closes 50, 51, 52, 53, 54**

| Trading day | Previous close | Close | Calculation | Return |
|---|---|---|---|---|
| 2024-03-11 | — | 50 | no previous close | `null` |
| 2024-03-12 | 50 | 51 | `1 / 50` | `0.02000000` |
| 2024-03-13 | 51 | 52 | `1 / 51 = 0.019607843137…` | `0.01960784` |
| 2024-03-14 | 52 | 53 | `1 / 52 = 0.019230769230…` | `0.01923077` |
| 2024-03-15 | 53 | 54 | `1 / 53 = 0.018867924528…` | `0.01886792` |

Each value is exposed in the answer alongside its inputs
(`ReturnPoint.previous_close`, `.close`, `.value`), so a response is its own
working. Those three literal tables are asserted verbatim in
`apps/analytics/tests/test_daily_returns.py`.

Two rounding cases worth noticing, because they show the eight places are doing
work rather than being decorative: `-1/102 = -0.009803921568…` truncates to
`-0.00980392`, because the ninth place is a `1` and the sign is carried rather
than rounded; and `1/103 = 0.009708737864…` carries, because the ninth place is a
`7`, giving `0.00970874`. An exactly-tied value such as `2.5e-8` rounds away from
zero to `0.00000003` in both directions, asserted as a unit test.

### The window: one row of warm-up

The subtle bug this metric can hide is in the *reading*, not the arithmetic. If
the query returns only the requested range, the first row has no predecessor,
because a `WHERE` clause is evaluated before a window function and a
`LAG(close)` computed over a filtered set sees nothing to lag to. The response
then opens with a `null` the database could have answered.

So the loader reads `pad = 1`: the one observation immediately *before*
`start_date`, in the same query. Requesting 2024-03-12 to 2024-03-15 returns five
rows — 03-11 as warm-up, then the four requested days — and the 03-12 return is
`(102 - 100) / 100 = 0.02`, computed from a row the caller never sees.
`PriceSeries.padding` says how many leading rows were warm-up, and
`in_range()` drops them, so the warm-up is accounted for rather than leaked into
the response.

The warm-up boundary is found with an inline, uncorrelated subquery that selects
the `pad`-th previous date, so it costs no extra round trip:

```sql
WHERE trading_date <= :end
  AND trading_date >= COALESCE(
        (SELECT trading_date FROM marketdata_pricerecord
          WHERE instrument_id = :id AND trading_date < :start
          ORDER BY trading_date DESC LIMIT 1),
        (SELECT trading_date FROM marketdata_pricerecord
          WHERE instrument_id = :id ORDER BY trading_date ASC LIMIT 1))
ORDER BY trading_date
```

The `COALESCE` is not decoration. A history shorter than `pad` makes the offset
subquery return NULL, and `trading_date >= NULL` is *unknown*, which `WHERE`
reads as false — every requested row would vanish. Falling back to the
instrument's earliest date turns that case into "look back as far as there is
anything to look back at", which is the intent. It cannot scan unboundedly: the
fallback is only reachable when there is less than `pad` rows of history.

### Three implementations, one answer

| Backend | Previous close comes from | Use it when |
|---|---|---|
| `sql` (default) | `LAG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date)`, computed by MySQL | The canonical path. The window stays in the database, which is where it pays off as the series or the read shape grows. |
| `python` | The previous item of a list already in memory | Simpler, and marginally the fastest at this size. |
| `pandas` | `Series.shift(1)`, vectorised | Bulk and offline work: one frame, many instruments, many columns at once. Also the reference implementation. |

All three produce the same points, and the tests assert it row for row on the
sample series and on real MySQL data. Measured by `scripts/verify_analytics.py`
on the benchmark instrument (500 rows of history, a 31-observation window, warm,
median of 30 calls):

| Backend | Median |
|---|---|
| `python` | 1.16 ms |
| `sql` | 1.35 ms |
| `pandas` | 1.48 ms |

The honest reading of that table is that **for one small instrument the SQL
window is not the fastest of the three**, and it is worth saying why rather than
presenting the canonical path as the fast one. All three pay the same single
query and the same warm-up boundary; `sql` additionally has MySQL compute a
`LAG` window and return a column for it (`previous_close`), and it saves Python
no work, because the shift is trivial and the ratio is computed in Python
anyway. The reason `sql` is still the default is that it is the only one of the
three that does not change shape when the question changes shape: ranking a
cross-section, or filtering on the windowed value, keeps the window inside the
database, where the alternative is shipping every row to Python to compute a
shift.

The ratio is computed in Python in every backend, including `sql`. Doing it in
SQL would need a `CAST(... AS DECIMAL)` for the division to behave identically on
MySQL and SQLite (SQLite divides integers as integers — the trap the exercise
tests document), and the round trip through a float would lose the exactness that
`Decimal` is there for. The window belongs in the database; the ratio belongs
where the precision is.

Both undefined cases agree across backends too: Pandas yields `inf` for a zero
denominator and `nan` for `0/0`, and both are mapped to `None`. NumPy's
divide-by-zero warning is suppressed because a zero previous close is an expected
input, not an anomaly.

`pandas` does not use `Series.pct_change()`, which is the shorthand for this
formula. `pct_change` computes `close / previous - 1`, equal in exact arithmetic
but different in the last bit of a float64, and its `fill_method` default changed
between pandas 2.1 and 2.2. Writing the shift and the division out makes the
reference literal, and a test asserts that `pct_change` still agrees once it is
quantized to the engine's eight places.

Pandas and NumPy are imported *inside* the function that needs them. They cost
hundreds of milliseconds to import, the request path does not need them, and a
worker that only answers from the SQL backend should not pay for them at start-up
— the *first* `pandas` call in a fresh process measured 284 ms against the same
data, and the 1.48 ms above is what the call itself costs once the import has
been paid for once.

## How computation changes as the dataset grows

| Growth | Effect | Why |
|---|---|---|
| More rows per instrument (longer history) | The loader's range scan is bounded by the requested range, not the instrument's life: `pad + observations in range`. A 20-year history costs the same per request as a 1-year one. | The bounds are pushed into `WHERE`, which uses `uniq_price_instrument_trading_date`. Warm-up padding adds `pad` rows — a fixed 1 for this metric, 89 for a 90-day average, independent of history length. |
| More instruments | Per-request cost is unchanged: one instrument, one query. Ranking across instruments is the case that changes the answer — it must not become one query per instrument. | Pending: the rankings metric will read the cross-section in one frame and group by instrument, which is what the `pandas` backend is for. |
| More metrics on the same series | Free if they share the loader call. Each additional metric over the same rows adds arithmetic, not queries. | The loader is the only thing that reads, and it returns every column a metric asked for. |
| More concurrent readers | Linear in requests, with no shared mutable state in the engine. | The engine writes nothing. The API's response cache (see [api_optimisation.md](api_optimisation.md)) covers the snapshot reads, not the computed series, which depends on the requested range. |
| A very long single series read whole | Becomes memory-bound, not query-bound, and is not the shape this engine is designed for: a range is what bounds the work. | If a full-history export is needed, that is an offline job over a DataFrame, not a request. |

## Tests

`apps/analytics/tests/test_series.py` — the read boundary, 22 tests:

* the whole series in exactly one query, in date order (query count pinned);
* only the requested columns are selected — `source`, `ingestion_job_id`,
  `created_at` and `adjusted_close` are asserted absent from the SQL, and the
  instrument table is asserted not to be joined;
* warm-up: one row before the range is read, `pad` larger than the available
  history still returns every requested row, `pad` without a `start_date` is a
  no-op, and the window does not lag across instruments;
* `PriceSeries` invariants: repeated or out-of-order dates raise, `in_range()`
  drops warm-up from every column, a missing column names what was loaded;
* the database refuses a second row for one instrument and day.

`apps/analytics/tests/test_daily_returns.py` — the metric, 28 tests:

* the formula against the three hand-computed tables above;
* both undefined cases: no previous close, zero previous close (including
  `0/0`), and that a zero close does not raise;
* rounding: half-up away from zero, symmetric for a loss, and reached through
  the formula as well as through the quantizer;
* the three backends agree point for point, on the sample series and on a zero
  price;
* the service: the input triple is exposed, the warm-up row makes the first
  requested date answerable (and, read without padding, the same date is `null`
  — the contrast is the test), the range bounds the dates, the whole request is
  one query, and an unknown backend is rejected by name;
* gaps: returns span a five-day hole, and a calendar-bounded range returns only
  trading days.

## Deliberately not done yet

* Metrics 2–9, each with its own sample, edge-case policy and trade-off note.
* **No HTTP endpoint yet.** The service layer is where a metric is defined, and
  its shape is still settling (the `points` list, the `padding` count, the
  backend choice). A serializer written now would be rewritten at metric 3. The
  endpoint, its filters, and the query-parameter contract arrive once the metric
  set is stable.
* **No snapshot recalculation job yet.** `AnalyticSnapshot` already models the
  grain and the CLI/service to write it is designed but not built; it is needed
  by the cross-sectional rankings (metric 9), not by a per-instrument series.
* **No caching of computed series.** The range is part of the key, so a cache
  would multiply keys to save milliseconds over rows that are already a single
  indexed scan. The existing response cache covers the stored snapshot list.

## Reproducing the verification

```powershell
# Unit and integration tests (SQLite in memory, cache off)
./venv/Scripts/python.exe -m pytest apps/analytics -q -p no:cacheprovider

# Full suite
./venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

The MySQL checks in this document run read-only against the development database
and the benchmark instrument seeded by `scripts/bench_api.py` — 500 rows of
history, of which the request asks for a 31-observation window. They are
`scripts/verify_analytics.py`, which prints the plan below and asserts the
padding, backend agreement and single-query properties:

```powershell
./venv/Scripts/python.exe scripts/verify_analytics.py --rounds 30
```

`EXPLAIN FORMAT=TREE` for that request:

```
-> Filter: (marketdata_pricerecord.trading_date >= <cache>(coalesce((select #2),(select #3))))  (cost=14.7 rows=32)
    -> Index range scan on marketdata_pricerecord using uniq_price_instrument_trading_date
       over (instrument_id = 13 AND '2024-01-31' <= trading_date <= '2024-03-14')
    -> Select #2 (subquery in condition; run only once)
        -> Limit: 1 row(s)
            -> Filter: ((u0.instrument_id = 13) and (u0.trading_date < DATE'2024-02-01'))
                -> Covering index range scan on U0 using uniq_price_instrument_trading_date
                   over (instrument_id = 13 AND trading_date < '2024-02-01') (reverse)
    -> Select #3 (subquery in condition; run only once)
        -> Limit: 1 row(s)
            -> Covering index lookup on U0 using uniq_price_instrument_trading_date (instrument_id = 13)
```

What the plan shows, and why it was worth checking rather than assuming:

* both scalar subqueries are **"run only once"**, not once per candidate row, and
  the value is cached — the concern that a scalar subquery in `WHERE` is
  re-evaluated per row does not apply here, because the subqueries are
  uncorrelated;
* the access is an **index range scan** on
  `uniq_price_instrument_trading_date`, from the *resolved* warm-up date
  (`2024-01-31`, one session before the requested `2024-02-01`) — the padding is
  part of the range, not an afterthought applied to the result;
* the padding was consumed as intended: `pad = 1` returned 32 rows with
  `padding = 1`; `pad = 89` returned 120 rows with `padding = 89`.
