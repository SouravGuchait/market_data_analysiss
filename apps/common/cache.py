"""The read-through cache used by the analytics endpoints.

Three properties matter here more than raw speed, and each is a deliberate
choice rather than a default.

**A miss is never an error.**
Every backend call is wrapped. A Redis that cannot be reached, a socket that
times out, a payload that will not decode -- each is reported to the caller as
"not cached", and the request falls back to the database. The API's availability
therefore does not depend on the cache's availability, which is the whole point
of a memoised read: the cache only ever holds a second copy of rows that are
still in MySQL. Failures are logged at most once a minute, because a Redis outage
that floods the log is a second outage.

**Invalidation is a version bump, not a delete.**
Keys embed a per-namespace version integer. Committing new prices increments the
``usage`` version, which orphans every entry in that namespace in one O(1) call.
There is deliberately no ``delete_pattern``: it needs ``SCAN``, it walks the
entire keyspace, and on a shared Redis it is a documented way to stall the
server. Orphaned entries are simply never read again and fall out on TTL.

**A cold key is computed once.**
Concurrent misses on the same key take a short lock: one request computes, the
rest wait briefly and re-read that answer. If the winner is slow the waiters give
up and compute for themselves, so the lock can cost a duplicated query but can
never deadlock a request. No answer depends on the lock being held.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger("market_data.cache")

VERSION_PREFIX = "ver"
# Version 1 is simply "the state before anything invalidated it". Nothing reads
# meaning into the number; only a change of value matters.
INITIAL_VERSION = 1

# Long enough for a readable key, short enough that a pathological one cannot
# become a payload in its own right.
MAX_KEY_LENGTH = 200

# A Redis outage would otherwise log once per request. Once a minute is enough to
# see it in the logs and still notice when it recovers.
ERROR_LOG_INTERVAL_SECONDS = 60
STAMPEDE_POLL_SECONDS = 0.05

_last_error_logged_at = 0.0


def enabled() -> bool:
    """Whether cached reads are switched on for this deployment."""
    return bool(getattr(settings, "API_CACHE_ENABLED", True))


def _setting(name: str, default):
    return getattr(settings, name, default)


def _degraded(operation: str, error: Exception) -> None:
    """Record a cache failure without letting it become the request's problem."""
    global _last_error_logged_at
    now = time.monotonic()
    if now - _last_error_logged_at >= ERROR_LOG_INTERVAL_SECONDS:
        _last_error_logged_at = now
        logger.warning(
            "cache %s failed (%s); serving from the database: %s",
            operation,
            type(error).__name__,
            error,
        )


# --------------------------------------------------------------------------- #
# Wrapped primitives -- the only places that touch the backend
# --------------------------------------------------------------------------- #


def _get(key: str):
    """``(found, value)``.

    ``found`` is deliberately three-valued: ``True`` (the cache answered and the
    key was present), ``False`` (the cache answered and the key was absent), and
    ``None`` (the cache could not answer at all). Callers treat the last two the
    same way -- as a miss -- but the stampede path needs to tell them apart, so
    that an unreachable cache ends the wait instead of being waited out.
    """
    try:
        value = cache.get(key)
    except Exception as error:  # noqa: BLE001 - a cache fault must never surface
        _degraded("get", error)
        return None, None
    if value is None:
        return False, None
    return True, value


def _set(key: str, value, timeout) -> bool:
    try:
        cache.set(key, value, timeout)
        return True
    except Exception as error:  # noqa: BLE001
        _degraded("set", error)
        return False


def _add(key: str, value, timeout):
    """``True`` if the lock was taken, ``False`` if another holder has it,
    ``None`` if the cache could not answer."""
    try:
        return bool(cache.add(key, value, timeout))
    except Exception as error:  # noqa: BLE001
        _degraded("add", error)
        return None


def _delete(key: str) -> None:
    try:
        cache.delete(key)
    except Exception as error:  # noqa: BLE001
        _degraded("delete", error)


# --------------------------------------------------------------------------- #
# Namespace versions
# --------------------------------------------------------------------------- #


def _version_key(namespace: str) -> str:
    return f"md:{VERSION_PREFIX}:{namespace}"


def get_version(namespace: str) -> int:
    """The current version of a namespace, or the initial one if never bumped."""
    found, value = _get(_version_key(namespace))
    if found and isinstance(value, int):
        return value
    return INITIAL_VERSION


# Fallback versions issued by this process, per namespace. Only ever consulted
# when the shared counter has gone missing -- see below.
_fallback_versions: dict[str, int] = {}

