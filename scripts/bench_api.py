"""Measure the market-data API before and after the query/cache work.

Why a script and not a test
---------------------------
``assertNumQueries`` proves a query count, but it cannot tell you *which* query
is slow, whether MySQL chose an index, or what a page actually costs in
milliseconds. This harness answers all four questions for the same request:

* **query count** -- captured with ``CaptureQueriesContext``, split into the
  cold call (first, so caches miss) and the warm call (a repeat);
* **latency** -- median and p95 over N in-process requests, after warm-up;
* **result size** -- the rendered response body in bytes;
* **query plan** -- ``EXPLAIN`` on the SQL MySQL actually ran. On the MySQL
  backend Django's ``last_executed_query`` returns ``cursor._executed``, which
  already has the parameters interpolated, so the captured SQL is executable.

The dataset is seeded and therefore reproducible: the same seed produces the
same instruments, the same instrument dimensions, the same prices, and the same
snapshots. It is written straight to the database with ``bulk_create`` rather
than through the ingestion pipeline, because the point is to measure *reads* at
a realistic scale without paying for an ingest.

Usage
-----
    python scripts/bench_api.py baseline
    python scripts/bench_api.py after
    python scripts/bench_api.py after --label after-v2 --reset

Results land in ``docs/benchmarks/api_<label>.json`` and a summary is printed.

``--reset`` removes only the benchmark exchange (``BENCH``) and everything
hanging off it, so it never touches real data.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.analytics.models import AnalyticSnapshot, MetricType  # noqa: E402
from apps.instruments.models import AssetType, Exchange, Instrument  # noqa: E402
from apps.ingestion.models import IngestionJob, SourceType  # noqa: E402
from apps.marketdata.models import PriceRecord  # noqa: E402

# The in-process API client sends ``Host: testserver``, which Django only permits
# automatically inside the test runner. The harness is not the test runner, so it
# says so explicitly -- otherwise every request is a 400 error page and the whole
# measurement is quietly meaningless.
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]

# --------------------------------------------------------------------------- #
# The reproducible dataset
# --------------------------------------------------------------------------- #

BENCH_EXCHANGE = "BENCH"
BENCH_USER_EMAIL = "bench-owner@example.test"
DEFAULT_SEED = 20261017
BENCH_END_DATE = date(2024, 3, 14)

METRIC_TYPES = [MetricType.RETURN, MetricType.MOVING_AVERAGE, MetricType.VOLATILITY, MetricType.DRAWDOWN]
LOOKBACKS = [30, 90]
ASSET_TYPES = [AssetType.EQUITY, AssetType.ETF, AssetType.CRYPTO, AssetType.FOREX]


def trading_days(count: int, *, end: date = BENCH_END_DATE) -> list[date]:
    """``count`` weekdays ending at ``end``, oldest first.

    Weekends are skipped so the calendar shape resembles real data without
    pretending to be a holiday calendar.
    """
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def reset_dataset() -> None:
    """Delete the benchmark exchange and everything hanging off it.

    The order matters: ``PriceRecord.instrument`` and ``Instrument.exchange`` are
    both ``PROTECT``, so the children have to go before the parents.
    """
    instrument_ids = list(
        Instrument.objects.filter(exchange__code=BENCH_EXCHANGE).values_list("id", flat=True)
    )
    if instrument_ids:
        AnalyticSnapshot.objects.filter(instrument_id__in=instrument_ids).delete()
        PriceRecord.objects.filter(instrument_id__in=instrument_ids).delete()
        Instrument.objects.filter(id__in=instrument_ids).delete()
    Exchange.objects.filter(code=BENCH_EXCHANGE).delete()
    IngestionJob.objects.filter(created_by__email=BENCH_USER_EMAIL).delete()
    get_user_model().objects.filter(email=BENCH_USER_EMAIL).delete()


def build_dataset(instruments: int, days: int, *, seed: int = DEFAULT_SEED) -> dict:
    """Create the seeded dataset, or return what already exists.

    Idempotent: an existing benchmark exchange short-circuits the rebuild, so the
    harness can be re-run after a code change without re-seeding.
    """
    existing = Instrument.objects.filter(exchange__code=BENCH_EXCHANGE).count()
    if existing:
        prices = PriceRecord.objects.filter(
            instrument__exchange__code=BENCH_EXCHANGE
        ).count()
        snapshots = AnalyticSnapshot.objects.filter(
            instrument__exchange__code=BENCH_EXCHANGE
        ).count()
        return {"instruments": existing, "prices": prices, "snapshots": snapshots, "reused": True}

    calendar = trading_days(days)

    with transaction.atomic():
        exchange = Exchange.objects.create(
            code=BENCH_EXCHANGE, name="Benchmark Exchange", timezone="UTC"
        )
        user = get_user_model().objects.create_user(
            email=BENCH_USER_EMAIL, password="benchmark-only-not-a-secret-42!"
        )

        # A handful of jobs so the usage endpoint aggregates real rows, and so
        # every price has an owning job to attribute it to.
        jobs = [
            IngestionJob.objects.create(
                created_by=user,
                source_type=SourceType.CSV,
                status="completed",
                rows_received=len(calendar),
                rows_accepted=len(calendar),
            )
            for _ in range(8)
        ]

        instrument_rows = []
        for index in range(instruments):
            instrument_rows.append(
                Instrument(
                    symbol=f"BENCH{index:04d}",
                    name=f"Benchmark Instrument {index:04d}",
                    asset_type=ASSET_TYPES[index % len(ASSET_TYPES)],
                    exchange=exchange,
                    currency="USD",
                    is_active=index % 7 != 0,  # a few inactive, so the filter has work
                )
            )
        Instrument.objects.bulk_create(instrument_rows)
        created = list(
            Instrument.objects.filter(exchange=exchange).order_by("symbol")
        )

        price_rows: list[PriceRecord] = []
        for index, instrument in enumerate(created):
            # The symbol seeds the walk, so every instrument is reproducible
            # independently of the order the rows are built in.
            walk = random.Random(f"{seed}:{instrument.symbol}")
            price = Decimal(walk.uniform(20, 400)).quantize(Decimal("0.0001"))
            job = jobs[index % len(jobs)]
            for day in calendar:
                price = max(Decimal("1"), price * Decimal(str(walk.uniform(0.985, 1.015))))
                price = price.quantize(Decimal("0.0001"))
                spread = Decimal(str(walk.uniform(0.001, 0.02)))
                high = (price * (1 + spread)).quantize(Decimal("0.0001"))
                low = (price * (1 - spread)).quantize(Decimal("0.0001"))
                price_rows.append(
                    PriceRecord(
                        ingestion_job=job,
                        instrument=instrument,
                        trading_date=day,
                        open=price,
                        high=high,
                        low=low,
                        close=price,
                        adjusted_close=price,
                        volume=walk.randint(10_000, 5_000_000),
                        source="benchmark",
                    )
                )
        PriceRecord.objects.bulk_create(price_rows, batch_size=2000)

        snapshot_rows: list[AnalyticSnapshot] = []
        snapshot_days = calendar[-min(250, len(calendar)) :]
        for instrument in created:
            history = random.Random(f"{seed}:snapshots:{instrument.symbol}")
            for metric in METRIC_TYPES:
                for lookback in LOOKBACKS:
                    value = Decimal(history.uniform(-1, 1)).quantize(Decimal("0.00000001"))
                    for day in snapshot_days:
                        snapshot_rows.append(
                            AnalyticSnapshot(
                                instrument=instrument,
                                as_of_date=day,
                                metric_type=metric,
                                lookback_days=lookback,
                                value=value,
                                metadata={"method": "simple", "benchmark": True},
                            )
                        )
        AnalyticSnapshot.objects.bulk_create(snapshot_rows, batch_size=5000)

    return {
        "instruments": len(created),
        "prices": len(price_rows),
        "snapshots": len(snapshot_rows),
        "reused": False,
    }


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #

SELECT_LIKE = ("SELECT", "WITH")

# Why the query log is cleared before every capture
# ------------------------------------------------
# Django keeps ``connection.queries_log`` as a ``deque(maxlen=9000)``.
# ``CaptureQueriesContext`` records ``len(queries_log)`` on entry and exit and
# slices between them, so once the deque is full its length stops changing, both
# indices are equal, and every capture comes back *empty*. Seeding a quarter of a
# million rows saturates it, which is exactly how this harness first reported
# "0 queries" for requests that were demonstrably doing work.
#
# Clearing it resets the indices. Note that assigning a replacement
# (``connection.queries_log = []``) does *not* work: ``ConnectionProxy`` forwards
# ``__getattr__`` but not ``__setattr__``, so the new value would land on the
# proxy and shadow the real log that the queries are appended to.
#
# A second trap, found while adding the cache: ``CaptureQueriesContext.
# captured_queries`` is a property that slices the *live* log on every access, not
# a snapshot taken at exit. Reading a context after the next request started
# therefore reports that request's queries -- which made every "cold" count an
# echo of the warm one and hid a genuine cache hit behind a zero. Both are fixed
# by snapshotting with ``list(context)`` the moment the request returns.


def explain(sql: str) -> dict | None:
    """``EXPLAIN`` one captured statement.

    MySQL 8 defaults to ``EXPLAIN FORMAT=TREE``, which comes back as a single
    ``EXPLAIN`` column holding an indented access-path tree -- the index actually
    chosen, and the row estimate, which is the part worth recording. Older
    servers (and MariaDB) answer with the classic one-row-per-table table, so both
    shapes are normalised into ``{"format": ..., "plan": ...}``.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN " + sql)
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
    except Exception as exc:  # pragma: no cover - diagnostic only
        return {"format": "error", "plan": str(exc)[:300]}

    if len(columns) == 1 and columns[0].upper() == "EXPLAIN":
        return {"format": "tree", "plan": rows[0][0] if rows else ""}
    return {"format": "table", "plan": [dict(zip(columns, row)) for row in rows]}


