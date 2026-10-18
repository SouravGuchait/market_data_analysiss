"""View mixins shared across apps."""

import logging

from django.conf import settings
from rest_framework.response import Response

from apps.common import cache

#: Hit/miss is logged here rather than inside the cache backend because this is
#: the one place that knows the answer semantically: ``payload is not None`` is
#: "the body was served from cache", whereas inside the backend a ``None`` also
#: means "caching is off" or "the entry would not decode". DEBUG, because every
#: cached read produces a line and the default stream is meant to stay readable.
logger = logging.getLogger("market_data.cache")


class CachedReadMixin:
    """Serves a read through the read-through cache.

    The subclass supplies the key, the TTL, and the payload; this mixin owns the
    hit/miss bookkeeping and the ``X-Cache`` header, so every cached endpoint
    reports what happened in the same way.

    Why the subclass keeps its own ``get``/``list``: DRF checks authentication
    and permissions in ``dispatch``, *before* the handler runs. A mixin that
    short-circuited ``dispatch`` could return a cached body without ever checking
    who was asking. Hooking the handler instead means the cache is only ever
    reached by a request that already passed the same permission checks as an
    uncached one.

    The header is not decoration. ``X-Cache: HIT`` on a response is how a cache is
    shown to work -- in a test, in ``curl``, or in a production log -- without
    reading the code or attaching a profiler.
    """

    #: The cache namespace whose version invalidates this view's entries.
    cache_namespace = None

    def get_cache_key(self) -> str:
        raise NotImplementedError

    def get_cache_ttl(self) -> int:
        raise NotImplementedError

    def produce_payload(self, request, *args, **kwargs):
        """Builds the response body the way an uncached request would have."""
        raise NotImplementedError

    def cached_response(self, request, *args, **kwargs) -> Response:
        key = self.get_cache_key()
        payload = cache.lookup(key)
        hit = payload is not None

        if not hit:
            payload = cache.compute_and_store(
                key,
                lambda: self.produce_payload(request, *args, **kwargs),
                ttl=self.get_cache_ttl(),
            )

        # The ambient request id makes this line findable from the request line
        # it belongs to; the key says which answer was hit or had to be built.
        logger.debug(
            "cache %s namespace=%s key=%s",
            "HIT" if hit else "MISS",
            self.cache_namespace,
            key,
        )

        response = Response(payload)
        response["X-Cache"] = "HIT" if hit else "MISS"
        return response

    @staticmethod
    def setting(name: str, default):
        """Reads a cache knob at call time, so tests can override it."""
        return getattr(settings, name, default)
