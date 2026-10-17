"""Naming the failure correctly: broker, cache, and database are not the same thing.

A single "Redis is unhealthy" light is not useful: Redis-as-broker failing means
nothing can be queued, while Redis-as-cache failing means pages are slower and no
job state changes at all. These tests pin that distinction down.
"""

import json

import pytest
from django.core.management import call_command
from django.db import OperationalError

from apps.ingestion import monitoring
from apps.ingestion.models import IngestionJob, JobStatus, SourceType

from .helpers import make_user

pytestmark = pytest.mark.django_db


def test_health_reports_each_dependency_separately():
    snapshot = monitoring.health()

    assert set(snapshot) == {
        "database",
        "broker",
        "queue_depth",
        "jobs",
        "stale_processing",
    }
    assert snapshot["database"] in {"up", "down"}
    assert snapshot["broker"] in {"up", "down"}


def test_an_unreachable_broker_is_down_with_an_unknown_depth():
    """"Cannot ask" must not read as "empty queue", which is what a 0 would say."""
    from django.test import override_settings

    with override_settings(CELERY_BROKER_URL=""):
        assert monitoring.broker_ok() is False
        assert monitoring.queue_depth() is None
        assert monitoring.health()["broker"] == "down"


def test_queue_depth_is_unknown_when_the_broker_refuses_the_connection():
    from django.test import override_settings

    with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/0"):
        assert monitoring.queue_depth() is None


def test_queue_depth_reads_the_broker_when_one_is_running():
    if not monitoring.broker_ok():
        pytest.skip("no local Redis broker to measure")
    depth = monitoring.queue_depth()
    assert isinstance(depth, int)
    assert depth >= 0


def test_a_broken_database_is_reported_as_down(monkeypatch):
    class _BrokenConnection:
        def cursor(self):
            raise OperationalError("database is down")

    monkeypatch.setattr(monitoring, "connection", _BrokenConnection())

    assert monitoring.database_ok() is False


def test_status_counts_are_a_plain_dict_of_the_whole_table():
    user = make_user()
    IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    IngestionJob.objects.create(
        created_by=user, source_type=SourceType.CSV, status=JobStatus.COMPLETED
    )

    counts = monitoring.status_counts()

    assert counts[JobStatus.PENDING] == 1
    assert counts[JobStatus.COMPLETED] == 1


def test_the_status_command_prints_the_separation(capsys):
    call_command("ingestion_status")

    out = capsys.readouterr().out
    assert "database" in out
    assert "broker" in out
    assert "jobs" in out


def test_the_status_command_can_emit_json_for_a_probe(capsys):
    call_command("ingestion_status", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["database"] == "up"
    assert "queue_depth" in payload