def summarise_plan(explain_result: dict | None) -> str:
    """One compact line per plan, whatever shape EXPLAIN came back in."""
    if not explain_result:
        return ""
    if explain_result["format"] == "tree":
        lines = [line.strip() for line in str(explain_result["plan"]).splitlines() if line.strip()]
        return " | ".join(lines)
    parts = []
    for step in explain_result["plan"]:
        parts.append(
            "{table}:{type}:{key}:{rows}:{extra}".format(
                table=step.get("table"),
                type=step.get("type"),
                key=step.get("key") or "NULL",
                rows=step.get("rows"),
                extra=(step.get("Extra") or "").replace("; ", "/"),
            )
        )
    return " | ".join(parts)


#: Django appends ``LIMIT 21`` to a ``.get()`` (single-object) lookup, which is
#: how ``main_query`` tells a primary-key probe apart from the list query whose
#: plan we actually want to read.
GET_PROBE_LIMIT = "LIMIT 21"


def main_query(queries: list[dict]) -> dict | None:
    """The statement whose plan describes the request.

    Picking by length alone is not enough: once the price list stopped joining
    the instrument, Django's own ``LIMIT 21`` instrument lookup became the longer
    of the two, and the report started describing that instead of the page. So
    single-object probes are only used when there is nothing else -- which is the
    detail endpoint's case -- and the list query wins wherever there is one.
    """
    selects = [
        query for query in queries if query["sql"].lstrip().upper().startswith(SELECT_LIKE)
    ]
    page_like = [q for q in selects if GET_PROBE_LIMIT not in q["sql"].upper()]
    candidates = page_like or selects
    return max(candidates, key=lambda query: len(query["sql"])) if candidates else None


