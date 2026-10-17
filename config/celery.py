"""The Celery application.

Redis is used as the **broker only**. There is deliberately no result backend:
the ``IngestionJob`` row is the durable, queryable record of what a load did,
and a second store of "job status" would be a second thing to keep in sync —
and a second thing to be wrong. So task results are discarded
(``task_ignore_result``) and every question about a load is answered from the
database.

The consequence, stated plainly: **we cannot assume a task runs exactly once.**
A broker gives at-least-once delivery, not exactly-once. What makes that safe
here is that the task is idempotent — it claims the job with a conditional
``UPDATE``, and re-processing a file upserts rather than inserts. See
``apps/ingestion/tasks.py``.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("market_data")

# Every knob lives in Django settings under the CELERY_ namespace, so there is
# exactly one place to configure a worker.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Pick up @shared_task definitions from every installed app.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):  # pragma: no cover - operational smoke test
    """Print the request, for checking broker connectivity by hand."""
    print(f"Request: {self.request!r}")
