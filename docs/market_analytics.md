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
| 2 | Cumulative returns | implemented |
| 3 | Moving averages (7/30/90) | implemented — three named series, one read |
| 4 | Rolling volatility | implemented |
| 5 | Maximum drawdown | implemented |
| 6 | Highest and lowest prices | implemented |
| 7 | Trading-volume summaries | implemented |
| 8 | Percentage change over a selected period | implemented |
| 9 | Rankings by return and trading volume | implemented — cross-sectional, one query for the universe |

The engine is served over HTTP by two read-only endpoints — one metric of one
instrument, and the cross-sectional ranking — described under [HTTP
surface](#http-surface) below. The routes are `apps/analytics/urls.py`; the engine
itself has no HTTP dependency, and each metric's own section is still the definition
of the measure.

## Where the code is

| Module | Responsibility | Depends on |
|---|---|---|
| `apps/analytics/series.py` | The read boundary. Loads one instrument's prices — one query, only the requested columns, plus the warm-up rows a window needs — and, for the one cross-sectional metric, many instruments' prices in one query. Owns the definition of a trading day. | Django ORM, `PriceRecord`, `TradingCalendar` |
| `apps/analytics/metrics.py` | Pure arithmetic. No Django, no I/O, no queryset: a metric is a function from sequences of numbers to numbers, so it can be checked against a hand-computed series without a database. Also the ranking arithmetic, which needs no prices at all. | standard library, Pandas (lazily) |
| `apps/analytics/services.py` | The entry points the API calls. Chooses a loader, a range, and a calculation backend, and returns one result. Also holds the stored-snapshot reads. | `series`, `metrics`, `models` |
| `apps/analytics/views.py`, `apps/analytics/serializers.py` | The HTTP surface. Parses a request into exactly one service call and names the fields of the frozen results; no arithmetic, and no query construction. | `services`, DRF |

The split exists so that "what was read" and "what was computed" are auditable
separately: a wrong number is either the wrong rows or the wrong arithmetic, and
the two live in different files with different tests. The surface adds no third
place for a number to be wrong: it renders what the engine returned and nothing
else.

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

**Counts are `Decimal`, quantized to whole numbers.** Trading volume is a share
count, not a price and not a ratio: the column is a `BIGINT`, and a count is not
divisible in a report. The single-session and summed volumes are whole by
construction, so for them the quantizer is a normalisation — a driver returns a
`BIGINT` as an `int` and a `SUM() OVER ()` annotation as whatever the engine felt
like, and one normalisation makes both render alike. The one place it rounds is the
**mean** volume, which is a statistic *about* counts rather than a count; the exact
figures it came from (`total_volume` and `observations`) are reported beside it so
nothing is lost, and the summary's classification uses the exact quotient rather
than the rounded one. Three quantizers for three kinds of quantity, named so that a
schema change to one does not silently move the others.

**Undefined is `None`, never `0` and never `inf`.** "No value" is a real answer
here — the first day of a series genuinely has no return — and `0` would be a
claim that the price did not move. It is represented as JSON `null` once an
endpoint renders it, and every result counts its undefined points so a caller can
see a hole without reading every row. A metric whose points *cannot* be missing a
value — the price extremes, where every row has a close — still answers the count,
with `0` and a reason rather than by raising.

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

### Window units: observations, and returns

A window is a count, and *what it counts* differs between the metrics that
measure a level and the ones that measure a rate. The engine keeps each metric's
standard unit and states it, rather than forcing one convention on all of them
and leaving the off-by-one to be discovered:

| Metric | `window` counts | Needs | A "20" window means |
|---|---|---|---|
| Moving average | observations (prices) | `window` closes | the mean of 20 closes |
| Rolling volatility | returns | `window + 1` closes | the deviation of 20 returns |
| Cumulative returns | — (anchored at the range start) | 1 close | — |
| Maximum drawdown | — (running maximum from the range start) | 1 close | — |
| Price extremes | — (running extremes from the range start) | 1 close | — |
| Volume summaries | — (running total from the range start) | 1 volume | — |
| Period change | — (two chosen observations) | 2 closes | — |
| Ranking | observations, **per instrument** (`lookback`) | `lookback` closes (or volumes) | the newest 20 sessions of *each* instrument |

Both columns are right. "20-day volatility" really is the standard deviation of
twenty daily returns, and "20-day average" really is the mean of twenty closes;
they differ by one observation, and every response carries `sample_size` (or the
`window` it was asked for) so the difference is visible rather than assumed.

All windows are **trailing and right-aligned** — a value dated `t` uses no price
after `t` — and all of them count **observations**, not calendar days, so a
holiday does not shorten a window and a gap does not become one of its members.
`pad` is `window - 1` for a moving average (for the named series, the longest
window asked for), `window` for a volatility (which needs one more close than
return), and `1` for a daily return. It is `0` for cumulative returns, maximum
drawdown, price extremes, volume summaries and period change — the five metrics
whose anchor is inside the range, or narrower than it: see their sections.

The ranking is the one that cannot be expressed as a `pad` at all, and that is
worth stating rather than hiding in a `0`: its window is per *instrument*, while a
read is per *range*. There is no row before `start_date` that would give every
instrument the same number of sessions, so the slice is taken after the read, off
the end of each series, and the metric counts the observations that survived — see
Metric 9.

### Backends

Every metric exposes the same interchangeable backends, and every backend returns
the same points, asserted row for row:

| Metric | `sql` | `python` | `pandas` |
|---|---|---|---|
| Daily returns | `LAG(close)` | list shift | `Series.shift(1)` |
| Moving averages (7/30/90) | three `AVG(close) OVER (...)` frames in one query | three running sums | three `Series.rolling` |
| Rolling volatility | returns from `LAG`, deviation in Python | returns from a shift, deviation in Python | `Series.rolling().std()` |
| Cumulative returns | — | direct ratio | direct ratio, vectorised |
| Maximum drawdown | running maximum from `MAX(close) OVER (...)`, ratio in Python | running maximum scan | `Series.cummax()` |
| Price extremes | running maximum and minimum from `MAX(close)`/`MIN(close) OVER (...)` | running scan in both directions | `Series.cummax()`/`cummin()` |
| Volume summaries | running total from `SUM(volume) OVER (...)`, ratio in Python | running sum | `Series.cumsum()` (`int64`) |
| Period change | — | linear scan for the two bounds | `Index.searchsorted` |
| Rankings (return, volume) | — | one range read, sliced per instrument | one frame, `groupby().tail()` |

Four of the nine have no `sql` member. Two of them share a reason: **a metric
whose window must be computed from *windowed values* cannot be expressed in one
SQL query**, because a window function may not appear inside another window
function's arguments. Cumulative returns need a *product* (no dialect has a
windowed product), and rolling volatility needs the deviation of the *returns*,
which are themselves `LAG`s. MySQL states the second one outright:

```
OperationalError: (3593, "You cannot use the window function 'lag' in this context.'")
```

Period change has no `sql` member for a third and different reason: it has no window
at all. Its "window" is the *choice of two rows* — the first observation at or after
the period's start, the last at or before its end — and the engine's read boundary
returns a series, not a pair. The SQL form would be two `ORDER BY ... LIMIT 1`
subqueries, a different query shape rather than a frame over the rows the loader
already has, so it is left out rather than smuggled in as a second read path for a
subtraction. The verification script *runs* that query to show the two agree, and
`EXPLAIN` shows it is two index lookups; reading the period's rows instead costs one
indexed range scan and buys `observations` — how many sessions the period held —
which the two-endpoint query could not report.

The ranking has no `sql` member for a fourth reason, and it is the most interesting
of the four because the SQL is *so* natural that it needs a measurement to argue
against. Its window — the newest *n* observations **of each** instrument — has an
obvious query:

```sql
SELECT instrument_id, trading_date, close FROM (
    SELECT instrument_id, trading_date, close,
           ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY trading_date DESC) AS rn
    FROM marketdata_pricerecord WHERE trading_date BETWEEN '…' AND '…'
) AS ranked WHERE rn <= 31
```

That is a legal, single-level, *partitioned* window, and Django can express it (a
`Window(RowNumber())` filtered on the annotation). It is still declined, because it
is slower in both shapes, and `scripts/verify_analytics.py` measures both rather
than asserting it:

| Read | Rows returned | Median (MySQL, benchmark universe) |
|---|---|---|
| Bounded range read (used) | 3,100 | 16–21 ms |
| Bounded row-number read | 3,100 | 21–22 ms |
| Whole-history range read | 50,000 | 74–83 ms |
| Whole-history row-number read | 3,100 | 115–133 ms |

With a date bound the two read the same rows and the window is pure overhead; **without**
one it does ship sixteen times fewer rows and is *still* around 60% slower, for the same
reason in both cases — `EXPLAIN` shows the window is `Materialize`d and sorted per
partition before `rn <= 31` can filter it, so the database pays for every row it was
trying not to return. `sql` would be the slow path here, and a default that is slower is
not a default.

The other SQL shape — pushing the *measure* down, one row per instrument — is a
different read shape rather than a window over the rows the loader returns, which is
the same objection the period change raised: a `GROUP BY instrument_id` with
`SUM(volume)` serves the volume measure only, while a return needs each instrument's
first *and* last close, which is the two-endpoint read again, and it would have to be
a pivot (`MAX(CASE WHEN …)` over two row numbers) that the ORM cannot express without
raw SQL. Two backends that read the same rows and differ in the arithmetic are what
the engine promises; a third that reads differently and computes the same thing is
not.

Maximum drawdown, price extremes and volume summaries are the test of that rule
rather than an exception to it: each of their windows is over a column the row
already carries — one frame, or two of them — so the database can carry the whole
scan, and only the arithmetic on top of it is computed in Python. Asking for two
levels is what fails, which is why the verification script writes the drawdown as a
derived table to get a database-computed cross-check:

```
OperationalError: (3593, "You cannot use the window function 'max' in this context.'")
```

So the engine does not offer a SQL member it cannot honestly implement. The four
without one are computed in Python over rows a single query already returned — which
is why the choice costs nothing in queries, only in where the arithmetic runs. The
engine's canonical path stays inside the database wherever a window is a real window
*and* is not slower than not pushing it.

### On demand or stored

| | Computed on demand | Stored in `AnalyticSnapshot` |
|---|---|---|
| What | The whole per-instrument series — returns, cumulative returns, moving averages, volatility, drawdown, extremes, volume summaries, period change — and the cross-sectional ranking built from them | The audit trail |
| Why | O(n) over rows that are already in memory, and the answer depends on the requested range, so caching it by range would multiply the keys while saving microseconds | A stored value is the answer to "what did we say on 12 March", which a recomputation cannot reproduce |
| Cost | One query per request, bounded by the date range — except a ranking with no date bounds, which reads the histories it is ranking (see Metric 9) | A write path, and recalculation after new prices |