def measure(client, method: str, url: str, *, repeat: int) -> dict:
    """Issue one request repeatedly and report count, latency, size, and plan.

    ``cold`` is the *first* request for this case in this process, so with the
    cache on it is guaranteed to be a miss: its query count is the cost of
    answering from the database. ``warm`` is the immediate repeat, so a hit shows
    up as a smaller count. With no cache in the path the two are identical, which
    is what makes the before/after comparison honest.

    The captures deliberately happen before the timing runs. An earlier revision
    warmed the endpoint up first, which quietly turned every "cold" measurement
    into a cache hit once caching existed.
    """
    def capture():
        """One request, plus the queries it ran, snapshotted immediately.

        Clearing first defeats the capped log. Taking ``list(context)`` here --
        rather than reading the context later -- matters just as much: Django
        5.2's ``CaptureQueriesContext.captured_queries`` is a *live* slice of the
        connection's log, not a snapshot, so a context read after the following
        request reports that request's queries instead. Reading it late made
        every "cold" count a copy of the warm one.
        """
        connection.queries_log.clear()
        with CaptureQueriesContext(connection) as context:
            response = getattr(client, method)(url)
        return response, list(context)

    _cold, cold_queries = capture()
    warm, warm_queries = capture()

    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        getattr(client, method)(url)
        samples.append((time.perf_counter() - started) * 1000)

    # The plan comes from the cold capture, which with caching on is the miss:
    # that is the statement the endpoint actually runs.
    main = main_query(cold_queries)

    if warm.status_code != 200:
        print(
            f"    !! {url} returned {warm.status_code}; the numbers below are not a measurement",
            file=sys.stderr,
        )

    return {
        "url": url,
        "status": warm.status_code,
        "cold_queries": len(cold_queries),
        "warm_queries": len(warm_queries),
        # ``-`` when the endpoint does not consult the cache at all, which is how
        # a reader can tell "cached and missed" from "not cached".
        "cache": warm.headers.get("X-Cache", "-"),
        "median_ms": round(statistics.median(samples), 2),
        "p95_ms": round(statistics.quantiles(samples, n=20)[18], 2) if len(samples) > 4 else round(max(samples), 2),
        "min_ms": round(min(samples), 2),
        "bytes": len(warm.content),
        "rows": (warm.data.get("count") if hasattr(warm, "data") and isinstance(warm.data, dict) else None),
        "sql": main["sql"] if main else None,
        "explain": explain(main["sql"]) if main else None,
        # Every statement, with its plan: the report reads one of them, but the
        # evidence for "this query, this plan" should not depend on which one the
        # report happened to pick.
        "statements": [
            {"sql": query["sql"], "explain": explain(query["sql"])}
            for query in cold_queries
        ],
    }


