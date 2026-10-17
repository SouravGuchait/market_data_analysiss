"""Print the ingestion system's operational state in one glance.

This is the command an operator runs first when a queue looks stuck. It keeps
the three dependencies separate on purpose: "the broker is down" and "the
database is down" have completely different remedies, and a combined health
check that reports both as "unhealthy" tells you nothing about which to fix.

Exit code is non-zero when the database is unreachable — that is the one failure
that makes the numbers above it meaningless.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.ingestion import monitoring


class Command(BaseCommand):
    help = "Report broker, database, queue depth, and job-status counts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit machine-readable JSON (for a monitoring probe).",
        )
        parser.add_argument(
            "--queue",
            default=None,
            help="Queue name to measure. Defaults to the Celery default queue.",
        )

    def handle(self, *args, **options):
        snapshot = monitoring.health()
        if options["queue"]:
            snapshot["queue_depth"] = monitoring.queue_depth(options["queue"])

        if options["as_json"]:
            self.stdout.write(json.dumps(snapshot, indent=2, sort_keys=True))
        else:
            self._render(snapshot)

        if snapshot["database"] != "up":
            raise SystemExit(2)

    def _render(self, snapshot: dict) -> None:
        database = snapshot["database"]
        broker = snapshot["broker"]
        self.stdout.write(
            f"database : {self.style.SUCCESS('up') if database == 'up' else self.style.ERROR('down')}"
        )
        self.stdout.write(
            f"broker   : {self.style.SUCCESS('up') if broker == 'up' else self.style.ERROR('down')}"
        )

        depth = snapshot["queue_depth"]
        # None means "could not ask", not "zero waiting" — say so rather than
        # print a 0 that would read as a healthy, empty queue.
        self.stdout.write(
            f"queue    : {depth if depth is not None else 'unknown (broker unreachable)'}"
        )

        jobs = snapshot["jobs"]
        if jobs:
            self.stdout.write("jobs     :")
            for name in sorted(jobs):
                self.stdout.write(f"  {name:<22} {jobs[name]}")
        else:
            self.stdout.write("jobs     : none")

        stale = snapshot["stale_processing"]
        line = f"stale    : {stale} processing job(s) with a cold heartbeat"
        self.stdout.write(self.style.WARNING(line) if stale else line)
        if stale:
            self.stdout.write(
                "           recover them with: manage.py requeue_stale_jobs"
            )
