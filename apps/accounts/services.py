"""Business logic for user-facing account data.

Aggregation is pushed into SQL so the database does the counting rather than the
application iterating rows in Python.
"""

from django.db.models import Count, Max, Min, Sum

from apps.marketdata.models import PriceRecord


def get_user_usage_statistics(user):
    """Returns the authenticated user's activity summary.

    Deliberately a handful of aggregate queries rather than one complex one:
    each is a single pass over an indexed table, and the results stay readable
    and independently cacheable.
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
