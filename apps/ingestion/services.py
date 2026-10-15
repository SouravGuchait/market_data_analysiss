"""Business logic for ingestion jobs.

Job creation is the one write path in the API layer, so the rules that make a job
valid live here rather than in the view: which fields a client may set, what the
initial state is, and who is allowed to see a job.
"""

from django.core.exceptions import ValidationError

from .models import IngestionJob, JobStatus, SourceType

VALID_SOURCE_TYPES = {choice.value for choice in SourceType}


def get_job_queryset():
    """The base queryset for every job read.

    ``select_related`` joins the owning user in one statement, because every job
    representation shows who created it.
    """
    return IngestionJob.objects.select_related("created_by")


def list_jobs_for_user(user, *, status=None, source_type=None):
    """Returns a user's own jobs, newest first."""
    queryset = get_job_queryset().filter(created_by=user)

    if status:
        queryset = queryset.filter(status=status)
    if source_type:
        queryset = queryset.filter(source_type=source_type)

    return queryset.order_by("-requested_at")


def get_job_for_user(user, pk):
    """Returns one job owned by ``user``, or raises IngestionJob.DoesNotExist.

    Scoping by owner inside the lookup is what makes cross-user access
    impossible: a job belonging to someone else is indistinguishable from a job
    that does not exist.
    """
    return get_job_queryset().get(pk=pk, created_by=user)


def create_job(*, created_by, source_type):
    """Creates a pending ingestion job owned by ``created_by``.

    Only the owner and the source type may be set. Status, timestamps, and row
    counts are all owned by the pipeline: a client sending ``status: succeeded``
    is rejected rather than trusted.
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ValidationError(
            {"source_type": [f"Must be one of: {', '.join(sorted(VALID_SOURCE_TYPES))}."]}
        )

    return IngestionJob.objects.create(created_by=created_by, source_type=source_type)


def get_validation_errors_for_job(job, *, error_code=None):
    """Returns the validation errors recorded against a job."""
    queryset = job.validation_errors.all()

    if error_code:
        queryset = queryset.filter(error_code=error_code)

    return queryset.order_by("row_number")


def get_job_statistics(user):
    """Aggregates a user's ingestion activity.

    Aggregated in SQL so the work happens in the database rather than by
    iterating jobs in Python.
    """
    from django.db.models import Count, Sum

    jobs = list_jobs_for_user(user)
    return jobs.aggregate(
        total_jobs=Count("id"),
        total_rows_received=Sum("rows_received"),
        total_rows_accepted=Sum("rows_accepted"),
        total_rows_rejected=Sum("rows_rejected"),
    )
