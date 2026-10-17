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
| 3 | Moving averages (7/30/90) | implemented |
| 4 | Rolling volatility | implemented |
| 5 | Maximum drawdown | implemented |
| 6 | Highest and lowest prices | next |
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

Both columns are right. "20-day volatility" really is the standard deviation of
twenty daily returns, and "20-day average" really is the mean of twenty closes;
they differ by one observation, and every response carries `sample_size` (or the
`window` it was asked for) so the difference is visible rather than assumed.

All windows are **trailing and right-aligned** — a value dated `t` uses no price
after `t` — and all of them count **observations**, not calendar days, so a
holiday does not shorten a window and a gap does not become one of its members.
`pad` is `window - 1` for a moving average, `window` for a volatility (which
needs one more close than return), and `1` for a daily return. It is `0` for
cumulative returns and maximum drawdown, the two metrics whose anchor is inside
the range: see their sections.

### Backends

Every metric exposes the same interchangeable backends, and every backend returns
the same points, asserted row for row:

| Metric | `sql` | `python` | `pandas` |
|---|---|---|---|
| Daily returns | `LAG(close)` | list shift | `Series.shift(1)` |
| Moving average | `AVG(close) OVER (...)` | running sum | `Series.rolling` |
| Rolling volatility | returns from `LAG`, deviation in Python | returns from a shift, deviation in Python | `Series.rolling().std()` |
| Cumulative returns | — | direct ratio | direct ratio, vectorised |
| Maximum drawdown | running maximum from `MAX(close) OVER (...)`, ratio in Python | running maximum scan | `Series.cummax()` |

Two of the five have no `sql` member, and both for the same reason: **a metric
whose window must be computed from *windowed values* cannot be expressed in one
SQL query**, because a window function may not appear inside another window
function's arguments. Cumulative returns need a *product* (no dialect has a
windowed product), and rolling volatility needs the deviation of the *returns*,
which are themselves `LAG`s. MySQL states the second one outright:

```
OperationalError: (3593, "You cannot use the window function 'lag' in this context.'")
```

Maximum drawdown is the test of that rule rather than an exception to it: its
window is over the *closes*, one legal frame, so the database can carry the whole
scan — and only the ratio on top of it is computed in Python. Asking the same
question of a nested window is what fails, which is why the verification script
writes it as a derived table to get a database-computed cross-check:

```
OperationalError: (3593, "You cannot use the window function 'max' in this context.'")
```

So the engine does not offer a SQL member it cannot honestly implement. The two
without one are computed in Python over rows a single query already returned —
which is why the choice costs nothing in queries, only in where the arithmetic
runs. The engine's
canonical path stays inside the database wherever a window is a real window.

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

Closes 10, 12, 11, 13, 15, window 3:

| Trading day | Close | Window | Calculation | Mean |
|---|---|---|---|---|
| 2024-03-11 | 10 | 10 | not full (1 of 3) | `null` |
| 2024-03-12 | 12 | 10, 12 | not full (2 of 3) | `null` |
| 2024-03-13 | 11 | 10, 12, 11 | `33 / 3` | `11.00000000` |
| 2024-03-14 | 13 | 12, 11, 13 | `36 / 3` | `12.00000000` |
| 2024-03-15 | 15 | 11, 13, 15 | `39 / 3` | `13.00000000` |

A mean that needs more than eight places, closes 1, 2, 2 with window 3:
`5 / 3 = 1.66666…` → `1.66666667` (half up).

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
the loader changes: `pad=window-1` and the same single query. On the benchmark
instrument a `window=30` request resolves the boundary 29 rows back and reads 60
rows — 29 warm-up plus 31 requested — in one round trip. A test asserts the padding
is exactly `window - 1` and that the first requested date carries a value.

### What the database does with the window

The `sql` backend pushes the whole mean into the query:

```sql
AVG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date
                 ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS moving_average
```

and `EXPLAIN FORMAT=TREE` on it, against MySQL, reads:

