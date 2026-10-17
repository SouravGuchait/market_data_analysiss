"""``manage.py ingest_prices`` — run one synchronous price ingestion from disk.

This is the operator-facing entry point to the ETL. It is deliberately thin: it
resolves the owning user, builds a policy from the flags, delegates to
:mod:`apps.ingestion.services`, and prints the data-quality report. All the logic
lives in the engine so the same behaviour is covered by the test suite.

Example
-------
    python manage.py ingest_prices \
        --file sample_data/synthetic/batches/prices_batch_001.csv \
        --email analyst@example.test
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.ingestion.etl.errors import IngestionError
from apps.ingestion.etl.pipeline import IngestionPolicy
from apps.ingestion.services import ingest_file


class Command(BaseCommand):
    help = "Ingest a CSV, JSON, or XLSX price file synchronously and report the result."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the source file.")
        parser.add_argument("--email", required=True, help="Email of the job's owner.")
        parser.add_argument(
            "--source-type",
            default=None,
            help="Override the source type inferred from the file extension.",
        )
        parser.add_argument(
            "--sheet",
            default=None,
            help="Worksheet name, for an XLSX source with more than one sheet.",
        )
        parser.add_argument(
            "--on-invalid",
            choices=["skip", "abort"],
            default="skip",
            help="skip: load valid rows and report the rest (default). abort: all-or-nothing.",
        )
        parser.add_argument(
            "--on-existing",
            choices=["update", "skip", "fail"],
            default="update",
            help="What a repeated (instrument, date) means. Default: update.",
        )

    def handle(self, *args, **options):
        user_model = get_user_model()
        try:
            user = user_model.objects.get(email=options["email"])
        except user_model.DoesNotExist:
            raise CommandError(f"no user with email {options['email']!r}") from None

        policy = IngestionPolicy(
            on_invalid=options["on_invalid"],
            on_existing=options["on_existing"],
        )
        try:
            result = ingest_file(
                user=user,
                path=options["file"],
                source_type=options["source_type"],
                policy=policy,
                sheet_name=options["sheet"],
            )
        except (ValueError, IngestionError) as exc:
            raise CommandError(str(exc)) from exc

        job = result.job
        self.stdout.write(
            self.style.SUCCESS(
                f"job {job.pk} {job.status}: "
                f"received={job.rows_received} accepted={job.rows_accepted} "
                f"rejected={job.rows_rejected}"
            )
        )
        if job.error_summary:
            self.stdout.write(job.error_summary)
        for rule in result.report.get("rules", []):
            if rule["failed"]:
                self.stdout.write(
                    f"  {rule['rule']}: {rule['passed']} pass / {rule['failed']} fail"
                )
