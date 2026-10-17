"""Logging filters that keep operational noise off the console.

`config.settings.LOGGING` attaches these to handlers by name. They live in their
own module rather than inline in settings so they can be unit-tested and so the
filter logic stays out of a file that should read as configuration.
"""

import logging


class SuppressProbeNotFound(logging.Filter):
    """Drop ``Not Found`` warnings raised for liveness probes.

    An external platform poller requests ``/health`` every 30 seconds. The API
    does not serve that path, so Django logs a WARNING each time and the console
    becomes unreadable. Only that narrow case is dropped: a 401, 400, or 500 is
    still logged, because those represent real faults rather than a probe
    asking for a route this service never claimed to have.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING:
            return True
        message = record.getMessage()
        return not (message.startswith("Not Found:") and "/health" in message)