```
-> Sort: marketdata_pricerecord.trading_date
    -> Table scan on <temporary>  (cost=13.9..17.1 rows=60)
        -> Temporary table
            -> Window aggregate with buffering:
                 avg(marketdata_pricerecord.close) OVER (
                   PARTITION BY instrument_id ORDER BY trading_date
                   ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
                -> Filter: (trading_date >= <cache>(coalesce((select #2),(select #3))))
                    -> Index range scan ... over (instrument_id = 13 AND
                         '2023-12-22' <= trading_date <= '2024-03-14')
                    -> Select #2 (subquery in condition; run only once)
                        -> Limit/Offset: 1/28 row(s)
                    -> Select #3 (subquery in condition; run only once)
```

Three things in that plan are the design, visible rather than asserted: the frame
is `29 PRECEDING AND CURRENT ROW`; the boundary subquery is `Limit/Offset: 1/28`,
i.e. the 29th prior date, computed once (`run only once`) and cached; and the index
range scan *starts* at the resolved warm-up date (`2023-12-22`), so the padding is
part of the range rather than a filter applied to a result.

The honest cost is the first line: MySQL implements the window with a **buffer and a
temporary table**, because a frame aggregate is not streamable in general. That is
work the Python rolling sum does not do — it is a single pass over rows that are
already in memory — which is the trade-off the timing table below records.

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

## Backend timings, measured

Median of 25 warm calls per cell against MySQL, on the benchmark instrument: 500
rows of history, 31 observations requested, a 30-observation window where the metric
takes one. Reproduce with `scripts/verify_analytics.py`; absolute values drift with
machine load, so read the ordering and the gaps rather than the digits.

| Metric | `python` | `sql` | `pandas` |
|---|---|---|---|
| Cumulative returns | **0.67 ms** | — | 1.24 ms |
| Maximum drawdown | **0.66 ms** | 1.18 ms | 0.85 ms |
| Daily returns | **1.52 ms** | 2.22 ms | 2.22 ms |
| Moving average, window 30 | **1.52 ms** | 2.19 ms | 2.95 ms |
| Rolling volatility, window 30 | **3.25 ms** | 3.34 ms | 3.96 ms |

What the table says, honestly:

* **The Python backend wins every row at this size, and the default is still `sql`
  for three of the four metrics.** This is the trade-off being stated rather than
  hidden: a single instrument's rows are in memory either way, so the arithmetic is
  the same work, and the database path adds either an extra column (`LAG`) or a
  buffer and a temporary table (`AVG() OVER`). The SQL path is the default because it
  is the one that scales in the direction this engine is built to grow — many
  instruments, and windows consumed inside the database — and because a metric whose
  canonical form is a SQL window should not have a Python default that quietly
  becomes the only implementation.
* **The moving average shows the widest gap (1.52 vs 2.19 ms)** because its SQL form
  is the only one that makes the database build a temporary table: a frame aggregate
  cannot be streamed. The Python rolling sum is a single pass with no allocation.
* **Volatility is the one row where the gap closes** (`sql` 3.34 vs `python` 3.25):
  both compute the deviation in Python, so the SQL backend's extra work is just
  `LAG` instead of a list shift, and the deviation dominates the total. That is the
  clearest statement of what the `sql` backend actually buys here — the *returns*,
  not the statistic.
* **Cumulative returns is the cheapest metric** (0.67 ms) and has no SQL member: it
  reads no warm-up row and computes a ratio, so the request is one query returning
  exactly the requested range.