#: Bit width of a fallback version. This is not arbitrary: Django's Redis backend
#: stores a Python ``int`` verbatim and advances the counter with Redis ``INCR``,
#: which is a *signed 64-bit* operation. A value at or above ``2**63`` makes INCR
#: fail with "value is not an integer or out of range", after which the namespace
#: can never be invalidated again -- it would keep serving stale bodies until
#: every entry's TTL happened to expire. 62 bits leaves an order of magnitude of
#: headroom while keeping the space of possible versions far larger than any real
#: number of invalidations. (LocMemCache never surfaces this: it increments a
#: Python int, which has no width.)
_FALLBACK_VERSION_BITS = 62

_random = random.SystemRandom()


def _version_after_a_lost_counter(namespace: str) -> int:
    """A version guaranteed to differ from every one already in use.

    The counter has no TTL, so it should outlive everything -- but Redis evicting
    it under an ``allkeys-lru`` policy is a real possibility, and "the counter is
    gone" must not mean "start counting again". Restarting at 2 would re-adopt
    every entry written under version 2 *and still live*, silently serving stale
    bodies: exactly the failure the version exists to prevent.

    Versions only ever have to *differ* from the ones before them, so the
    replacement is a random number rather than the counter we no longer have. It
    is bounded to :data:`_FALLBACK_VERSION_BITS` bits so that the *next*
    :func:`invalidate` can still ``INCR`` it on Redis. The process-local record
    rules out the two collisions that are nameable -- a repeat within this
    process, and the initial version every namespace starts at.
    """
    candidate = _random.randrange(1, 2**_FALLBACK_VERSION_BITS)
    while candidate in (INITIAL_VERSION, _fallback_versions.get(namespace)):
        candidate = _random.randrange(1, 2**_FALLBACK_VERSION_BITS)
    _fallback_versions[namespace] = candidate
    return candidate


def invalidate(*namespaces: str) -> None:
    """Orphan every entry in each namespace.

    One increment per namespace -- O(1), no keyspace scan. Entry *deletion* is
    left to the TTL, which is why every ``set`` must carry one.
    """
    for namespace in namespaces:
        key = _version_key(namespace)
        try:
            cache.incr(key)
        except ValueError:
            # No counter yet -- the ordinary first invalidation of a namespace --
            # or it was evicted. Either way, seed a fresh bounded version.
            _set(key, _version_after_a_lost_counter(namespace), None)
        except Exception as error:  # noqa: BLE001
            # The counter cannot be incremented: it holds a value outside Redis's
            # 64-bit integer range (a counter seeded by an older build, before the
            # bound above), a wrong type, or the cache is unreachable. Re-seed it
            # so the namespace is not permanently frozen -- a *different* version
            # only forces recomputation, it can never serve a stale body. When the
            # cache is genuinely down the re-seed is a no-op and the next request
            # reads the database.
            _degraded("incr", error)
            _set(key, _version_after_a_lost_counter(namespace), None)


def invalidate_after_commit(*namespaces: str) -> None:
    """Invalidate once the surrounding transaction commits.

    Being callable from inside an atomic block is the point: if the transaction
    rolls back then nothing was written, so nothing should have been invalidated.
    Django runs the callback immediately when there is no block to join.
    """
    transaction.on_commit(lambda: invalidate(*namespaces))


# --------------------------------------------------------------------------- #
# Keys and values
# --------------------------------------------------------------------------- #


def build_key(namespace: str, parts: dict | None = None, scope=None) -> str:
    """A readable, bounded cache key.

    Readable because a key you can read in ``redis-cli`` is a key you can debug:
    ``analytics:v4:role:analyst:instrument=13:from=2024-01-01:page=1`` says
    exactly which question it answers, which is what makes an unfamiliar entry
    safe to delete. The version is part of the key, so invalidation shows up here
    too.

    Bounded because the readable form must not grow without limit; anything past
    :data:`MAX_KEY_LENGTH` collapses to a digest of the same string, so two
    different questions still get two different keys.

    Absent values are omitted rather than rendered as "None": "no metric filter"
    and "an empty metric filter" are the same question, and giving them two keys
    would halve the hit rate for no reason.
    """
    version = get_version(namespace)
    segments = [namespace, f"v{version}"]
    if scope:
        segments.append(str(scope))
    for name, value in sorted((parts or {}).items()):
        if value is None or value == "":
            continue
        segments.append(f"{name}={value}")

    key = ":".join(segments)
    if len(key) <= MAX_KEY_LENGTH:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:v{version}:{digest}"


def _encode(value):
    """JSON for the stored payload, or ``None`` if it will not encode.

    JSON rather than pickle: a cached payload is a rendered API response, so JSON
    is the same representation the client receives, and it cannot execute code on
    the way back out. ``default=str`` covers the stray date or Decimal that DRF
    would have rendered as a string anyway.
    """
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError) as error:
        _degraded("encode", error)
        return None


def _decode(payload):
    try:
        return json.loads(payload)
    except (TypeError, ValueError) as error:
        _degraded("decode", error)
        return None


