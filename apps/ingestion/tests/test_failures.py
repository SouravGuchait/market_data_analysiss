"""Transient vs permanent — the decision that decides whether a worker retries.

This is the smallest module in the async layer and the one with the largest
blast radius: get it wrong in one direction and a poison message takes down a
queue, in the other and a five-second database blip loses a load.
"""

import random

import pytest
from django.db import IntegrityError, OperationalError

from apps.ingestion.etl.failures import is_transient
from apps.ingestion.tasks import backoff_seconds


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(OperationalError("(2006, 'MySQL server has gone away')"), id="mysql-gone-away"),
        pytest.param(OperationalError("Deadlock found when trying to get lock"), id="deadlock"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param(ConnectionError("connection reset by peer"), id="connection-reset"),
    ],
)
def test_failures_of_the_environment_are_transient(exc):
    assert is_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(IntegrityError("Duplicate entry for key 'uniq_price'"), id="constraint"),
        pytest.param(ValueError("not a number"), id="value-error"),
        pytest.param(KeyError("close"), id="missing-key"),
        pytest.param(FileNotFoundError("no such file"), id="missing-file"),
    ],
)
def test_failures_of_the_work_itself_are_permanent(exc):
    assert is_transient(exc) is False


def test_a_client_constraint_error_is_permanent_despite_being_a_database_error():
    """IntegrityError is a DatabaseError, and a duplicate will be a duplicate again."""
    assert is_transient(IntegrityError("duplicate")) is False


def test_a_transient_error_wrapped_by_the_caller_is_still_transient():
    """Frameworks wrap. The interesting error is rarely the outermost one."""
    try:
        try:
            raise OperationalError("server has gone away")
        except OperationalError as inner:
            raise RuntimeError("ingestion failed") from inner
    except RuntimeError as outer:
        assert is_transient(outer) is True


def test_a_permanent_error_wrapped_by_a_transient_one_is_still_permanent():
    """A permanent marker anywhere in the chain wins.

    An ``IntegrityError`` raised inside a connection-recovery block would be
    retried until the budget ran out if only the outermost type were checked.
    """
    try:
        try:
            raise IntegrityError("duplicate")
        except IntegrityError as inner:
            raise OperationalError("wrapped") from inner
    except OperationalError as outer:
        assert is_transient(outer) is False


def test_a_broker_outage_is_transient():
    redis = pytest.importorskip("redis")
    assert is_transient(redis.exceptions.ConnectionError("broker down")) is True


def test_a_temporary_api_failure_is_transient():
    requests = pytest.importorskip("requests")
    assert is_transient(requests.exceptions.ConnectTimeout("api timed out")) is True


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #


def test_backoff_doubles_then_stops_at_the_cap():
    assert backoff_seconds(0, base=10, cap=900, jitter=False) == 10
    assert backoff_seconds(1, base=10, cap=900, jitter=False) == 20
    assert backoff_seconds(2, base=10, cap=900, jitter=False) == 40
    assert backoff_seconds(3, base=10, cap=900, jitter=False) == 80
    # 10 * 2**10 = 10240, capped.
    assert backoff_seconds(10, base=10, cap=900, jitter=False) == 900


def test_backoff_never_returns_a_negative_or_unbounded_delay():
    for attempt in range(8):
        ceiling = backoff_seconds(attempt, base=10, cap=900, jitter=False)
        value = backoff_seconds(attempt, base=10, cap=900, rng=random.Random(attempt))
        assert 0 <= value <= ceiling


def test_backoff_jitter_spreads_retries_instead_of_synchronising_them():
    """Many jobs failing together must not all retry at the same instant."""
    draws = {backoff_seconds(4, base=10, cap=900, rng=random.Random(seed)) for seed in range(20)}
    assert len(draws) > 1


def test_backoff_reads_its_bounds_from_settings(settings):
    settings.INGESTION_RETRY_BACKOFF_SECONDS = 3
    settings.INGESTION_RETRY_BACKOFF_MAX_SECONDS = 12

    assert backoff_seconds(0, jitter=False) == 3
    assert backoff_seconds(5, jitter=False) == 12