* **Maximum drawdown is the cheapest metric with a `sql` member** (1.18 ms against
  Python's 0.66 ms), and it is the clearest case of what the SQL path costs at this
  size: the whole scan is pushed into the database, a `ROWS BETWEEN UNBOUNDED
  PRECEDING` frame over 31 rows, buffered through a temporary table to return one
  float per row that Python then divides. `pandas` sits between the two because
  `cummax` is genuinely vectorised — this is the one metric where it beats `sql`.
* **`pandas` is the slowest per instrument and that is not its use case.** Its cost
  is Series construction and the vectorised call over 500 rows; the same call over
  fifty instruments in one frame amortises both to nothing, which is what the
  cross-sectional metrics will do. It is also the independent reference every other
  backend is asserted against, and import cost is paid once per process (284 ms for
  the first call, ~1 ms each afterwards).

## How computation changes as the dataset grows

| Growth | Effect | Why |
|---|---|---|
| More rows per instrument (longer history) | The loader's range scan is bounded by the requested range, not the instrument's life: `pad + observations in range`. A 20-year history costs the same per request as a 1-year one. | The bounds are pushed into `WHERE`, which uses `uniq_price_instrument_trading_date`. Warm-up padding adds `pad` rows — 1 for a daily return, `window - 1` for a moving average, `window` for a volatility, and 0 for cumulative returns and maximum drawdown, whose anchors are inside the range — a constant per request, independent of how much history exists. |
| More instruments | Per-request cost is unchanged: one instrument, one query. Ranking across instruments is the case that changes the answer — it must not become one query per instrument. | Pending: the rankings metric will read the cross-section in one frame and group by instrument, which is what the `pandas` backend is for. |
| More metrics on the same series | Free if they share the loader call. Each additional metric over the same rows adds arithmetic, not queries. | The loader is the only thing that reads, and it returns every column a metric asked for. |
| More concurrent readers | Linear in requests, with no shared mutable state in the engine. | The engine writes nothing. The API's response cache (see [api_optimisation.md](api_optimisation.md)) covers the snapshot reads, not the computed series, which depends on the requested range. |
| A very long single series read whole | Becomes memory-bound, not query-bound, and is not the shape this engine is designed for: a range is what bounds the work. | If a full-history export is needed, that is an offline job over a DataFrame, not a request. |

## Tests

`apps/analytics/tests/test_series.py` — the read boundary, 29 tests:

* the whole series in exactly one query, in date order (query count pinned);
* only the requested columns are selected — `source`, `ingestion_job_id`,
  `created_at` and `adjusted_close` are asserted absent from the SQL, and the
  instrument table is asserted not to be joined;
* warm-up: one row before the range is read, `pad` larger than the available
  history still returns every requested row, `pad` without a `start_date` is a
  no-op, and the window does not lag across instruments;
* the window columns: `previous_close` is the previous session, the moving-average
  column is the trailing mean, the running-max column is the highest close so far
  *within the rows returned* (so the date range anchors it), all three can be read
  in the same single query, a database partial-frame mean is passed through as data
  (the metric applies the completeness rule), and a window below 1 is rejected;
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

`apps/analytics/tests/test_moving_average.py` — the window, 23 tests:

* the hand-computed series, the `5/3` rounding, a window of 1 (the price itself),
  and a window exactly as long as the series (one value, on the last date);
* incompleteness: a window longer than the history is `None` for every date with
  `sample_size` counting up, rather than a partial mean;
* the running sum is asserted **exactly** equal to a fresh per-window sum, on
  prices with no exact binary representation — the claim that `Decimal` makes the
  `O(n)` shortcut exact;
* supplied (database) means are used, obey the same completeness rule, and agree
  with the computed ones when they are the true means;
* the three backends agree, including when the history is shorter than the window
  (where a `ROWS BETWEEN` frame would quietly return a partial mean);
* the service: `padding == window - 1` and the first requested date carries a
  value; the same dates read without warm-up leave holes; the window counts
  observations rather than calendar days (prices on DAY1, DAY2 and DAY5 with a
  window of 3 have a mean on DAY5 across a four-day hole); one query; a window
  below 1 is rejected.

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

## Deliberately not done yet

* Metrics 6–9, each with its own sample, edge-case policy and trade-off note:
  highest and lowest prices, volume summaries, percentage change over a period,
  and the cross-sectional rankings.
* **No HTTP endpoint yet.** The service layer is where a metric is defined, and
  five metrics in it is the right moment to fix the shape rather than the moment it
  was settled: the results now share a `MetricResult` base with a `points` list, a
  `padding` count, an echoed `window`, a nested summary object where a metric has
  one, and a consistent rendering rule, and that is what the serializer, the
  filters and the query-parameter contract will be written against. Until then, the
  metrics are called from Python and tested there.
* **No snapshot recalculation job yet.** `AnalyticSnapshot` already models the
  grain — `(instrument, as_of_date, metric_type, lookback_days)` — and the moving
  averages and volatility are its natural occupants, since a stored value is how a
  series becomes rankable across instruments and how "what did we say on 12 March"
  becomes answerable. It is needed by the rankings (metric 9) and by the audit
  trail, not by a per-instrument series, and it is the next structural piece.
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
`STDDEV_SAMP` and derived-table `MIN` cross-checks, and the two nested-window
rejections that explain why two metrics have no SQL member:

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