A ranking's per-instrument numbers are the one place where the snapshot grain is
already the right answer rather than a future one: the return over `n` sessions is
`metric_type="return"` with `lookback_days=n`, so "rank the universe by the return
over 21 sessions as of 14 March" is one indexed read of one row per instrument at
one `as_of_date`, instead of reading every instrument's prices. The other measures
would need new `MetricType` choices — the same gap metrics 6, 7 and 8 have — which is
part of what the recalculation job in [Deliberately not done
yet](#deliberately-not-done-yet) would settle.

Snapshots are written by a recalculation job, never by a request, which is why
the analytics API is read-only (`ListAPIView`).

## Missing prices, zero denominators, duplicate dates, insufficient history

| Situation | Behaviour | Why |
|---|---|---|
| **Missing price** (a session with no row) | Not an observation. Windows close over the gap and span it. Returned rows are the observations only. | A gap is an absence of data, not a price of zero; inserting a zero would invent a 100% loss. `missing_trading_dates()` reports gaps separately for anyone auditing the data. |
| **Zero previous close** | The return is `None`. No exception, no `inf`. | The table permits a zero close (it constrains `high >= low` and `volume >= 0`, not the price), so this is reachable production data. Division by zero has no value, and reporting `0` or a very large number would both be wrong. |
| **Zero mean volume** (nothing has traded yet in the range) | `relative_volume` is `None` while the running total is `0`; the running total and the session volume are still exact and still reported. | `volume / 0` is not a ratio. Same rule as a zero previous close: report no value rather than `0` (typical of nothing?) or `inf`. |
| **Duplicate dates** | Rejected. `PriceSeries.assert_one_row_per_date()` raises rather than deduplicating. In the table it cannot happen: `uniq_price_instrument_trading_date` is unique, and re-ingestion upserts on it (the corrected price replaces the old row). | Deduplication would have to choose which of two contradictory closes for one day is real. Either choice fabricates a price; fixing the source is the only correct response. |
| **Insufficient history** | `None`, never a partial window. Nine observations cannot produce a ten-day measure. | A "7-day average" of three days is a different statistic wearing the same name. A short series returns `None` for the early rows and a value as soon as the window is full. |
| **A ranking window shorter than its `lookback`** | That instrument's measure is `None` and it is listed **last and unranked**, with its `observations` and the two dates it did have. The ranks of the measured instruments are unaffected. | The same rule as above: a twenty-session total computed over three sessions is a different statistic, and mixing it into one ranking compares unlike things. The row still appears, because a universe that quietly shrank would be a lie about how many instruments were compared. |
| **An instrument with no prices in the ranking's window** | It is not in the universe at all. | A ranking is over prices: an instrument with no price in the window has no measure and no place in it. Counting reference data instead would make `universe` a fact about the instrument table rather than about the data being compared — so a caller who passed `instrument_ids` can compare that list with `universe` to see who dropped out. |
| **A period with no observation in it** | `None` throughout — no base, no end, `observations` of `0` — and the result counts it as undefined. Not an error. | The selected bounds are calendar dates and may fall between two sessions, or before the first one, or after the last. There is no pair of prices to divide, so there is no change; the same rule as a date range with no rows. |
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
sample series and on real MySQL data. They are timed together with the other
metrics, in [Backend timings](#backend-timings-measured) below.

The honest reading of those numbers is that **for one small instrument the SQL
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

## Metric 2 — cumulative returns

### Business meaning

How far the price has come since the start of the range, as a fraction of where it
started. It answers "what would one unit held at the start of this period be worth
by each date", which is the sentence a period return is made of. It is the metric a
performance report quotes as a single number ("the period returned +4.0%") and the
one a chart draws as a cumulative curve.

### Formula

```
growth(t)     = close(t) / close(base)
cumulative(t) = growth(t) - 1
```

`base` is the first observation of the requested range. Both forms are reported:
`growth` is the wealth relative (`1.04`) and `value` is the return (`+4%`), and they
are the same fact in the two notations a reader might want.

This is also "the compounded product of the daily returns", because the product
telescopes:

```
prod(1 + r_i) = close(t) / close(base)
```

The engine divides the two prices instead of multiplying the returns, and that is
a deliberate precision choice rather than a shortcut. The returns it *reports* are
rounded to eight places, so compounding `n` of them accumulates up to `n x 5e-9` of
rounding — on a 500-day range, more than the value's own last place. Dividing the
closes is exact and gives the number the product would give at full precision. The
tests compute the product from the reported returns anyway, bounded by that
`n x 5e-9`, so the identity is checked rather than assumed.

### The base is inside the range

The obvious alternative is to anchor on the last close *before* the range. That is
a different statement, and it is worth being explicit about which one this is:

* **inside (this engine)** — the range's return is a statement about the range. The
  first value is `0`, which says where the base is, and every value is reproducible
  from the range's own prices;
* **prior** — the first value is the move *into* the range, computed from a row the
  caller did not ask for. A caller who wants it can read the daily return of the
  first date of the range, which is exactly that number (metric 1 reads it with
  `pad = 1` for this reason).

The consequence is worth stating plainly, because it looks like an inconsistency and
is not: for the same request, metric 1's first returned value is the move into the
range while metric 2's is `0`. They are answering different questions — "how did
this session move" and "how has this period gone" — and the difference is the one
session at the boundary, which belongs to the previous period.

### Intermediate calculations

| Trading day | Close | Calculation | Growth | Cumulative |
|---|---|---|---|---|
| 2024-03-11 | 100 | base | `1.00000000` | `0.00000000` |
| 2024-03-12 | 102 | `102 / 100` | `1.02000000` | `0.02000000` |
| 2024-03-13 | 101 | `101 / 100` | `1.01000000` | `0.01000000` |
| 2024-03-14 | 103 | `103 / 100` | `1.03000000` | `0.03000000` |
| 2024-03-15 | 104 | `104 / 100` | `1.04000000` | `0.04000000` |

The last figure — `4%`, the range's total — is the number
`apps/marketdata/tests/helpers.py` already documents as `EXPECTED_RETURN` (`0.04`
for AAPL, `0.05` for MSFT, `0.08` for GOOG). That fixture was written for the
period return, this metric must reproduce it, and a test asserts it for all three:
the same question asked two ways, from two independent pieces of code.

A falling series, because it exercises the sign handling and the rounding of a
loss:

| Trading day | Close | Growth | Cumulative |
|---|---|---|---|
| 2024-03-11 | 200 | `1.00000000` | `0.00000000` |
| 2024-03-12 | 190 | `0.95000000` | `-0.05000000` |
| 2024-03-13 | 195 | `0.97500000` | `-0.02500000` |
| 2024-03-14 | 205 | `1.02500000` | `0.02500000` |
| 2024-03-15 | 210 | `1.05000000` | `0.05000000` |

### What breaks the chain

A zero close is reachable data, and the day whose return divides by it has no
return. Every cumulative value from that date onwards is `None` — not `0`, and not
a chain restarted from the next price. The alternatives and why they lose:

| Behaviour | What it claims | Why not |
|---|---|---|
| Skip the day (as if it returned 0%) | That a 100% loss and a recovery did not happen | The wealth relative cannot cross it; the number is fabricated |
| Restart the chain | That the range's return is measured from a new base | Silently changes what "cumulative" is measured from, and the value is no longer comparable across the break |
| Propagate `None` (chosen) | That the chain is broken | The only statement the prices support |

The base is the one place a zero price is *fine*: one unit held at a price of zero
is still one unit, so the first value is `0` even then. What breaks is the *next*
return, which is checked separately. `CumulativeReturns.total` is the last point's
value, so a broken chain has no total — and reporting the total of a partial range
would be worse than reporting nothing.

### Implementations

Two, because there is no third: SQL has no windowed product, so `sql` is not in
`CUMULATIVE_RETURN_BACKENDS` and asking for it raises rather than silently falling
back. The `python` backend divides the closes; the `pandas` backend does the same
with a vectorised ratio (`close / close[0]`) and an explicit walk for the break
rule. The walk is Python rather than a vector mask because the chain policy is a
statement about the series, not a per-element operation, and it runs over rows
already in memory.

## Metric 3 — moving averages (7, 30, 90)

### Business meaning

A smoothed level: the average price over the last week, month, or quarter of
sessions. It is the reference line a report compares a price against, and it is
less jumpy than the price itself because a single session moves it by only `1/N` of
its own move. Three windows are asked for — 7, 30 and 90 sessions — because they
read as a week, a month and a quarter **of trading**, which is what a market
participant means by those periods.

The engine reports the level. It does not cross a price over an average, score the
crossing, or call it a signal; a crossing is a fact about two numbers, and what to
do about it is a reader's judgement, not this engine's.

### Three named series, not a window parameter

7, 30 and 90 are a **closed vocabulary**, not a knob:

```python
MOVING_AVERAGE_SERIES = (
    MovingAverageSeries(name="moving_average_7", window=7),
    MovingAverageSeries(name="moving_average_30", window=30),
    MovingAverageSeries(name="moving_average_90", window=90),
)
```

A caller asks for a name — `names=("moving_average_30",)`, or all three by default —
and the name determines the window, the loader's column, and, if any of them is ever
snapshotted, the `lookback_days`. There is deliberately **no `window` argument** on
the service. A free integer would let a caller compare a 7 against a 23 and call the
two the same statistic; three names make the comparison meaningful, and an unknown
name is a `ValueError` that lists the vocabulary rather than quietly handing back
fewer series than were asked for. The output order is conventional as well: asking
for 90 then 7 returns 7 then 90, so the same request in a different order is the same
payload, and a name asked for twice is computed once.

The arithmetic is one function for all of them. `moving_average(dates, closes,
window)` keeps its `window` because it is the *primitive* — the pure calculation the
unit tests hand-check — and the named series are a vocabulary layered over it.

### Formula and window semantics

```
SMA_N(t) = ( close(t-N+1) + … + close(t) ) / N
```

Three properties, each of which a reader could reasonably have assumed differently,
so each is pinned by a test:

* **the window is `N` observations**, the current one included. Not calendar days:
  a "7-day average" over a week containing a holiday spans eight calendar days, and
  a gap in the ingest does not become a day inside the window. This is what makes
  the window gap-tolerant by construction;
* **trailing and right-aligned**: the value at `t` uses no price after `t`. A centred
  average would put a future price into a past value, which is not usable in a
  report about a date;
* **a full window is required.** Until `N` observations exist the value is `None`,
  never the mean of what there is — a three-observation mean labelled a seven-
  observation mean is a different statistic with a more impressive name.
  `sample_size` reports how full the window is, so a young series can be told apart
  from a hole in the data.

### Intermediate calculations

The primitive, on closes 10, 12, 11, 13, 15 with window 3:

| Trading day | Close | Window | Calculation | Mean |
|---|---|---|---|---|
| 2024-03-11 | 10 | 10 | not full (1 of 3) | `null` |
| 2024-03-12 | 12 | 10, 12 | not full (2 of 3) | `null` |
| 2024-03-13 | 11 | 10, 12, 11 | `33 / 3` | `11.00000000` |
| 2024-03-14 | 13 | 12, 11, 13 | `36 / 3` | `12.00000000` |
| 2024-03-15 | 15 | 11, 13, 15 | `39 / 3` | `13.00000000` |

A mean that needs more than eight places, closes 1, 2, 2 with window 3:
`5 / 3 = 1.66666…` → `1.66666667` (half up).

The named 7-session series, on twelve sessions with closes 10 … 21:

| Trading day | Close | 7-observation window | Calculation | Mean |
|---|---|---|---|---|
| 2024-03-16 | 15 | 10, 11, 12, 13, 14, 15 | not full (6 of 7) | `null` |
| 2024-03-17 | 16 | 10, 11, 12, 13, 14, 15, 16 | `91 / 7` | `13.00000000` |
| 2024-03-19 | 18 | 12, 13, 14, 15, 16, 17, 18 | `105 / 7` | `15.00000000` |
| 2024-03-22 | 21 | 15, 16, 17, 18, 19, 20, 21 | `126 / 7` | `18.00000000` |

The 30- and 90-session series on the same twelve sessions are `null` at every row,
because twelve observations cannot fill either window — which is the completeness
rule doing its job rather than a bug.

The sample series, window 3, requested from 2024-03-13 — the table that shows the
warm-up paying off:

| Trading day | Close | Window | Calculation | Mean |
|---|---|---|---|---|
| 2024-03-13 | 101 | 100, 102, 101 | `303 / 3` | `101.00000000` |
| 2024-03-14 | 103 | 102, 101, 103 | `306 / 3` | `102.00000000` |
| 2024-03-15 | 104 | 101, 103, 104 | `308 / 3` | `102.66666667` |

Read without the warm-up, the same three dates give `null`, `null`, and only the
last mean — which is the contrast a test asserts directly.

### The metric that justifies the loader's `pad`

One row of warm-up was enough for a return. A 90-observation average needs **89**,
and the loader's boundary subquery takes the offset as a parameter, so nothing about
the loader changes: `pad=window-1`. The named series make that parameter a *sequence*
— the loader adds one `AVG(close) OVER (...)` column per window — while still taking
**one** pad, covering the longest series asked for.

That is a choice with a visible cost, so it is stated rather than hidden. On the
benchmark instrument a request for all three reads `89 + 31 = 120` rows and computes
three means from them. Three separate calls would read `6 + 31`, `29 + 31` and
`89 + 31` rows over three queries. So asking for all three is *cheaper* in rows than
the three calls it replaces, and it is one query instead of three. The price is paid
only by a caller who wants the 7-session series alone: it still reads 89 warm-up rows,
which is why `names` exists as a selection. A test asserts the padding is
`max(window) - 1` and that a short history bounds it (the `Coalesce` fallback).

### What the database does with the window

The `sql` backend pushes all three means into the query, as three frame aggregates
over the same rows:

```sql
AVG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)  AS moving_average_7
AVG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS moving_average_30
AVG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN 89 PRECEDING AND CURRENT ROW) AS moving_average_90
```

and `EXPLAIN FORMAT=TREE` on it, against MySQL, reads:

```
-> Sort: marketdata_pricerecord.trading_date
    -> Table scan on <temporary>  (cost=27.7..31.6 rows=120)
        -> Temporary table
            -> Window aggregate with buffering:
                 avg(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                                  ROWS BETWEEN 89 PRECEDING AND CURRENT ROW)
                -> Table scan on <temporary>
                    -> Temporary table
                        -> Window aggregate with buffering:
                             avg(close) OVER (... ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
                            -> Table scan on <temporary>
                                -> Temporary table
                                    -> Window aggregate with buffering:
                                         avg(close) OVER (... ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
                                        -> Sort: instrument_id, trading_date  (cost=54.3 rows=120)
                                            -> Filter: (trading_date >= <cache>(coalesce((select #2),(select #3))))
                                                -> Index range scan ... over (instrument_id = 13 AND
                                                     '2023-09-29' <= trading_date <= '2024-03-14')
                                                -> Select #2 (subquery in condition; run only once)
                                                    -> Limit/Offset: 1/88 row(s)
                                                -> Select #3 (subquery in condition; run only once)
                                                    -> Limit: 1 row(s)
```

Four things in that plan are the design, visible rather than asserted:

* **all three frames are in the one statement** — the verification script asserts
  that each window's `ROWS BETWEEN … PRECEDING` appears in the SQL actually sent, so
  "one query for three series" is checked rather than claimed (measured: 120 rows for
  `sql`, `padding=89`, `undefined=0`, and the last values
  `41.38411429` / `40.76325000` / `39.82356889` for 7 / 30 / 90);
* the frames are `6`, `29` and `89 PRECEDING AND CURRENT ROW` — the windows' own
  off-by-one, once per named series;
* the boundary subquery is `Limit/Offset: 1/88`, i.e. the 89th prior date, computed
  once (`run only once`) and cached, and the index range scan *starts* at the resolved
  warm-up date (`2023-09-29`), so the padding is part of the range rather than a
  filter applied to a result;
* there is exactly **one sort** for all three — the frames are nested over the same
  ordered rows rather than the rows being sorted three times.

The honest cost is the nesting: MySQL implements each frame with its own **buffered
window aggregate and temporary table**, and three frames mean three of them, layered.
That is work the Python rolling sums do not do — they are three passes over rows
already in memory, with no temporary table — which is the trade-off the timing table
below records. It is also why the `sql` backend is the default where it exists but is
not the fastest here: what it offers is that the arithmetic runs where the rows are,
not that it runs sooner.

### Trade-offs

**A rolling sum is exact in `Decimal`, and that is not a small thing.** The
implementation adds the close that entered the window and subtracts the one that
left it, so the cost is `O(observations)` rather than `O(observations x window)`.
With binary floats that optimisation drifts — the classic rolling-sum error, where
repeated add/subtract leaves a residue — and the shortcut would have to be
abandoned or corrected. `Decimal` addition is exact, so the incremental sum is
*identical* to re-adding the window, and a test asserts that identity on prices
that have no exact binary representation.

**Two routes, one rule about completeness.** The database's `ROWS BETWEEN` frame
averages *however many rows it has*, so the first rows of a short history come back
as partial means. Rather than trusting the frame, the engine applies the same
completeness rule to a supplied mean as to a computed one and reports `None`. The
rule lives in one place (`sample_size == window`), and the three backends are
asserted equal on a series shorter than its window — the case where an off-by-one
would hide.

**A moving average is not a smoothed *return*.** It is a mean of prices, so it is
denominated in currency and its rounding is money rounding
(`quantize_price`, eight places) rather than ratio rounding. The two happen to be
the same width today, which is exactly why they have different names in
`metrics.py`.

**A name is a contract, and that is the point.** Because the windows are a closed
set, `moving_average_7` means one thing in a service call, a loader column, a
snapshot's `lookback_days` and this document. A single free `window` parameter is
more flexible and less useful: flexibility that lets two reports disagree about what
"the moving average" is has a cost paid at every comparison, while a fixed set has a
cost paid once, when a fourth window is wanted.

## Metric 4 — rolling volatility

### Business meaning

How much the return moves, i.e. risk as it is conventionally quantified: two
instruments with the same average return and different volatility are not the same
investment, and the second number is this one. It is reported per date as a rolling
statistic, so a report can say "volatility over the last month" as a series rather
than one number for all time.

### Formula and conventions

```
sigma(t) = sqrt( sum((r_i - mean(r))^2) / (window - 1) )      over the window's returns
value(t) = sigma(t) * sqrt(periods_per_year)
```

Each choice in that pair of lines could reasonably go the other way, so each is
documented and tested:

* **sample standard deviation, `ddof = 1`**, dividing by `window - 1`. The returns
  in a window are a sample of the process that generated them, not the population.
  The choice is not cosmetic: with two observations the population form is 41.4%
  smaller than the sample form, and a test asserts the sample answer for a window
  whose two returns are `+10%` and `-10%` (`sigma = 0.1 x sqrt(2) = 0.14142136`, not
  `0.1`). It also matches `pandas.rolling().std()`, whose default is `ddof=1`;
* **simple returns**, the same ones metric 1 reports, not log returns. One
  definition of "return" in the engine is worth more than the modest statistical
  advantage of log returns, which differ at first order for small moves and
  materially for large ones. A log-return variant would change the input to the
  deviation, not the deviation;
* **annualized by `sqrt(252)`**, 252 sessions a year. This is a model, not a fact,
  and it is the part of this metric most often quoted without its caveat: it assumes
  returns are independent and identically distributed so that variance grows
  linearly with time. Real returns cluster — quiet weeks follow quiet weeks — so an
  annualized figure taken from a quiet window **overstates** the risk and one taken
  from a violent window **understates** it. `periods_per_year` is a parameter, the
  per-session figure is reported alongside as `daily`, and the mean return of the
  window is reported as `mean`, so the working is in the response and a reader can
  annualize differently or not at all;
* **a full window of defined returns is required.** A zero previous close inside the
  window leaves it incomplete and the value is `None`. A window of *identical*
  returns is not incomplete: zero volatility is a value, and it is reported as `0`.

`window` must be at least 2, because the sample deviation of one observation is a
division by zero rather than a small number.

### Intermediate calculations

**Returns 1%, 2%, 3%, window 3.** Mean 2%, deviations -1%, 0, +1%, sum of squares
`2e-4`, variance `2e-4 / 2 = 1e-4`, so sigma is exactly `0.01`:

| Quantity | Value |
|---|---|
| `mean` | `0.02000000` |
| `daily` | `0.01000000` |
| Calculation | `0.01 x sqrt(252) = 0.01 x 15.874507866…` |
| `value` | `0.15874508` |

**Returns +10%, -10%, window 2.** Mean 0, sum of squares `0.02`, variance
`0.02 / 1 = 0.02`:

| Quantity | Value |
|---|---|
| `daily` | `0.14142136` (`0.1 x sqrt(2)`) |
| Calculation | `0.1 x sqrt(2) x sqrt(252) = 0.1 x sqrt(504)` |
| `value` | `2.24499443` |
| The `ddof = 0` answer would be | `0.10000000` — 41.4% smaller |

**Returns 10%, 10%, 10%, window 3.** No deviation at all: `daily = 0.00000000` and
`value = 0.00000000`, reported as values, not as `null`.

On the sample series with window 3:

| Trading day | Returns in the window | `mean` | `daily` | `value` |
|---|---|---|---|---|
| 2024-03-14 | 2.000%, -0.980%, 1.980% | `0.00999935` | `0.01715042` | `0.27225453` |
| 2024-03-15 | -0.980%, 1.980%, 0.971% | `0.00656893` | `0.01505062` | `0.23892116` |

Reproducibility is a property the tests assert rather than a hope: the deviation is
computed from the **reported** returns, so a reader can recompute a window's sigma
from the JSON of metric 1 alone. A test writes the definition out by hand from those
reported returns and compares.

### Implementations, and the SQL that does not exist

Three backends, but only two kinds of arithmetic: the `sql` backend takes the
returns from `LAG(close)` — a window function doing exactly the job it is for — and
then computes the deviation in Python, and `python`/`pandas` differ only in where
the returns came from and whether the deviation is vectorised. The three agreeing
point-for-point is therefore a statement about the *implementations* rather than
about the plumbing: they share their inputs (the reported returns) and their
convention (`ddof = 1`), so the comparison is between a hand-written decimal loop
and `Series.rolling().std()` rather than between two spellings of the same loop.

There is no pure-SQL volatility, and the reason is a hard limit rather than a
preference: the deviation is over the *returns*, the returns are `LAG(close)`, and
**a window function may not appear inside another window function's arguments**.
MySQL says so directly:

```
mysql> SELECT STDDEV_SAMP((close - LAG(close) OVER (...)) / LAG(close) OVER (...))
              OVER (PARTITION BY instrument_id ORDER BY trading_date ROWS BETWEEN ...);
ERROR 3593 (HY000): You cannot use the window function 'lag' in this context.
```

It *is* expressible as a derived table — compute the returns in an inner query, take
the deviation in an outer window — and the verification script does exactly that as
a cross-check, which is where this metric gets its strongest piece of evidence.
MySQL's `STDDEV_SAMP` is the **sample** deviation, i.e. `ddof = 1`, so it is the
engine's convention computed by a different implementation on a different side of the
connection, and the two agree. On the benchmark instrument, window 30, last
available date:

| Quantity | Engine | MySQL `STDDEV_SAMP` in a derived table |
|---|---|---|
| `daily` | `0.00831893` | `0.00831892589489335` |
| `value` | `0.13205884` | `0.13205885` |

The daily figures agree exactly at eight places, and the annualized figures differ by
one unit in the last place — because the engine deviates the reported returns (eight
places) while MySQL deviates raw double-precision ratios, and the `sqrt(252)` factor
multiplies that difference by sixteen. The one-place difference is the honest
measure of computing from published values rather than from raw ones.

The engine still keeps the deviation in Python for the ordinary path. A derived
table means a second level of query, a materialised inner result set, and a float
returned across the connection — three things the engine avoids everywhere else, and
all three for a movement of one unit in the last place.

## Metric 5 — maximum drawdown

### Business meaning

The worst a holder of the instrument could have felt: the largest fall from a
prior high, and whether it came back. A return says where a price ended up; a
drawdown says what the path did on the way, which is the difference between two
instruments that arrive at the same place with very different experience — and it
is the number a risk limit is usually written against. It is reported per date
(the decline from the high so far) *and* as one summary episode: how deep, from
which date to which date, and whether the peak was regained inside the range.

### Formula

```
peak(t)  = max(close(s) for s <= t)      # the running maximum, s in the range
value(t) = close(t) / peak(t) - 1        # <= 0; exactly 0 on a new high
```

The summary is the minimum of `value` over the range, with the pair of dates that
produced it. The sign is the engine's: a decline is negative, so a report that
quotes a drawdown as a magnitude reads `-value`. Nothing reports both, because two
signs for one statistic is how a number gets inverted in a report.

### The peak is inside the range

This is the same decision metric 2 makes about its base, and here it is forced
rather than chosen. The alternative — the **all-time** high, so that a request
starting mid-decline reports the decline it inherited — needs the running maximum
to see every row before `start_date`. The other metrics' warm-up is a *fixed*
`pad` (`1`, `window - 1`, `window`), and the all-time peak is not fixed: it is
however far back the instrument's first row is, which turns a bounded range scan
into an unbounded read and makes the response change as history accumulates.

So the peak starts at the range's first observation, and a caller who wants a
decline that began earlier asks for a range that starts earlier. The consequence
is the sharpest version of the point made in metric 2, and a test asserts it as
the difference between two requests:

| Request | Closes | Peak at the first date | `max_drawdown` |
|---|---|---|---|
| whole series | 100, 102, 101, 103, 104 | 100 | `-0.00980392` |
| from 2024-03-13 | 101, 103, 104 | 101 | `0.00000000` |

The 102 of 2024-03-12 is the highest close in the table, and the second request
does not see it — not because it is hidden, but because the request is about the
range from 2024-03-13. `padding` is `0`, so the read is exactly the requested rows.

### Intermediate calculations

The running maximum, and the decline below it, on a series with room for several
episodes (closes 100, 120, 90, 110, 80, 130):

| Trading day | Close | Peak | Peak date | Calculation | Drawdown |
|---|---|---|---|---|---|
| 2024-03-11 | 100 | 100 | 2024-03-11 | new high | `0.00000000` |
| 2024-03-12 | 120 | 120 | 2024-03-12 | new high | `0.00000000` |
| 2024-03-13 | 90 | 120 | 2024-03-12 | `90 / 120 - 1` | `-0.25000000` |
| 2024-03-14 | 110 | 120 | 2024-03-12 | `110 / 120 - 1 = -0.08333…` | `-0.08333333` |
| 2024-03-15 | 80 | 120 | 2024-03-12 | `80 / 120 - 1 = -0.33333…` | `-0.33333333` |
| 2024-03-16 | 130 | 130 | 2024-03-16 | new high | `0.00000000` |

The worst episode is the `-0.33333333` of 2024-03-15, measured from the 120 of
2024-03-12, and it is made back on 2024-03-16 (130 ≥ 120). Every point names its
own peak, so a reader can check any row of the response rather than only the worst
one — which is why `peak` and `peak_date` are per point and not just in the
summary.

The sample series, where the drawdown is small and the recovery immediate:

| Trading day | Close | Peak | Calculation | Drawdown |
|---|---|---|---|---|
| 2024-03-11 | 100 | 100 | new high | `0.00000000` |
| 2024-03-12 | 102 | 102 | new high | `0.00000000` |
| 2024-03-13 | 101 | 102 | `101 / 102 - 1 = -0.00980392…` | `-0.00980392` |
| 2024-03-14 | 103 | 103 | new high | `0.00000000` |
| 2024-03-15 | 104 | 104 | new high | `0.00000000` |

### Edge cases with a policy, not an accident

| Situation | Behaviour | Why |
|---|---|---|
| New high | Drawdown `0`, and the peak moves with the price | Nothing was given up. `0` is a value, and it is the only date whose peak is the price itself. |
| A second date at the same high | The peak date stays at the **first** date that reached it | "The episode starts at the high" means the first high. Otherwise the same decline would date itself from a later row that gave nothing up. |
| Peak of zero | The drawdown is `None` until the price exceeds zero | `close / 0` is not a ratio. Reporting `0` would claim the price equals its high; `inf` is not a drawdown. The next close above zero is a new high, so the metric resumes. |
| Series that never fell | `max_drawdown = 0`, `peak_date`/`trough_date` `None`, `recovered` `None` | There is no peak-to-trough episode to name, and nothing was given up to be made back. Naming the first and last dates would invent an episode. |
| The worst decline twice | The **earliest** episode is reported | A 20% drawdown twice is one number. The earlier one is what a report means, and a recovery search from the later trough would mis-date it. |
| Still under water at the range's end | `recovered = False`, `recovery_date = None` | A statement about the data, not a forecast: the peak was not regained *by the last observation in the range*. |
| A gap between sessions | The trough is the next *observation*, however many calendar days later | Windows and episodes count observations, as everywhere else in the engine. |

### What the database does, and why this one has a SQL backend

The running maximum is a scan, and a scan is a window SQL already has:

```sql
MAX(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_max
```

The difference from volatility is the whole point of the rule stated in the
backends section: this window is over the *closes*, so it is one legal frame, and
the database can carry the entire scan. Only the ratio on top is arithmetic.

`EXPLAIN FORMAT=TREE` on it, against MySQL, over a request bounded to
2024-02-01 … 2024-03-14:

```
-> Sort: marketdata_pricerecord.trading_date
    -> Table scan on <temporary>
        -> Temporary table
            -> Window aggregate: max(marketdata_pricerecord.close) OVER (
                 PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                -> Sort: instrument_id, trading_date
                    -> Index range scan ... over (instrument_id = 13 AND
                         '2024-02-01' <= trading_date <= '2024-03-14')
```

Two things are visible in that plan and both are the design. The frame is
`UNBOUNDED PRECEDING AND CURRENT ROW`, so the peak has no ceiling but does have a
floor — the first row returned. And the range scan **starts at the requested
date**: there is no offset subquery and no warm-up row in this query at all,
because the anchoring is by construction rather than by padding.

The honest cost is the same temporary table the moving average pays for, for the
same reason: a frame aggregate is buffered. The Python and Pandas backends do the
same scan over rows already in memory, which the timing table below measures.

The database can also produce the *whole* metric — but only one level up, because
a window function cannot be an argument to another window function. MySQL refuses
the direct form and accepts the derived one, which is what the verification script
uses as a cross-check:

```sql
-- refused, with the same error the volatility section quotes for its own reason
SELECT MIN(close / MAX(close) OVER (...)) OVER (...);

-- accepted: the running maximum inside, the worst ratio outside
SELECT MIN(ratio) FROM (
    SELECT close / MAX(close) OVER (ORDER BY trading_date
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 1 AS ratio
    FROM marketdata_pricerecord
    WHERE instrument_id = 13 AND trading_date BETWEEN '2024-02-01' AND '2024-03-14'
) AS peak;
```

On the benchmark instrument, the derived query returns `-0.025169249869` and the
engine returns `-0.02516925`: the same number at the engine's eight places, which
is the strongest check available for a statistic whose "worst row" is a date and
not a value. The engine still computes the ratio in Python, because the date of
the worst point — the thing a drawdown report is actually about — is not a SQL
aggregate, and shipping a float across the connection to recover a date that is
already in the rows would be the wrong trade.

## Metric 6 — highest and lowest prices

### Business meaning

What the period actually contained. A moving average or a return compresses a
range into one number; the extremes say how far the price travelled, which is what
a support level, a limit order or a stop is set against, and what a reader means by
"it traded between X and Y". Both the date and the price are reported, because a
high without a date cannot be checked or plotted.

### Formula

```
highest_close(t) = max(close(s) for s <= t)
lowest_close(t)  = min(close(s) for s <= t)     # s in the range
```

and, over the whole range, two more pairs from the traded columns:

```
highest_high = max(high(s))      lowest_low = min(low(s))
traded_range = highest_high - lowest_low
```

### "The highest price" is two different questions

This is the one metric where the requirement is ambiguous, and the engine reports
both readings rather than choosing one silently:

* the extremes of the **closes** — the reference price every other metric is built
  on, and the price a return, a moving average or a drawdown is made of;
* the extremes of the **traded range**, from the `high` and `low` columns — what
  the market actually *touched* intraday.

They are different numbers and neither implies the other. On the benchmark
instrument over 2024-02-01 … 2024-03-14:

| | price | date |
|---|---|---|
| highest close | `41.84570000` | 2024-03-14 |
| highest **high** | `42.21780000` | 2024-03-14 |
| lowest close | `39.89900000` | 2024-02-09 |
| lowest **low** | `39.21960000` | 2024-02-09 |
| traded range | `2.99820000` | — |

The price reached 42.2178 intraday but never closed above 41.8457, and touched
39.2196 but never closed below 39.899. A reader who asked for "the highest price"
and got the close would be short by 0.37; one who got the high and assumed it was a
close would carry a price that never existed at the close. Reporting both, named,
is the only answer that cannot be misread.

**`traded_range` is not any row's range.** `high >= low` holds within a row (the
table constrains it), but the highest high and the lowest low of a period generally
come from different sessions, so this is the width of the *period*, not of a day. On
the hand series below, no single row spans the 12 the period does.

### The extremes are inside the range

Anchored at the first observation of the requested range, as the drawdown's peak
and the cumulative base are, and for the same reason: the all-time high is however
far back the instrument's first row is, which is not a fixed `pad`. `padding` is
`0`, the read is exactly the requested rows, and the first point is — correctly —
both the highest and the lowest close seen so far.

### Intermediate calculations

Closes 100, 102, 101, 103, 104 with traded highs 105, 106, 103, 107, 105 and lows
98, 99, 95, 101, 100 — chosen so the two readings disagree:

| Trading day | Close | High | Low | Highest so far | Lowest so far |
|---|---|---|---|---|---|
| 2024-03-11 | 100 | 105 | 98 | `100` (new) | `100` (new) |
| 2024-03-12 | 102 | 106 | 99 | `102` (new) | `100` |
| 2024-03-13 | 101 | 103 | **95** | `102` | `100` |
| 2024-03-14 | 103 | **107** | 101 | `103` (new) | `100` |
| 2024-03-15 | 104 | 105 | 100 | `104` (new) | `100` |

| Quantity | Value | Date |
|---|---|---|
| `highest_close` | `104` | 2024-03-15 |
| `lowest_close` | `100` | 2024-03-11 |
| `highest_high` | `107` | 2024-03-14 |
| `lowest_low` | `95` | 2024-03-13 |
| `traded_range` (`107 - 95`) | `12` | — |

The highest **close** and the highest **high** fall on different days — the 107 of
the 14th gave back to 105 on the 15th — and the lowest close of the period (100) is
*higher* than the lowest low (95), which belongs to the 13th. Every point carries
its own running extremes and a `is_new_high`/`is_new_low` flag, so the rows that
moved the answer are visible rather than inferred.

On the sample series (where the fixture stores `high = low = close`) the two
readings coincide, and the test asserts that rather than assuming it:

| Trading day | Close | Highest so far | Lowest so far |
|---|---|---|---|
| 2024-03-11 | 100 | `100` (new) | `100` (new) |
| 2024-03-12 | 102 | `102` (new) | `100` |
| 2024-03-13 | 101 | `102` | `100` |
| 2024-03-14 | 103 | `103` (new) | `100` |
| 2024-03-15 | 104 | `104` (new) | `100` |

### Edge cases with a policy, not an accident

| Situation | Behaviour | Why |
|---|---|---|
| First observation | Both a new high and a new low | It is the only price seen, so it is the highest and the lowest. `0` or a null would both be wrong. |
| A repeated extreme | The date stays at the **first** date that reached it | The same rule as the drawdown's peak: the extreme "happened" when it first happened, and a later row that reached it again gave nothing new. |
| `high`/`low` not read | `highest_high`/`lowest_low`/`traded_range` are `None` | A close-only series has no traded extremes. Reporting the closes under that name would be the worst available answer. |
| An empty range | Every field `None`, no points | There is no extreme of nothing, and inventing the first or last date of a range with no rows would be a fabrication. |
| Ties for an extreme | The earliest date | Consistent with every other dated extreme in the engine. |
| A four-day holiday | The extremes span *observations*, not calendar days | The price that "lasted" across a closure is the same price; the engine does not invent a session. |

### What the database does

Two single windows, both over the closes, can be had in one query:

```sql
MAX(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_max,
MIN(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_min
```

`EXPLAIN FORMAT=TREE` against MySQL shows both frames, and shows the range scan
starting at the requested date — no offset subquery, no warm-up row:

```
-> Sort: trading_date
    -> Table scan on <temporary>
        -> Temporary table
            -> Window aggregate: min(close) OVER (PARTITION BY instrument_id
                 ORDER BY trading_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                -> Temporary table
                    -> Window aggregate: max(close) OVER (PARTITION BY instrument_id
                         ORDER BY trading_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                        -> Sort: instrument_id, trading_date
                            -> Index range scan ... over (instrument_id = 13 AND
                                 '2024-02-01' <= trading_date <= '2024-03-14')
```

MySQL materialises the second window over the temporary table of the first: two
buffered stages, which is the cost of asking a second question of the same frame
and is why the timing table puts `sql` last on this metric. It is still one query,
and it is still one round trip.

The four extremes are checked against the database's own aggregates, which cannot
be wrong in the same way:

```sql
SELECT MAX(close), MIN(close), MAX(high), MIN(low)
FROM marketdata_pricerecord
WHERE instrument_id = 13 AND trading_date BETWEEN '2024-02-01' AND '2024-03-14';
```

```
MySQL: MAX(close)=41.84570000 MIN(close)=39.89900000 MAX(high)=42.21780000 MIN(low)=39.21960000
the engine's four extremes equal the database's four aggregates
```

A value check, not a date check — the date an extreme happened is the part SQL
cannot return, and it is the part the rows are kept for.

### `undefined` on this metric

Every metric's result reports how many values were missing. A price-extremes point
has *two* prices and no single `value` field, so the count is overridden to `0` with
a docstring saying why, rather than being inherited and raising: a point here cannot
be missing a value, because every row has a close. The traded pair can be `None`,
but only when there is no range at all, in which case there are no points to count.

## Metric 7 — trading-volume summaries

### Business meaning

How much changed hands, and whether that is normal. A price move on heavy volume is
a different event from the same move on light volume: volume is the participation
behind a price, and the questions a report is actually made of are — how much traded
over the period, what a typical session looked like, which sessions were heaviest and
lightest, and whether volume was running above or below its own average. It is also
the denominator of every liquidity question, and the thing a halt or a data gap is
most likely to show up in.

### Formula

With ``n`` sessions up to and including ``t``, and ``v`` the session's volume:

```
cumulative_volume(t) = sum(volume(s) for s in range, s <= t)
mean_so_far(t)       = cumulative_volume(t) / n
relative_volume(t)   = volume(t) / mean_so_far(t)
```

and over the whole range:

```
total_volume      = cumulative_volume(last)
average_volume    = total_volume / observations
highest_volume    = max(volume(s))          lowest_volume = min(volume(s))
```

with the count of sessions above, exactly at, and below the average, using the
**exact** mean: a session is above average when ``volume x n > total``, which is the
same statement as ``volume > total / n`` with no division and no rounding.

### The ratio is against the mean *so far*, not the period's mean

This is the metric's one real design decision, and it is the same rule every other
window in the engine follows: **a value dated ``t`` may not use a session after
``t``**. The period's mean is only known once the period is over, so a relative
volume computed against it would put a number in the first session's row that could
not have been computed on that day. Four typical sessions and one heavy one show the
difference concretely:

| Session | Volume | Mean so far | `relative_volume` | Against the period's mean (180) |
|---|---|---|---|---|
| 1 | 100 | 100 | `1.00000000` | `0.55555556` |
| 2 | 100 | 100 | `1.00000000` | `0.55555556` |
| 3 | 100 | 100 | `1.00000000` | `0.55555556` |
| 4 | 100 | 100 | `1.00000000` | `0.55555556` |
| 5 | 500 | 180 | `2.77777778` | `2.77777778` |

Only the last row can be computed both ways; the first four can only be the `1` of
"exactly typical of this range so far". A reader who wants the end-of-period view
has the **summary**, which is a statement about the whole range and may use the whole
range's mean — the same division of labour as the drawdown's per-date decline
against its summary episode.

### Intermediate calculations

Five sessions of 100, 300, 200, 0, 400 shares:

| Trading day | Volume | Total so far | `n` | Mean so far | `relative_volume` |
|---|---|---|---|---|---|
| 2024-03-11 | 100 | 100 | 1 | 100 | `1.00000000` |
| 2024-03-12 | 300 | 400 | 2 | 200 | `1.50000000` |
| 2024-03-13 | 200 | 600 | 3 | 200 | `1.00000000` |
| 2024-03-14 | **0** | 600 | 4 | 150 | `0.00000000` |
| 2024-03-15 | 400 | 1000 | 5 | 200 | `2.00000000` |

and the period's summary, every figure checkable against the column above:

| Quantity | Value | Check |
|---|---|---|
| `total_volume` | `1000` | the last running total |
| `observations` | `5` | one per session |
| `average_volume` | `200` | `1000 / 5` |
| `highest_volume` | `400` on 2024-03-15 | the largest volume, first date on a tie |
| `lowest_volume` | `0` on 2024-03-14 | the zero-volume session |
| `sessions_above_average` | `2` | 300 and 400 (`v x 5 > 1000`) |
| `sessions_at_average` | `1` | 200 (`200 x 5 == 1000` exactly) |
| `sessions_below_average` | `2` | 100 and 0 |
| `zero_volume_sessions` | `1` | 2024-03-14 |

The three classification counts always add up to `observations`, which a test pins
across several datasets.

### Why the classification does not use the reported average

Volumes 2 and 3, `n = 2`:

| Session | Volume | `v x n` | vs `total = 5` | Classification |
|---|---|---|---|---|
| 1 | 2 | 4 | `4 < 5` | below |
| 2 | 3 | 6 | `6 > 5` | **above** |

The exact mean is `2.5`, which the count quantizer reports as `3` (half up). Comparing
against the *reported* `3` would call the three-share session "exactly average" — it
is not; `3 > 2.5`. Multiplying the two exact integers instead of dividing is both
exact and reproducible from the payload, which this test pins as
`test_the_classification_uses_the_exact_mean_not_the_rounded_one`.

### Trading days versus calendar days

`average_volume` is per **observation** — per trading session. On the benchmark range
2024-02-01 … 2024-03-14 the difference is not subtle:

```
31 sessions span 43 calendar days: per session 2356781, per calendar day 1699075 (28% lower)
```

Dividing the total by 43 would report an "average" that is 28% below anything the
market did, because 12 of those days were weekends and holidays on which nothing
traded. `observations` and `total_volume` are both in the response, so a reader who
wants a per-calendar-day figure can compute it deliberately rather than receive it
by accident. The same distinction is why every window in this engine counts
observations (see the window-units table above), and why a *gap* — a session with no
row — is not an observation at all.

### Edge cases with a policy, not an accident

| Situation | Behaviour | Why |
|---|---|---|
| A zero-volume session | Kept as an observation; `relative_volume` is `0`, and it is counted in `zero_volume_sessions` | It is a session that traded nothing — real information. Dropping the row would change the denominator and flatter the mean (2 sessions of 0 and 10 average 5, not 10). |
| Nothing has traded *yet* | `relative_volume` is `None` while the running total is `0` | `volume / 0` is not a ratio, and `0` would claim the session was typical of a mean that does not exist. The running total is still exact and still reported, and the result counts these points as `undefined`. |
| A constant-volume series | Exactly at its own average on every session: above `0`, at `n`, below `0` | `v x n == total` for every session, which is a true statement rather than a degenerate case. |
| A tie for heaviest or lightest | The **first** session that reached it | Consistent with every other dated extreme in the engine. |
| An empty range | No points, every field `None` except the counts, which are `0` | There is no volume of nothing. |
| `volume` fractional in a future schema | `quantize_count` is the one place that changes | The column's comment already anticipates fractional-volume assets; the name exists so that change is local. |

### What the database does

A running total is a scan, and a scan is a window the database already has:

```sql
SUM(volume) OVER (PARTITION BY instrument_id ORDER BY trading_date
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total
```

`EXPLAIN FORMAT=TREE` against MySQL, over a request bounded to 2024-02-01 …
2024-03-14:

```
-> Sort: marketdata_pricerecord.trading_date
    -> Table scan on <temporary>
        -> Temporary table
            -> Window aggregate: sum(marketdata_pricerecord.volume) OVER (
                 PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                -> Sort: instrument_id, trading_date
                    -> Index range scan ... over (instrument_id = 13 AND
                         '2024-02-01' <= trading_date <= '2024-03-14')
```

One buffered stage — fewer than the extremes, which needs two — and the range scan
starts at the requested date: no offset subquery, and no warm-up row for a metric
whose anchor is inside the range.

The summary is checked against the database's own aggregates, which are exact:

```
MySQL: SUM=73060222 AVG=2356781.3548 MAX=4697728 MIN=61475 COUNT=31
total 73060222, average 2356781 per session, 31 sessions
heaviest 4697728 on 2024-03-07, lightest 61475 on 2024-02-14
above 17, at 0, below 14, zero-volume sessions 0
```

`SUM`, `MAX`, `MIN` and `COUNT` are asserted equal. `AVG` is *not* asserted equal to
the last place, and deliberately: MySQL returns `2356781.3548` and the engine rounds
its own exact quotient to the count `2356781`, so the two can differ in the final
place of a rounding. The exact figures the mean is derived from are the ones asserted,
which is the point of reporting them.

### Snapshots, and recalculating after new data

This metric is **computed on demand**, like every other per-instrument series: the
answer depends on the requested range, and the rows are already in memory. It is not
a cross-sectional figure, which is what the snapshot grain is for.

If a period total were stored, the recalculation rule is the one the existing
snapshot model already imposes and no more: the read is a pure function of
`PriceRecord` rows in a bounded range, so re-running it after new prices arrive
produces the same numbers for the same range and different numbers only for a range
that now contains the new sessions — no incremental state, no accumulation, and no
"recalculate *everything*" step. The upsert key
(`instrument, as_of_date, metric_type, lookback_days`) would make the rewrite
idempotent if the job existed. It does not yet; see *Deliberately not done yet*.

## Metric 8 — percentage price change over a selected period

### Business meaning

One number for a period: how much the price moved between two points in time, as a
percentage of where it started. It is what a report means by "up 4.4% this month", and
it is the *endpoint* of a shape metric 2 already lays out — cumulative returns are
this same ratio computed at every session, against the range's first observation.

It is a separate entry point for three reasons, none of which is "the number is
different":

* **the period need not be the range.** The base can be any date: "the change from the
  1st to the 15th" is a question about two sessions, not about the whole series
  requested;
* **a report wants the number and the percentage**, not a series it has to read the
  last row of;
* **a report wants several periods at once** — this week, this month, this quarter —
  and the engine answers all of them from **one read** of their union.

### Formula

```
change(t0, t1)  = ( close(t1) - close(t0) ) / close(t0)
percent_change  = change x 100
```

with `t0` the first observation at or after the period's start and `t1` the last at or
before its end. Both are reported back (`base_date`, `base_close`, `end_date`,
`end_close`), because a requested bound is a calendar date and the session actually
used may be a different one.

### Intermediate calculations

The sample series, closes 100, 102, 101, 103, 104 on consecutive sessions:

| Selected period | Base | End | `change` | `percent_change` | `observations` |
|---|---|---|---|---|---|
| 2024-03-11 … 2024-03-15 | 100 | 104 | `(104 - 100) / 100 = 0.04` | `4.00000000` | 5 |
| 2024-03-12 … 2024-03-15 | 102 | 104 | `2 / 102 = 0.0196078431…` | `1.96078400` | 4 |
| 2024-03-13 … 2024-03-15 | 101 | 104 | `3 / 101 = 0.0297029702…` | `2.97029700` | 3 |
| 2024-03-14 … 2024-03-15 | 103 | 104 | `1 / 103 = 0.0097087378…` | `0.97087400` | 2 |
| 2024-03-13 … 2024-03-13 | 101 | 101 | `0 / 101 = 0` | `0.00000000` | 1 |

A one-session period is a change of exactly `0`, which is a true statement rather than
a degenerate case, and it carries `observations` of `1` so it can be told apart from a
period that held no session at all.

### The percentage is derived from the ratio, not from the prices

`percent_change` is a property: `change x 100`, quantized once. It is **not**
recomputed from the two closes. That makes the payload internally consistent — a
reader can check `percent_change == change x 100` exactly, and a test asserts it for
every period shape — at a stated cost: the percentage carries the ratio's eight places
scaled up rather than eight places of its own. For a 2% move that is `2.00000000`
where re-deriving from the prices would give `2.00000001` — a hundredth of a basis
point, below any market's tick, in exchange for two fields that cannot disagree. A
reader who wants the extra place has `base_close` and `end_close` to hand.

### Several periods, one read

`percentage_change_for_instrument(instrument, periods=(...))` takes a sequence of
labelled periods — `SelectedPeriod(label, start_date, end_date)`, where a `None` bound
means "from the beginning" or "to the end" — and answers each one from **one** read of
their union. On the benchmark instrument, four periods spanning 2022-04-15 …
2024-03-14 came back in **1 query** (asserted):

```
last 5   2024-03-08 -> 2024-03-14  change=0.00755564  percent=0.75556400
last 21  2024-02-15 -> 2024-03-14  change=0.01466751  percent=1.46675100
window   2024-02-01 -> 2024-03-14  change=0.04438793  percent=4.43879300
to date  2022-04-15 -> 2024-03-14  change=0.52575083  percent=52.57508300
```

Note the union rule: the `to date` period has no start, so the read opens to the first
row of the series (500 rows here) rather than to the earliest *specified* bound. That
is the same `None`-means-open convention as the single-period form, and it is what
makes that period's `change` the full-history number. Passing `start_date`/`end_date`
**and** `periods` is a `ValueError`: mixing a range with a list of ranges invites a
caller to have one of them silently ignored.

### Two channels to the same number, and the cross-metric check

Metric 2 lays out the same fact: `cumulative_returns` over a range ends on
`close(last) / close(first) - 1`, which is this metric's `change` when the period *is*
the range. A test asserts `period_change.change == cumulative_returns.total` at
several bases rather than assuming it — the series and the endpoint are one fact seen
twice, and that is the only thing that makes the second computation worth having.

### What the database does — and what it deliberately does not

This metric has no `sql` member (see [Backends](#backends)): a period change is a
choice of two rows and a division, and the read boundary returns a series. The SQL form
that *would* be used is two index lookups, and the verification script runs it to show
the two agree:

```
-> Rows fetched before execution
-> Select #2 (subquery in projection; run only once)
    -> Limit: 1 row(s)
        -> Index range scan ... over (instrument_id = 13 AND '2024-02-01' <= trading_date)
-> Select #3 (subquery in projection; run only once)
    -> Limit: 1 row(s)
        -> Index range scan ... over (instrument_id = 13 AND trading_date <= '2024-03-14') (reverse)

two index lookups: 40.06720000 -> 41.84570000 = 0.0443879283, engine 0.04438793
```

Two seeks, no scan — and the engine still reads the period's rows instead, because
that buys `observations` (how many sessions the period held) and the same read serves
many periods. The trade-off is stated in both directions: the two-seek query is
cheaper for one long period, and it cannot tell you how many sessions were in it.

### Edge cases with a policy, not an accident

| Situation | Behaviour | Why |
|---|---|---|
| A bound that is not a session | Snaps inward: the base is the first session at or after it, the end the last at or before it; both are reported | A weekend has no price. The nearest session inside the period is what a report is quoting, and echoing it makes the snap visible. |
| A period with no session in it | `None` throughout, `observations` of `0`, counted as `undefined` | The base and end can cross when the period is narrower than a gap between sessions; there is then no pair of prices to divide. Same rule as an empty range. |
| A base of `0` | `change` is `None`; the sessions and both prices are still reported | `x / 0` is not a ratio — the same rule as a zero previous close in metric 1, and for the same reason: the schema permits it. |
| Start after end | `ValueError` | A caller's mistake, not data. The loader rejects it too, and quietly swapping the bounds would report a sign nobody asked for. |
| An open bound | `None` means the series' first or last row | "This year to date" has one bound. |
| An empty selection (`periods=()`) | No periods, and **no query** | Asking for nothing should not read the database. |
| An instrument with no prices | One period, `None`, counted as `undefined` | Same as every metric: a newly listed instrument is a normal state. |

## Metric 9 — rankings by return and trading volume

### Business meaning

Every metric so far answers a question about *one* instrument: how much did this
one move, how volatile was it, how far did it fall. A **ranking** answers a
question about a *set* of them — which instruments moved most, which traded
heaviest — and that is a different shape of question, not a different formula. The
report it serves is "where does each name stand, ordered", and the two measures
asked for are the two the other metrics already define:

* **by return** — the price change over the window, which is
  Metric 8 asked once
  per instrument;
* **by volume** — the shares traded over the window, which is the total from
  Metric 7 asked once per instrument.

What a ranking adds is the *comparison*: an order, a rank, and the universe the
comparison was made over. What it does not add is a recommendation. The engine
orders instruments by a measure it reports in full, and says nothing about which of
them to hold — an ordering is arithmetic, and the reader brings the judgement.

### Formula

With `v(i)` the measure for instrument `i`:

```
v(i)          = (close(last) - close(first)) / close(first)     -- by return
              = sum(volume(s) for s in window)                  -- by volume
rank(i)       = 1 + |{ j : v(j) is strictly better than v(i) }|
better        = greater, when descending (the default); smaller, when ascending
```

Two properties of that rank definition matter, and both are deliberate:

* **It is computed from the values alone.** A rank is not a position in a sorted
  list that a query happened to return; it is "how many instruments are strictly
  ahead of this one". Equal measures therefore share a rank and the next distinct
  measure skips the numbers they used — competition ranking, `1, 2, 2, 4` — and any
  two callers ranking the same numbers get the same answer whatever order the rows
  arrived in.
* **Ties are broken by symbol, not by chance.** The *order* of two tied rows is
  still deterministic (symbol ascending), so a report diffed between two runs is
  stable, even though their ranks are equal.

A row with no measurable value has `rank = None` and is listed after the ranked
rows. `universe`, `ranked`, `unranked` and `truncated` are on the result, because
"first of five" and "first of five hundred" are different statements.

### The window is a count of sessions, per instrument

`lookback` is the newest *n* **trading sessions of each instrument** — the engine's
observation-counting convention, applied across instruments rather than along one.
Instruments do not all trade on the same days, so counting sessions per instrument
is what makes the comparison fair: a name halted for a week is ranked on the
sessions it actually traded, and `observations` on its row says how many there were.

Three cases are worth stating, because each is easy to get wrong:

| Request | The window | What it means |
|---|---|---|
| `lookback=21`, no date bounds | each instrument's newest 21 sessions, whenever they were | "where does each name stand"; the read is each instrument's history, so a large universe wants a `start_date` |
| `lookback=21`, `start_date`/`end_date` | the newest 21 sessions *inside* those bounds | "the last 21 sessions of this period"; the read is bounded by the dates |
| `lookback=None` (default) | every observation between the bounds | "most active this month": totals over whatever each instrument traded in the range, with `observations` showing that the spans were equal |

**A window shorter than the `lookback` is not a window.** An instrument with fewer
than *n* observations in the range gets `value = None`, is listed unranked, and
reports the sessions it had — the same completeness rule the moving averages and the
volatility apply to their own windows (`sample_size == window`, or nothing), for the
same reason: a twenty-session total computed over three sessions is a different
statistic, and mixing the two in one list compares unlike things. A caller who wants
that rule enforced for every row passes a `lookback`; a caller who accepts ragged
spans passes `None` and reads `observations`.

A window of exactly **one** observation is a change of `0`, which is what
Metric 8 already says
about a one-session period. It is a true statement, and `observations` on the row is
what lets a reader see how narrow it was. It is also the one place where a single-session
"return" can enter a ranking, so a caller who does not want it passes `lookback >= 2`.

### Intermediate calculations

The standard three-instrument sample, on the five sessions `2024-03-11 .. 2024-03-15`,
measured over the whole range and over its newest two sessions:

| Instrument | Closes | Return, 5 sessions | Return, 2 sessions | Volume, 5 sessions | Volume, 2 sessions |
|---|---|---|---|---|---|
| AAPL | 100, 102, 101, 103, 104 | `(104 - 100) / 100 = 0.04000000` | `(104 - 103) / 103 = 0.00970874` | `5 x 1000 = 5000` | `2 x 1000 = 2000` |
| MSFT | 200, 190, 195, 205, 210 | `(210 - 200) / 200 = 0.05000000` | `(210 - 205) / 205 = 0.02439024` | `5 x 500 = 2500` | `2 x 500 = 1000` |
| GOOG | 50, 51, 52, 53, 54 | `(54 - 50) / 50 = 0.08000000` | `(54 - 53) / 53 = 0.01886792` | `5 x 2000 = 10000` | `2 x 2000 = 4000` |

which ranks, descending, as:

| Rank | By return, 5 sessions | By return, 2 sessions | By volume, 5 sessions | By volume, 2 sessions |
|---|---|---|---|---|
| 1 | GOOG `0.08000000` | MSFT `0.02439024` | GOOG `10000` | GOOG `4000` |
| 2 | MSFT `0.05000000` | GOOG `0.01886792` | AAPL `5000` | AAPL `2000` |
| 3 | AAPL `0.04000000` | AAPL `0.00970874` | MSFT `2500` | MSFT `1000` |

The window changes the *order* here, which is the point of getting it right: AAPL is
last over five sessions and last over two, but MSFT is second over five sessions and
**first** over two — the five-session total is dominated by its `200 → 190` fall, the
two-session window only sees `205 → 210`. Both are true; only one is the question.

A tie, to show the rank rule (values `0.05, 0.05, 0.02`, descending):

| Instrument | Value | Rank | Why |
|---|---|---|---|
| A | `0.05` | 1 | nothing ahead of it |
| B | `0.05` | 1 | one instrument equal, none strictly ahead |
| C | `0.02` | 3 | two strictly ahead — so the number 2 is skipped |

### The read: one query for the universe

A ranking must not become one query per instrument, so it does not use the
per-instrument loader at all. `load_price_cross_section()` reads every instrument's
observations **in one statement**, ordered by `(instrument_id, trading_date)`, and
returns them grouped — one `PriceSeries` per instrument, keyed by id. It is still
the same retrieval discipline (one query, only the requested columns, no warm-up),
with two departures that follow from reading many instruments at once:

* **The instrument table is joined**, for the one thing the caller cannot supply:
  the symbols. The per-instrument loader deliberately does not join it, because the
  caller holds the `Instrument`; a cross-section has no such instance. The join is on
  a primary key, so `EXPLAIN` shows a single-row index lookup per price row, and a
  second query for the symbols would cost more than the join.
* **The window is applied after the read**, because it is per instrument while the
  read is per range — see the window section above. `start_date`/`end_date` are
  therefore the hard bound on the work, and the only request that is not bounded by
  them is `lookback` with no dates, which reads each instrument's history.

The two backends differ only in how the window and the summary of it are taken:

| Backend | How the window is taken | Agreed on |
|---|---|---|
| `python` (default) | `PriceSeries.tail(n)` per instrument, then the shared arithmetic per row | — |
| `pandas` | one frame for the cross-section, `groupby("instrument_id").tail(n)`, then `size`/`first`/`last`/`sum` for every instrument at once | asserted row for row |

`pandas` vectorises the *window and its summary*, not the ratio: `Decimal` is not a
numpy dtype, and a float division would report a different number from the other
backend — the one thing a reference implementation may not do. This is the metric the
`pandas` backend was for: the work is a grouping, and one pass over a frame is what
grouping is good at.

### What the database does — and the window it declines

This is the metric whose SQL form looks most obvious and is not used, so the reason
is measured rather than asserted. The natural query is a partitioned row number in a
derived table, filtered to the newest *n* per instrument:

```sql
SELECT instrument_id, trading_date, close FROM (
    SELECT instrument_id, trading_date, close,
           ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY trading_date DESC) AS rn
    FROM marketdata_pricerecord WHERE trading_date BETWEEN '2024-02-01' AND '2024-03-14'
) AS ranked WHERE rn <= 31
```

It is legal, it is a real single-level window, and Django can express it — which is
exactly why it needed to be *run* to be rejected. Against the benchmark (100 seeded
instruments, 500 rows each, 31 sessions requested):

Both reads return the same rows — the verification script asserts that set equality,
not just the timings — and the row-number form is never the faster of the two. The
absolute milliseconds move between runs, so they are given as ranges:

| Read | Rows returned | Median |
|---|---|---|
| Range read, date-bounded (used) | 3,100 | 16–21 ms |
| Row-number read, date-bounded | 3,100 | 21–22 ms |
| Range read, no bounds | 50,000 | 74–83 ms |
| Row-number read, no bounds | 3,100 | 115–133 ms |

With a bound the two read the same rows and the window is pure overhead: a few tens
of per cent of *extra* work for nothing. **Without** one it does ship sixteen times
fewer rows and is *still* around 60% slower, for the same reason in both cases —
`EXPLAIN` shows the window must `Materialize` and sort each partition before
`rn <= 31` can filter it, so the database pays to look at every row it is trying not
to return. There is no shape in which it wins, which is why `RANKING_BACKENDS` has no
`sql` member: the fourth metric without one, and the only one declined on a
stopwatch rather than an impossibility. Read the shapes, not the digits, and
reproduce with `scripts/verify_analytics.py`.

The read that *is* used, for the whole universe:

```
-> Nested loop inner join  (cost=2480 rows=3100)
    -> Sort: marketdata_pricerecord.instrument_id, marketdata_pricerecord.trading_date
        -> Index range scan on marketdata_pricerecord using idx_price_trading_date
           over ('2024-02-01' <= trading_date <= '2024-03-14')
    -> Single-row index lookup on instruments_instrument using PRIMARY
       (id = marketdata_pricerecord.instrument_id)
```

One index range scan over the dates, one primary-key lookup per row for the symbol,
one sort to group them. 100 instruments and 3,100 rows come back in **one** query —
which is the property the metric had to have, since the alternative is one query per
instrument and a universe is not a fixed size.

On the seeded benchmark, the two rankings both cover all 100 instruments (every one
of them has the full 31 sessions), and the top rows are:

```
  return: universe 100, ranked 100, unranked 0, lookback 31 sessions
       1 BENCH0076   0.14722107 obs=31 2024-02-01 .. 2024-03-14
       2 BENCH0081   0.11692459 obs=31 2024-02-01 .. 2024-03-14
       3 BENCH0022   0.10895026 obs=31 2024-02-01 .. 2024-03-14
    ...
     100 BENCH0082  -0.08894698
  volume: universe 100, ranked 100, unranked 0, lookback 31 sessions
       1 BENCH0010      94440317 obs=31 2024-02-01 .. 2024-03-14
       2 BENCH0091      93725427 obs=31 2024-02-01 .. 2024-03-14
       3 BENCH0006      91093470 obs=31 2024-02-01 .. 2024-03-14
```

**The cross-metric check.** Each row's measure is the metric the engine already has,
not a second definition of it, and the verification script asserts the equality on
live data: the top return row's `0.14722107` is exactly what
`percentage_change_for_instrument()` reports between that row's own `first_date` and
`last_date`, and the top volume row's `94440317` is exactly
`volume_summaries_for_instrument().summary.total_volume` over the same range. A
ranking is a comparison; what is being compared is defined once.

### Edge cases with a policy, not an accident

| Situation | Behaviour | Why |
|---|---|---|
| A window shorter than the `lookback` | Listed unranked, `value = None`, `observations` reports the sessions it had | A partial window is a different statistic — see the window section |
| A one-observation window | A return of `0`, ranked, `observations = 1` | Metric 8's rule for a one-session period; the count is on the row |
| A base price of zero | Listed unranked, `value = None`, both prices and the session count still reported | `(close - 0) / 0` is not a ratio — the same rule as a zero previous close in a daily return |
| An instrument with no prices in the window | Not in the universe | A ranking is over prices; `universe` is a fact about the data, not the instrument table |
| A tie on the measure | Equal ranks, `1, 2, 2, 4`; order between the tied rows is by symbol | The rank comes from the values, so it cannot depend on row order |
| `limit` cutting through the list | Returned rows keep their **absolute** ranks, and `truncated` counts what was left out | A page of a ranking must not renumber its rows |
| An empty universe | `universe = 0`, `rows = ()`, one query, no error | A range with no prices is a normal state, not a failure |
| `descending=False` | The same code with the direction flipped: the losers, or the quietest sessions | One implementation, two readings — they cannot drift apart |
| An unknown measure or backend | Rejected by name, listing what is known | A typo must not silently become a default |

## HTTP surface

Two read-only endpoints, both under `/api/v1/analytics/`:

| Endpoint | Answers |
|---|---|
| `GET /instruments/<id>/series/?metric=...` | One metric of one instrument, computed on demand |
| `GET /rankings/?measure=...` | Instruments compared to one another over one window |

The view is an **adapter and nothing else**: it parses the query parameters, calls
exactly one service, and serializes the frozen result. There is no arithmetic in it
and no query construction, so a wrong number is wrong in one place with one test.
The strongest test of the surface is therefore the round trip — for each of the eight
per-instrument metrics the rendered body is asserted equal to the serializer's
rendering of the direct service call — which is what makes "the endpoint is the
engine" a fact rather than a claim.

**The envelope is the result.** The serializers read the dataclass fields, including
its properties: `undefined`, `total`, `windows`, `unranked` and `truncated` come from
the result that computed them rather than being derived a second time. The
per-instrument results share `MetricResultSerializer` (`symbol`, `backend`,
`start_date`, `end_date`, `padding`, `undefined`); the ranking stands outside it,
because a ranking is *about* symbols rather than belonging to one — the same reason
`Ranking` is not a `MetricResult` in the service layer.

**Rendering is a decision here, not in a metric.** Money and ratios are written at
eight places — the width of `AnalyticSnapshot.value` — so a ratio computed on demand
and a price read from the store render alike, and the payload does not depend on which
driver answered: a close is `Decimal('104')` from SQLite and `Decimal('104.00000000')`
from MySQL, and both are written `104.00000000`. Counts are written whole. A ranking's
`value` keeps the precision its measure carries — eight places under `return`, whole
under `volume`. Every number is a JSON **string**, so no float ever stands in for an
exact decimal; a missing value is `null`, and its count travels beside it in
`undefined`. (Money is passed through a metric at whatever precision the store holds
— see *Money is `Decimal`* above — precisely so this choice lives in one place.)

**There is no pagination, deliberately.** A windowed metric cannot be paged without
recomputing its warm-up for every page, and the envelope describes the series as a
whole, so the payload is one object whose `points` are the series. The ranking's
`limit` truncates *after* ranking, so the rows that come back keep their absolute
places rather than being renumbered as a page.

**Defaults come from the engine, not from the edge.** An omitted `start_date` or
`end_date` is no bound — the instrument's whole stored history — which is also why a
whole-history return series begins with one `null`: the first session of a history has
no predecessor to return from, and that is a fact about the data rather than a
warm-up row that was dropped. An omitted `backend` is the backend that exists for that
metric (`sql` where there is one, else `python`); an omitted `window` is 30 sessions;
an omitted `names` is all three conventional averages; an omitted `periods` is one
period spanning the requested range.

**A parameter the metric does not take is a 400, not a no-op.** These endpoints are
the one place in this API that reject an unknown query parameter — including one
belonging to a *different* metric, like `?metric=moving_averages&window=7`. The rest of
the API ignores what it does not understand; a computed answer that silently ignored a
window would look exactly like one that honoured it, which is the trap the README
documents for `is_active`. The engine's own `ValueError`s (an unknown backend, an
inverted range, a window below 2, a period passed alongside the dates it excludes) are
mapped to `400` rather than allowed to become a `500`.

**Nothing is cached.** A computed series depends on price rows, and a price write does
not bump the analytics cache namespace, so caching it would risk serving a series
computed before the newest session. The existing response cache covers the *stored*
snapshot list, which is invalidated by its own writes.

## Backend timings, measured

Median of 25 warm calls per cell against MySQL, on the benchmark instrument: 500
rows of history, 31 observations requested, a 30-observation window where the metric
takes one (and all three named averages where the metric takes three). Reproduce with
`scripts/verify_analytics.py`; absolute values drift with machine load, so read the
ordering and the gaps rather than the digits.

| Metric | `python` | `sql` | `pandas` |
|---|---|---|---|
| Period change (one period) | **0.63 ms** | — | 1.09 ms |
| Maximum drawdown | **0.68 ms** | 1.44 ms | 0.93 ms |
| Cumulative returns | **0.72 ms** | — | 1.40 ms |
| Volume summaries | **0.79 ms** | 1.16 ms | 1.04 ms |
| Price extremes | **0.81 ms** | 1.73 ms | 0.99 ms |
| Daily returns | **1.96 ms** | 2.13 ms | 2.20 ms |
| Moving averages (7/30/90, one read) | **2.29 ms** | 4.32 ms | 4.24 ms |
| Rolling volatility, window 30 | **3.01 ms** | 3.67 ms | 3.99 ms |

The ranking is the one cross-sectional row — a different shape, so it is measured
separately: 100 seeded instruments, 31 sessions each, the whole universe in one
query, and the figure covers the read, every instrument's measure, and the sort.

| Ranking | `python` | `pandas` |
|---|---|---|
| By return (100 instruments × 31 sessions) | **20 ms** | 25 ms |
| By volume | **21 ms** | 27 ms |

There is no `sql` column because there is no `sql` backend — the measurement that
declined it is in [Backends](#backends). `pandas` loses here for the same reason it
loses every other row, stated plainly because this is the row it was built for: what
it amortises is the frame, and constructing a frame for 3,100 rows from Python values
costs more than 100 slices of an already-materialised list. What it does do is the
*grouping* — one trailing window per instrument, in one pass — which is the shape of
the metric; the frame cost is the price of that shape at this size, and the number to
watch as a universe grows rather than a claim that it wins.

What the table says, honestly:

* **The Python backend wins every row at this size, and `sql` is still the default
  wherever it exists.** This is the trade-off being stated rather than hidden: a
  single instrument's rows are in memory either way, so the arithmetic is the same
  work, and the database path adds either an extra column (`LAG`) or a buffer and a
  temporary table (`AVG() OVER`, `MAX() OVER`). The SQL path is the default because
  it is the one that scales in the direction this engine is built to grow — many
  instruments, and windows consumed inside the database — and because a metric whose
  canonical form is a SQL window should not have a Python default that quietly
  becomes the only implementation.
* **The moving averages show the widest gap (2.29 vs 4.32 ms)** because their SQL form
  is the only one that makes the database build a temporary table *per frame*: a frame
  aggregate cannot be streamed, and three of them are nested. The Python path is three
  passes over rows already in memory, with no allocation. Asking for all three in one
  query is still cheaper than three calls — one sort, 120 rows, one round trip — but
  the gap against Python is the price of pushing the frames down.
* **Volatility is the one row where the gap nearly closes** (`sql` 3.67 vs `python`
  3.01): both compute the deviation in Python, so the SQL backend's extra work is just
  `LAG` instead of a list shift, and the deviation dominates the total. That is the
  clearest statement of what the `sql` backend actually buys here — the *returns*,
  not the statistic.
* **Period change is the cheapest metric** (0.63 ms) and has no SQL member: it reads
  no warm-up row, returns one number per period, and several periods share the read.
  **Cumulative returns is next** (0.72 ms), also without a SQL member: it reads no
  warm-up row and computes a ratio, so the request is one query returning exactly the
  requested range.
* **Maximum drawdown is the cheapest metric with a `sql` member** (1.44 ms against
  Python's 0.68 ms), and it is the clearest case of what the SQL path costs at this
  size: the whole scan is pushed into the database, a `ROWS BETWEEN UNBOUNDED
  PRECEDING` frame over 31 rows, buffered through a temporary table to return one
  float per row that Python then divides. `pandas` sits between the two because
  `cummax` is genuinely vectorised — this is the one metric where it beats `sql`.
* **Price extremes is the slowest `sql` row relative to its Python form** (1.73 vs
  0.81 ms, roughly double) because it asks *two* questions of the same frame: MySQL
  materialises the running minimum over the temporary table of the running maximum,
  so the row pays two buffered stages rather than one. The Python scan answers both
  in a single pass. It is one of the two metrics that read columns other than
  `close`, and the only one that reads two of them (`high` and `low`).
* **Volume summaries is the cheapest `sql` row after the drawdown** (1.16 ms against
  Python's 0.79 ms) and needs **one** buffered stage rather than the extremes' two,
  because it asks one question of the frame — a running sum — rather than two
  extremes. It is the other metric that reads a column other than `close` (`volume`),
  and the window sums that same column rather than adding a read. Its `pandas` cost
  is close to Python's (1.04 ms) because `cumsum` is a single vectorised pass, with
  no second allocation.
* **`pandas` is the slowest per instrument, and it is not a per-instrument backend.**
  Its cost is frame construction plus a vectorised call over 500 rows, and the
  construction is a fixed cost per call rather than a per-row one. That is why it is
  here: it is the independent reference every other backend is asserted against, and
  it is the grouping backend — the ranking is the metric that groups, and the frame
  is where that happens. At 100 instruments it is still behind Python (see the
  ranking table), which is the honest reading: the frame is a cost as well as an
  instrument, and this engine's per-request size is small. Import cost is paid once
  per process (284 ms for the first call, ~1 ms each afterwards).

## How computation changes as the dataset grows

| Growth | Effect | Why |
|---|---|---|
| More rows per instrument (longer history) | The loader's range scan is bounded by the requested range, not the instrument's life: `pad + observations in range`. A 20-year history costs the same per request as a 1-year one. | The bounds are pushed into `WHERE`, which uses `uniq_price_instrument_trading_date`. Warm-up padding adds `pad` rows — 1 for a daily return, the longest window minus 1 for the named moving averages, `window` for a volatility, and 0 for cumulative returns, maximum drawdown, price extremes, volume summaries and period change, whose anchors are inside the range — a constant per request, independent of how much history exists. The ranking has no `pad` either, and for a different reason: its window is per instrument, so it is a slice taken after the read. |
| More instruments | Per-request cost is unchanged for every per-instrument metric: one instrument, one query. The ranking across instruments is the case that changes the shape, and it still does not become one query per instrument. | `load_price_cross_section()` reads the whole universe in one statement — an index range scan over the dates plus a primary-key lookup per row for the symbol — and groups it in memory. One instrument's request is unaffected by how many others exist; a ranking's grows with the universe it names, and its read is bounded by `start_date`/`end_date` (the one exception being a `lookback` with no dates, which reads the histories it is ranking). |
| More metrics on the same series | Free if they share the loader call. Each additional metric over the same rows adds arithmetic, not queries. | The loader is the only thing that reads, and it returns every column a metric asked for. The three named moving averages are the case it was built for: one read, three columns, three means. Two rankings by different measures are *not* free — each has its own measure to compute and may need a different column (`volume`) — but they are still one read each. |
| More concurrent readers | Linear in requests, with no shared mutable state in the engine. | The engine writes nothing. The API's response cache (see [api_optimisation.md](api_optimisation.md)) covers the snapshot reads, not the computed series, which depends on the requested range. |
| A very long single series read whole | Becomes memory-bound, not query-bound, and is not the shape this engine is designed for: a range is what bounds the work. | If a full-history export is needed, that is an offline job over a DataFrame, not a request. |
| A ranking with a `lookback` and no date bounds | Reads each instrument's whole history — the metric's one unbounded shape. | `lookback` says how many sessions to *measure*, not how many to *read*: the read is bounded only by dates. A caller ranking a large universe passes `start_date`, or accepts that the read is the histories. This is "a range is what bounds the work" read in the other direction. |

## Tests

`apps/analytics/tests/test_series.py` — the read boundary, 56 tests:

* the whole series in exactly one query, in date order (query count pinned);
* only the requested columns are selected — `source`, `ingestion_job_id`,
  `created_at` and `adjusted_close` are asserted absent from the SQL, and the
  instrument table is asserted not to be joined;
* warm-up: one row before the range is read, `pad` larger than the available
  history still returns every requested row, `pad` without a `start_date` is a
  no-op, and the window does not lag across instruments;
* the window columns: `previous_close` is the previous session, the moving-average
  columns are named after their windows and each is the trailing mean, the running-max
  and running-min columns are the highest and lowest close so far *within the rows
  returned* (so the date range anchors them), the running-total column is the `volume`
  traded so far with the same in-range anchor and shares the read with a window, the
  one-direction and two-direction reads are each a single query, **several
  moving-average windows come back from one query**, a database
  partial-frame mean is passed through as data (the metric applies the completeness
  rule), and a window below 1 is rejected;
* `PriceSeries` invariants: repeated or out-of-order dates raise, `in_range()`
  drops warm-up from every column, a missing column names what was loaded,
  `tail(n)` takes the newest observations and keeps the warm-up count that
  survived the cut;
* the database refuses a second row for one instrument and day;
* the cross-section (the ranking's read): the whole universe in **one** query,
  grouped by instrument and oldest first, keyed by id and carrying the symbol, the
  symbol **joined** rather than fetched separately, the same only-the-requested-columns
  assertion, the instrument filter, the date bounds, an instrument with no
  observations in the range absent rather than empty, an empty universe, `tail` off
  each series, unknown fields and a reversed range rejected, no warm-up, and the frame
  built with one row per observation and the id as an ordinary column.

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

`apps/analytics/tests/test_cumulative_returns.py` — the chain, 20 tests:

* the growth and return of the hand-computed series, and that the base itself is
  `0` rather than `null` (nothing has happened yet, which is a value);
* the identity that makes the two definitions one: the compounded product of the
  *reported* returns reproduces the growth within `n x 5e-9`;
* the break rule: a zero previous close leaves `None` from that date onwards, a
  series starting at zero is defined at the base and nowhere after, and a broken
  chain has no `total`;
* the two backends agree, including on a series whose chain breaks;
* the service: the base is the first *requested* date (starting at DAY3, the chain
  starts at `0` on DAY3), `padding` is always `0`, the total equals the
  `EXPECTED_RETURN` fixture for all three sample instruments, and `backend="sql"`
  is rejected by name because there is no such backend.

`apps/analytics/tests/test_moving_average.py` — the named series and the window, 34
tests:

* the vocabulary: three names in the conventional order, a selection answered in that
  order however it was asked for, a repeated name computed once, an empty selection
  empty, and an unknown name rejected with the list of known ones;
* the hand-computed series, the `5/3` rounding, a window of 1 (the price itself),
  and a window exactly as long as the series (one value, on the last date);
* incompleteness: a window longer than the history is `None` for every date with
  `sample_size` counting up, rather than a partial mean;
* the running sum is asserted **exactly** equal to a fresh per-window sum, on
  prices with no exact binary representation — the claim that `Decimal` makes the
  `O(n)` shortcut exact;
* supplied (database) means are used, obey the same completeness rule, and agree
  with the computed ones when they are the true means;
* the three backends agree on **every named series**, including when the history is
  shorter than the window (where a `ROWS BETWEEN` frame would quietly return a partial
  mean);
* the service: all three series and one query by default, the 7-session series'
  hand-computed means on a twelve-session series, the pad being `max(window) - 1` — a
  request for the 7-session series alone still reads past the 30-session pad, shown by
  two paddings differing on a twelve-row history — the first requested date carrying a
  value, the same dates read without warm-up leaving holes, the window counting
  observations rather than calendar days (seven sessions with a three-day hole), a
  subset selection, an empty selection that reads nothing, lookup by name, an empty
  instrument, and name/backend validation.

`apps/analytics/tests/test_rolling_volatility.py` — the deviation, 21 tests:

* three hand-computed cases: exactly `0.01` for returns 1/2/3%, `0.1 x sqrt(2)`
  for `+10%`/`-10%` written as the assertion that the divisor is `window - 1` and
  not `window`, and exactly `0` for a flat window;
* the annualizer: the value is the daily figure times `sqrt(252)`, within the
  quantum carried through it, and `periods_per_year` is a parameter (a test uses
  52 and expects `0.1 x sqrt(104)`);
* incompleteness: an undefined return makes every window containing it `None`,
  with `sample_size` reporting how many defined returns there were; too little
  history is `None`, not a short window; a window below 2 is rejected;
* reproducibility: the deviation is recomputed by hand from metric 1's *reported*
  returns and compared, so a window's sigma can be recovered from the payload;
* the three backends agree point for point, and across the warm-up boundary where
  an off-by-one in `pad` would show (`padding == window`, exact expected values);
* the service: the window and annualizer are echoed back, one query per request,
  the young-series holes, a zero close, a flat series (zero volatility on every
  covered date), an empty instrument, and backend validation.

`apps/analytics/tests/test_maximum_drawdown.py` — the peak, 25 tests:

* the running maximum and the decline it produces, row by row, on a six-session
  series with several episodes, and the summary episode recovered from them;
* every point names the peak it is measured from, so any row is checkable and not
  only the worst one;
* a new high is a `0` drawdown rather than a missing one, and a tie does not
  restart the peak date;
* a zero peak is `None` (`close / 0`), and the metric resumes at the next close
  above zero; a supplied peak of zero obeys the same rule;
* supplied (database) peaks are used, obey the length check, and agree with the
  scan when they are the running maximum;
* the summary: an unrecovered decline, a series that never fell (`0`, no dates, no
  `recovered`), equal-depth episodes resolving to the earliest, and an all-`None`
  series summarising to all-`None`;
* the three backends agree on the points *and* on the summary, including on a
  series with a zero peak;
* the service: the range's own peak as the difference between two requests (the
  whole series against one starting after the high), `padding == 0` and no warm-up
  row read, one query, the date bounds, a four-day gap spanned by observation, and
  backend validation.

`apps/analytics/tests/test_price_extremes.py` — the running high and low, 30 tests:

* the running extremes and the new-high/new-low flags, row by row, on a series whose
  traded high/low are chosen to differ from its closes, and the summary built from
  them;
* every point's extreme carries the date it was first reached, and its last row is
  the summary's close pair;
* the first point is both the highest and the lowest close so far, a repeated extreme
  is not a new one, and a tie reports the earliest date;
* supplied (database) extremes are used, obey the length checks, agree with the scan
  when they are the running extremes, and cannot turn a non-event into a flag;
* the summary: the close pair equals a direct `max`/`min` of the closes, the traded
  pair comes from the `high`/`low` columns, a close-only series has no traded pair,
  `traded_range` is a width no single row spans, and an empty series summarises to
  all-`None`;
* the two readings disagree on purpose — the highest close and the highest high fall
  on different dates, and the lowest close is not the lowest low;
* the three backends agree on the points *and* the summary, including where the
  traded pair differs from the close pair;
* the service: the range's own extremes as the difference between two requests,
  `padding == 0`, one query, the date bounds, `undefined == 0` (the overridden count),
  the traded pair read from the columns, an empty instrument, and backend validation.

`apps/analytics/tests/test_volume_summaries.py` — the running total and the period's
figures, 30 tests:

* the running total and the relative volume, row by row, against the table above —
  including the zero-volume session, which is a `0.00000000` ratio rather than a
  missing one;
* the ratio is against the mean *so far* and not the period's mean, shown by the one
  series where the two rules give different answers on four of five rows;
* volumes are normalised to counts (a driver's `DECIMAL('100.00000000')` renders as
  `100`), a supplied running total is the one used, and supplied totals that *are* the
  running total agree with the scan;
* the `pandas` reference is exact past the float integer limit — a `2**53` total is
  asserted equal to the `Decimal` scan, and the float path that would have dropped a
  share is recorded in the test;
* length mismatches are rejected and an empty series is an empty answer;
* the summary: the hand-computed period figures, the exact-mean classification (the
  2-and-3 case that a rounded average would get wrong), a constant-volume series at
  its own average on every session, and the three classification counts adding up to
  `observations` across six datasets;
* a zero-volume session lowers the mean rather than being dropped, ties report the
  first session that reached the extreme, and an all-zero series has a total of zero
  with every session at average;
* the three backends agree on the points *and* the summary, on the sample series and
  on a series containing a zero-volume session;
* the service: `padding == 0` and no warm-up row, the range's own total rather than the
  history's, one query, the date bounds, the summary total equal to the last running
  total, a zero-volume opening session counted as `undefined`, an empty instrument, and
  backend validation.

`apps/analytics/tests/test_percentage_change.py` — the period, 37 tests:

* the five hand-computed periods in the table above, plus a signed loss, a
  one-session period as `0`, and an empty series;
* `percent_change` is asserted **exactly** `change x 100` for every period shape,
  because it is derived from the reported ratio rather than recomputed from the prices;
* snapping: a Saturday-to-Tuesday bound settles on Monday and reports both, an end past
  the last session reports the last one, and a period with no observation in it is
  `None` throughout with `observations` of `0` — including the case where the base and
  the end cross because the period is narrower than a gap;
* a base of `0` has no ratio while its sessions and both prices are still reported;
* reversed bounds and mismatched lengths are rejected;
* `pandas` agrees with `python` on a table of cases (labels, open bounds, exact bounds,
  weekend bounds, an empty period, an empty series) and rejects the same reversed
  period;
* the service: the whole range's number, **the cross-metric check** that
  `change == cumulative_returns.total` at several bases, a period that is not the
  range, the span's interior not entering the answer, one period as one query, four
  labelled periods as one query with their labels and values, the union read for
  several periods, an open bound opening the read, both forms together rejected, an
  empty selection that reads nothing, an empty instrument counted as `undefined`, an
  observation count rather than a calendar span on a gapped instrument, and the absent
  `sql` backend rejected by name.

`apps/analytics/tests/test_rankings.py` — the cross-section, 53 tests:

* the rank arithmetic with no database at all: descending and ascending from the same
  code, competition ranks (`1, 2, 2, 4`), the rank asserted as `1 + the number strictly
  better` for every row, an order that does not depend on the order the points arrived
  in, ties broken by symbol, unmeasurable points ranked `None` and listed last rather
  than dropped, `limit` truncating without renumbering, a negative limit rejected, and
  the inputs of the measure (id, observations, both dates) coming back on the row;
* the entry point against the sample: the return ranking `GOOG > MSFT > AAPL` at
  0.08/0.05/0.04 and the volume ranking `GOOG > AAPL > MSFT` at 10000/5000/2500;
* the measures are the metrics the engine already has, asserted directly: each row's
  volume equals `volume_summaries_for_instrument().summary.total_volume` and each row's
  return equals `percentage_change_for_instrument()` over that row's own bounds;
* both backends agree **row for row** on both measures, and a request is **one** query;
* the window: `lookback=2` re-orders the sample (`MSFT` first) with the hand-computed
  two-session values, the volume total windows with it, a `lookback` longer than the
  history leaves every instrument unranked with its `observations`, and a
  one-observation window is a return of `0`;
* the range bounds the eligible observations, and the window is per instrument — an
  instrument with a gap is measured over *its* newest two sessions and reports the two
  dates it used;
* the universe: an instrument filter, an unknown id absent rather than empty, an
  instrument with no prices in the range absent, no prices at all as an empty ranking,
  and the universe counted against `COUNT(DISTINCT instrument_id)` on live data;
* a single-observation instrument still listed with its count, a zero base price
  unranked with both prices reported, two equal returns sharing a rank, `limit` keeping
  absolute ranks and `truncated`/`unranked` counting past it, lookup by symbol, and
  `Ranking` asserted not to be a `MetricResult` (a ranking is about symbols, so it has
  no symbol of its own);
* validation: an unknown measure rejected with the vocabulary, an unknown backend
  rejected, `sql` rejected by name because there is no such backend, a `lookback` below
  1 and a negative `limit` rejected, and a reversed range rejected.

## Deliberately not done yet

* **No snapshot recalculation job yet.** `AnalyticSnapshot` already models the
  grain — `(instrument, as_of_date, metric_type, lookback_days)` — and the named moving
  averages (`moving_average` with `lookback_days` 7/30/90), volatility, the period
  change and a ranking's return are its natural occupants, since a stored value is how
  "what did we say on 12 March" becomes answerable and how a ranking by return becomes
  one indexed read of one row per instrument instead of reading every instrument's
  prices. The other measures would need new `MetricType` choices, the same gap metrics
  6, 7 and 8 have. It is the next structural piece.
* **No caching of computed series.** The range is part of the key, so a cache would
  multiply keys to save milliseconds over rows that are already a single indexed
  scan. The existing response cache covers the stored snapshot list.
* **No total-return variant.** These metrics read `close`. A total-return series
  would read `adjusted_close` through the loader's `fields` parameter, which exists
  for exactly this; the arithmetic above does not change, only the column.

## Reproducing the verification

```powershell
# Unit and integration tests (SQLite in memory, cache off)
./venv/Scripts/python.exe -m pytest apps/analytics -q -p no:cacheprovider

# Full suite
./venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

The MySQL checks in this document run read-only against the development database
and the benchmark instrument seeded by `scripts/bench_api.py` — 500 rows of
history, of which the request asks for a 31-observation window. They are all in
`scripts/verify_analytics.py`, which asserts, per metric, that the request is a
single query and that the backends agree point for point, and prints the plans and
the engine-versus-database comparisons quoted above — including the two
`STDDEV_SAMP` and derived-table `MIN` cross-checks, the four-aggregate extremes
check, the volume summary against the database's own `SUM`/`AVG`/`MAX`/`MIN`/`COUNT`,
the period change against the database's own two index lookups (with the cross-check
that it equals the cumulative return over the same range), the three frames carried in
the one moving-average statement, the two nested-window rejections that explain why
two of the four metrics without a SQL member lack one, and the ranking: the whole
universe as one read, each row's measure asserted equal to the per-instrument metric it
reuses, and the row-number form that `RANKING_BACKENDS` declines run **twice** — bounded
and unbounded — to show it selects the same rows and is never the faster of the two.

```powershell
./venv/Scripts/python.exe scripts/verify_analytics.py --rounds 30
```

`EXPLAIN FORMAT=TREE` for a `pad = 1` daily-returns request:

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