def price_query_ab(instrument_pk: int, *, rounds: int = 300) -> dict:
    """Time one price page with and without the instrument join.

    The endpoint-level number cannot settle this: the request spends most of its
    time in DRF serialisation and pagination, so a fraction of a millisecond
    disappears into run-to-run noise. This isolates the statement, alternating the
    two forms so that neither is handed a warmer connection, and measures twice so
    a drifting machine shows up as a disagreement between passes rather than as a
    false win.
    """
    joined = (
        PriceRecord.objects.select_related("instrument", "instrument__exchange")
        .filter(instrument_id=instrument_pk)
        .order_by("trading_date")
    )
    lean = (
        PriceRecord.objects.filter(instrument_id=instrument_pk).order_by("trading_date")
    )

    def time_form(queryset) -> float:
        started = time.perf_counter()
        for _ in range(rounds):
            list(queryset[:25])
        return (time.perf_counter() - started) * 1000 / rounds

    pass_one = (time_form(joined), time_form(lean))
    pass_two = (time_form(joined), time_form(lean))

    with_join_sql = str(joined[:25].query)
    without_join_sql = str(lean[:25].query)
    return {
        "rounds": rounds,
        "with_join_ms": round(min(pass_one[0], pass_two[0]), 3),
        "without_join_ms": round(min(pass_one[1], pass_two[1]), 3),
        "with_join_sql_chars": len(with_join_sql),
        "without_join_sql_chars": len(without_join_sql),
        # Both plans, because "the same access path, less work" is the claim.
        "with_join_plan": explain(with_join_sql),
        "without_join_plan": explain(without_join_sql),
    }


# --------------------------------------------------------------------------- #
# The cases
# --------------------------------------------------------------------------- #

