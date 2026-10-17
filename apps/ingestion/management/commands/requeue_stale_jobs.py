"""Recover ingestion jobs abandoned by a worker that died mid-load.

A worker killed with ``SIGKILL``, or a machine that loses power, cannot fail the
job it was holding: the row is left ``processing`` and its heartbeat stops. This
command is the half of the recovery story the worker cannot implement itself.

It runs in two passes, each of which answers a different question:

1. **Jobs with attempts left** are put back to ``pending`` and re-enqueued. They
   resume from their checkpoint, so a load that got 90% of the way through is
   not started over from zero.
2. **Jobs that have exhausted their attempts** are marked ``failed``. Retrying a
   job that has already crashed the worker five times is how a poison message
   takes a queue down; the honest answer is to stop and let a human look.

Safe to run on a schedule, and safe to run twice: the second run finds no stale
jobs because the first one moved them out of ``processing``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ingestion import monitoring
from apps.ingestion.models import IngestionJob, JobStatus


class Command(BaseCommand):
    help = "Return jobs abandoned by a crashed worker to the queue, or fail them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than",
            type=int,
            default=None,
            help=(
                "Seconds without a heartbeat before a job is considered "
                "abandoned. Defaults to INGESTION_STALE_AFTER_SECONDS."
            ),
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=None,
            help=(
                "Attempts after which a job is failed instead of requeued. "
                "Defaults to INGESTION_MAX_RETRIES + 1."
            ),
        )
        parser.add_argument(
            "--no-requeue",
            action="store_true",
            help="Reset stale jobs to pending but do not publish them to the broker.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        max_attempts = options["max_attempts"]
        if max_attempts is None:
            max_attempts = int(getattr(settings, "INGESTION_MAX_RETRIES", 5)) + 1

        stale = list(monitoring.stale_jobs(seconds=options["older_than"]))
        if not stale:
            self.stdout.write(self.style.SUCCESS("No stale jobs found."))
            return

        requeued, failed = 0, 0
        for job in stale:
            if job.attempts >= max_attempts:
                if not options["dry_run"]:
                    self._fail(job, max_attempts)
                failed += 1
                self.stdout.write(
                    f"  failed   {job.pk}  attempts={job.attempts} "
                    f"(>= max {max_attempts}) status={job.status}"
                )
                continue

            if not options["dry_run"]:
                self._requeue(job, options["no_requeue"])
            requeued += 1
            self.stdout.write(f"  requeued {job.pk}  attempts={job.attempts}")

        verb = "would be" if options["dry_run"] else "were"
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(stale)} stale job(s): {requeued} {verb} requeued, "
                f"{failed} {verb} failed."
            )
        )

    # ------------------------------------------------------------------ #

    def _requeue(self, job: IngestionJob, no_requeue: bool) -> None:
        """Hand the job back to the pool, keeping its checkpoint for a resume."""
        IngestionJob.objects.filter(pk=job.pk).update(
            status=JobStatus.PENDING,
            heartbeat_at=None,
            last_error=(
                "requeued after an abandoned attempt by worker "
                f"{job.celery_task_id or 'unknown'}"
            ),
        )
        if no_requeue:
            return
        # Imported here so `--no-requeue` works with no broker at all.
        from apps.ingestion.services import enqueue_ingestion_job

        try:
            enqueue_ingestion_job(job)
        except Exception as exc:
            # The row is already pending, so nothing is lost: a later run (or a
            # broker recovery) will pick it up. Report, do not abort the sweep.
            self.stderr.write(
                f"  warning: {job.pk} is pending but could not be queued: {exc}"
            )

    def _fail(self, job: IngestionJob, max_attempts: int) -> None:
        """Stop retrying a job that keeps crashing its worker.

        Failing never publishes anything — that is the difference between this
        and :meth:`_requeue`, and the reason the two are separate methods rather
        than one method with a flag.
        """
        IngestionJob.objects.filter(pk=job.pk).update(
            status=JobStatus.FAILED,
            completed_at=timezone.now(),
            last_error=f"abandoned after {max_attempts} attempts",
            error_summary=f"abandoned after {max_attempts} attempts",
        )