def _as_it_will_be_read_back(value):
    """``value`` in the exact form a later :func:`lookup` would hand back.

    A cached read and a fresh read must be indistinguishable. On the wire they
    already are -- the JSON renderer turns a date into the same ISO string the
    cache stored -- but ``response.data`` is *not* the wire, and there a fresh
    summary carries ``date`` objects while a cached one carries strings. That
    difference is invisible to a client and poisonous in a test: the same
    assertion would pass or fail depending on whether the cache was warm.

    Normalising the fresh result through the same encode/decode the cache uses
    makes the two paths agree by construction rather than by luck. It costs one
    ``dumps``/``loads`` of a small payload on a miss -- paid against the database
    query that miss just ran.
    """
    encoded = _encode(value)
    if encoded is None:
        return value
    decoded = _decode(encoded)
    return value if decoded is None else decoded


def lookup(key: str):
    """The cached payload for ``key``, or ``None`` on a miss or an unusable entry."""
    if not enabled():
        return None

    found, payload = _get(key)
    if not found or payload is None:
        return None

    value = _decode(payload)
    if value is None:
        # Undecodable: drop it so the next reader does not pay for it again.
        _delete(key)
    return value


def remember(key: str, value, ttl: int) -> bool:
    """Store ``value`` under an already-built key. Returns whether it was stored."""
    if not enabled():
        return False
    encoded = _encode(value)
    if encoded is None:
        return False
    return _set(key, encoded, ttl)


def _release(lock_key: str, token: str) -> None:
    """Drop the lock, but only if it is still ours.

    The read and the delete are not atomic, so a very slow request could delete a
    lock another request has just taken. That costs one duplicated query and can
    never produce a wrong answer, because nothing is ever read from the lock --
    only the entry, and only after it has been written.
    """
    found, current = _get(lock_key)
    if found and current == token:
        _delete(lock_key)


def compute_and_store(key: str, producer, ttl: int, *, stampede: bool = True):
    """Run ``producer`` and cache the result, collapsing concurrent misses.

    Returns the payload *as a later read would return it* (see
    :func:`_as_it_will_be_read_back`), so that a miss and a hit are
    indistinguishable to the caller.

    With ``stampede`` off every caller computes for itself; that is only useful
    for a producer so cheap that serialising it would cost more than repeating
    it.
    """
    if not enabled():
        return _as_it_will_be_read_back(producer())

    if not stampede:
        result = producer()
        remember(key, result, ttl)
        return _as_it_will_be_read_back(result)

    lock_key = f"{key}:lock"
    token = uuid.uuid4().hex
    locked = _add(lock_key, token, _setting("API_CACHE_LOCK_SECONDS", 5))

    if locked is None:
        # The cache cannot answer, so there is no lock to take and nothing to
        # wait for. Compute now, exactly as if caching were off: an outage should
        # cost a query, never a timeout.
        return _as_it_will_be_read_back(producer())

    if locked:
        try:
            result = producer()
            remember(key, result, ttl)
            return _as_it_will_be_read_back(result)
        finally:
            _release(lock_key, token)

    # Somebody else is computing this exact answer. Wait for it rather than
    # repeat an expensive query -- but only for a moment, and never at the cost
    # of failing: a timeout here computes from the database like a plain miss.
    deadline = time.monotonic() + float(
        _setting("API_CACHE_STAMPEDE_WAIT_SECONDS", 0.5)
    )
    while time.monotonic() < deadline:
        time.sleep(STAMPEDE_POLL_SECONDS)
        found, payload = _get(key)
        if found is None:
            # The cache went away mid-wait. Waiting longer cannot produce the
            # answer, so stop paying for it.
            break
        if found:
            value = _decode(payload)
            if value is not None:
                return value
    return _as_it_will_be_read_back(producer())


def cached(
    namespace: str,
    *,
    parts: dict,
    ttl: int,
    producer,
    scope=None,
    stampede: bool = True,
):
    """The cached answer for ``parts``, computed by ``producer`` on a miss.

    The one-call form, for producers that neither need to know whether they were
    a hit nor have to run anything else on the way out.
    """
    key = build_key(namespace, parts, scope=scope)
    value = lookup(key)
    if value is not None:
        return value
    return compute_and_store(key, producer, ttl, stampede=stampede)


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #


def health():
    """``True`` if the cache answers, ``False`` if it does not, ``None`` if off.

    A probe rather than a lookup, because an absent key says nothing about the
    server: only a round trip that succeeds and reads back what was written does.
    """
    if not enabled():
        return None

    probe_key = "md:health"
    try:
        cache.set(probe_key, "ok", 30)
        return cache.get(probe_key) == "ok"
    except Exception as error:  # noqa: BLE001
        _degraded("health", error)
        return False