def build_cases(instrument_pk: int) -> list[tuple[str, str, str]]:
    """(name, method, url) for every read the API exposes meaningfully."""
    today = BENCH_END_DATE
    start = today - timedelta(days=120)
    return [
        ("instruments.list.p1", "get", "/api/v1/instruments/?page=1"),
        ("instruments.list.p3", "get", "/api/v1/instruments/?page=3"),
        ("instruments.list.filter", "get", "/api/v1/instruments/?asset_type=equity"),
        ("instruments.list.search", "get", "/api/v1/instruments/?search=Benchmark"),
        ("instruments.list.order", "get", "/api/v1/instruments/?ordering=-created_at"),
        ("instruments.detail", "get", f"/api/v1/instruments/{instrument_pk}/"),
        ("exchanges.list", "get", "/api/v1/instruments/exchanges/"),
        ("prices.p1", "get", f"/api/v1/market-data/instruments/{instrument_pk}/prices/?page=1"),
        ("prices.p20", "get", f"/api/v1/market-data/instruments/{instrument_pk}/prices/?page=20"),
        ("prices.range", "get", f"/api/v1/market-data/instruments/{instrument_pk}/prices/?start_date={start}&end_date={today}"),
        ("prices.order_close", "get", f"/api/v1/market-data/instruments/{instrument_pk}/prices/?ordering=-close"),
        ("analytics.p1", "get", f"/api/v1/analytics/instruments/{instrument_pk}/metrics/?page=1"),
        ("analytics.metric", "get", f"/api/v1/analytics/instruments/{instrument_pk}/metrics/?metric_type=return"),
        ("analytics.range", "get", f"/api/v1/analytics/instruments/{instrument_pk}/metrics/?metric_type=return&start_date={start}&end_date={today}"),
        ("analytics.order_value", "get", f"/api/v1/analytics/instruments/{instrument_pk}/metrics/?ordering=-value"),
        ("auth.usage", "get", "/api/v1/auth/usage/"),
        ("jobs.list", "get", "/api/v1/ingestion/?page=1"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("label", help="name for this run, e.g. baseline or after")
    parser.add_argument("--instruments", type=int, default=100)
    parser.add_argument("--trading-days", type=int, default=500)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--repeat", type=int, default=15, help="timed requests per case")
    parser.add_argument("--reset", action="store_true", help="delete the benchmark dataset first")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="disable cached reads, to measure the ORM work on its own",
    )
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "docs" / "benchmarks")
    args = parser.parse_args(argv)

    if args.no_cache:
        # Read at call time by apps.common.cache, so setting it here is enough --
        # and it isolates the ORM changes from the caching layer in one run.
        settings.API_CACHE_ENABLED = False
        print("Cached reads disabled (--no-cache).")

    if connection.vendor != "mysql":
        print(f"WARNING: measuring on {connection.vendor}, not MySQL; plans will differ.", file=sys.stderr)

    if args.reset:
        print("Resetting the benchmark dataset ...")
        reset_dataset()

    print(f"Building dataset (instruments={args.instruments}, days={args.trading_days}, seed={args.seed}) ...")
    started = time.perf_counter()
    dataset = build_dataset(args.instruments, args.trading_days, seed=args.seed)
    seed_seconds = time.perf_counter() - started
    print(
        f"  instruments={dataset['instruments']} prices={dataset['prices']:,} "
        f"snapshots={dataset['snapshots']:,} reused={dataset['reused']} ({seed_seconds:.1f}s)"
    )

    # Clear the cache so "cold" really is cold, and drop the cache from the
    # measurement if it is not reachable (the harness must never fail on Redis).
    cache_available = True
    try:
        cache.clear()
    except Exception as exc:
        cache_available = False
        print(f"  cache cleared: no ({type(exc).__name__}); measuring the database path only")

    user = get_user_model().objects.get(email=BENCH_USER_EMAIL)
    client = APIClient()
    client.force_authenticate(user=user)

    instrument_pk = (
        Instrument.objects.filter(exchange__code=BENCH_EXCHANGE)
        .order_by("symbol")
        .values_list("id", flat=True)
        .first()
    )

    results: dict[str, dict] = {}
    header = f"{'case':24} {'cold':>5} {'warm':>5} {'cache':>5} {'median':>9} {'p95':>8} {'bytes':>8}  plan"
    print("\n" + header)
    print("-" * len(header))
    for name, method, url in build_cases(instrument_pk):
        result = measure(client, method, url, repeat=args.repeat)
        results[name] = result
        print(
            f"{name:24} {result['cold_queries']:>5} {result['warm_queries']:>5} "
            f"{result['cache']:>5} "
            f"{result['median_ms']:>8.2f}m {result['p95_ms']:>7.2f}m {result['bytes']:>8,}  "
            f"{summarise_plan(result['explain'])[:70]}"
        )

    print("\nPrice page, with and without the instrument join:")
    if connection.vendor == "mysql":
        join_ab = price_query_ab(instrument_pk)
        print(
            f"  with join   {join_ab['with_join_ms']:>7.3f}ms  "
            f"{join_ab['with_join_sql_chars']:>5} chars  "
            f"{summarise_plan(join_ab['with_join_plan'])[:60]}"
        )
        print(
            f"  without     {join_ab['without_join_ms']:>7.3f}ms  "
            f"{join_ab['without_join_sql_chars']:>5} chars  "
            f"{summarise_plan(join_ab['without_join_plan'])[:60]}"
        )
    else:
        join_ab = None

    payload = {
        "label": args.label,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "environment": {
            "database": connection.vendor,
            "database_version": connection.get_connection_params().get("database"),
            "cache_backend": settings.CACHES["default"]["BACKEND"] if hasattr(settings, "CACHES") else None,
            "cache_enabled": settings.API_CACHE_ENABLED,
            "cache_available": cache_available,
            "python": sys.version.split()[0],
            "django": django.get_version(),
            "seed": args.seed,
            "instruments": dataset["instruments"],
            "prices": dataset["prices"],
            "snapshots": dataset["snapshots"],
            "repeat": args.repeat,
        },
        "price_query_ab": join_ab,
        "cases": results,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / f"api_{args.label}.json"
    destination.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {destination.relative_to(REPO_ROOT)}")

    totals = {
        "queries_cold": sum(case["cold_queries"] for case in results.values()),
        "queries_warm": sum(case["warm_queries"] for case in results.values()),
    }
    print(
        f"Total queries across all cases: cold={totals['queries_cold']} "
        f"warm={totals['queries_warm']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
