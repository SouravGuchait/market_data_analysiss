"""Classify an exception as transient or permanent.

The distinction decides whether a worker retries or gives up, so it is worth
stating the rule precisely:

    **Transient** means *the same work could succeed if attempted again*.

A dropped database connection, a Redis blip, a gateway timeout: yes. Those are
failures of the environment, not of the job, and a retry is exactly the right
response.

    **Permanent** means *the same work will fail the same way next time*.

A unique-constraint violation, a malformed file, a row that cannot be parsed:
no. Retrying those burns the retry budget, delays the operator learning the
truth, and can amplify load during an incident.

Note what is *not* here: a data-quality rejection. A row that fails validation
is not an exception at all — the pipeline records it and finishes
``completed_with_errors``. Only genuine failures of execution reach this module,
which is why the classification can be this narrow.
"""

from __future__ import annotations

from django.db import IntegrityError, InterfaceError, OperationalError


def _transient_types() -> tuple[type[BaseException], ...]:
    """Build the transient class tuple, tolerating missing optional clients.

    ``redis`` and ``requests`` are declared dependencies, but a task that never
    touches either should not fail to import because one of them is absent.
    """
    types: list[type[BaseException]] = [
        OperationalError,  # Django: connection lost, deadlock, server gone away
        InterfaceError,  # Django: low-level connection misuse
        ConnectionError,
        TimeoutError,
    ]
    try:
        from redis.exceptions import RedisError
    except Exception:  # pragma: no cover - redis is a declared dependency
        pass
    else:
        types.append(RedisError)
    try:
        from requests.exceptions import RequestException
    except Exception:  # pragma: no cover - requests is a declared dependency
        pass
    else:
        types.append(RequestException)
    return tuple(types)


_TRANSIENT = _transient_types()


def _chain(exc: BaseException):
    """Yield ``exc`` and its causes/contexts, without looping on a cycle."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient(exc: BaseException) -> bool:
    """Return ``True`` if retrying the same job could plausibly succeed.

    The whole ``__cause__``/``__context__`` chain is inspected, because the
    interesting error is usually wrapped: an ``OperationalError`` surfaces inside
    whatever framework was talking to the database.

    A permanent marker anywhere in the chain wins. A constraint violation that
    happens to be wrapped in a connection error is still a constraint violation —
    retrying it will produce the same violation, and the retry budget is better
    spent elsewhere.
    """
    chain = list(_chain(exc))
    if any(isinstance(link, IntegrityError) for link in chain):
        return False
    return any(isinstance(link, _TRANSIENT) for link in chain)
