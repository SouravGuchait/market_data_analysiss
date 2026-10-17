"""Business logic for user-facing account data.

Aggregation is pushed into SQL so the database does the counting rather than the
application iterating rows in Python.
"""

from django.conf import settings
from django.db.models import Count, Max, Min, Sum

from apps.common import cache
from apps.marketdata.models import PriceRecord

USAGE_CACHE_NAMESPACE = "usage"
#: Committing prices invalidates this namespace; the TTL is the backstop for
#: everything else that changes a summary (a job finishing, a job's counters).
USAGE_CACHE_TTL_SETTING = "API_CACHE_USAGE_TTL"
USAGE_CACHE_TTL_DEFAULT = 60


def usage_cache_key(user) -> str:
    """The cache key for one user's summary.

    The owner is the scope. Unlike the analytics endpoints -- whose rows are
    reference data every authenticated role sees -- this response *is* the
    caller's own data, so it must never be shared between users. Keying on the
    user id makes that impossible rather than merely unlikely.
    """
    return cache.build_key(USAGE_CACHE_NAMESPACE, scope=f"user:{user.pk}")


def usage_cache_ttl() -> int:
    return getattr(settings, USAGE_CACHE_TTL_SETTING, USAGE_CACHE_TTL_DEFAULT)


def get_user_usage_statistics(user):
    """Returns the authenticated user's activity summary.

    Deliberately a handful of aggregate queries rather than one complex one:
    each is a single pass over an indexed table, and the results stay readable
    and independently cacheable.

    Always reads the database. The cached read is
    :func:`get_cached_user_usage_statistics`; keeping this one pure means a test
    (or a caller that needs a guaranteed-fresh number) has one function to reach
    for that cannot be served from a cache.
    """
    jobs = user.ingestion_jobs.all()

    job_totals = jobs.aggregate(
        total_jobs=Count("id"),
        total_rows_received=Sum("rows_received"),
        total_rows_accepted=Sum("rows_accepted"),
        total_rows_rejected=Sum("rows_rejected"),
    )
    jobs_by_status = {
        row["status"]: row["count"]
        for row in jobs.values("status").annotate(count=Count("id"))
    }

    imported = PriceRecord.objects.filter(ingestion_job__created_by=user)
    price_totals = imported.aggregate(
        price_records_imported=Count("id"),
        instruments_covered=Count("instrument", distinct=True),
        earliest_price_date=Min("trading_date"),
        latest_price_date=Max("trading_date"),
    )

    return {
        "jobs": {
            "total": job_totals["total_jobs"] or 0,
            "by_status": jobs_by_status,
            "rows_received": job_totals["total_rows_received"] or 0,
            "rows_accepted": job_totals["total_rows_accepted"] or 0,
            "rows_rejected": job_totals["total_rows_rejected"] or 0,
        },
        "prices": {
            "records_imported": price_totals["price_records_imported"] or 0,
            "instruments_covered": price_totals["instruments_covered"] or 0,
            "earliest_date": price_totals["earliest_price_date"],
            "latest_date": price_totals["latest_price_date"],
        },
    }
