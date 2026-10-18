"""Building blocks for the ``LOGGING`` dictionary.

Kept out of ``settings.py`` for the same reason as :mod:`config.log_filters`:
constructing the file handler and attaching handlers to loggers is logic, and
logic is easier to test when it is not buried in a configuration literal.

The API writes one line per request (see
:class:`apps.common.middleware.RequestContextMiddleware`). Beside the console,
those lines are appended to a rotating plain-text file so an operator can read
them without a log collector, and so a container's stdout being lost does not
take the request history with it. Rotation is by size, which needs no external
tool to run and no cron job to be reliable.
"""

from __future__ import annotations

from pathlib import Path

#: The console and the file share the ``verbose`` formatter, so both carry the
#: ``[request_id]`` prefix and differ only in destination.
FORMATTER = "verbose"

#: Applied to the file handler as well as the console, so the file is filtered
#: the same way -- a request id on every line, and probe noise dropped.
FILTERS = ["request_id", "suppress_probe_not_found"]


def file_log_handler(
    *,
    log_dir,
    filename="api.log",
    max_bytes=10 * 1024 * 1024,
    backup_count=10,
):
    """A rotating, UTF-8, plain-text file handler, or ``None`` if it cannot be built.

    Contract
    --------
    in : ``log_dir`` -- where the file lives; created if missing. ``filename`` --
         the file's name. ``max_bytes`` -- rotate once the file reaches this size.
         ``backup_count`` -- how many rotated files to keep (``api.log.1``,
         ``api.log.2``, ...); the oldest is discarded.

    out: a ``logging`` dict-config handler mapping, or ``None`` when the log
         directory could not be created.

    Failing to ``None`` rather than raising is deliberate: a read-only or missing
    filesystem should cost the file copy of the logs, never the process. The
    console handler still carries every line, so the application boots and logs
    normally.
    """
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    return {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(log_dir / filename),
        "maxBytes": max_bytes,
        "backupCount": backup_count,
        "formatter": FORMATTER,
        "filters": list(FILTERS),
        # Explicit rather than platform-default: on Windows the default is not
        # UTF-8, and a non-ASCII path or query string would corrupt the line.
        "encoding": "utf-8",
    }


def add_file_handler(logging_config, handler):
    """Attach ``handler`` as ``file`` to the root and to every named logger.

    The named loggers set ``propagate: False`` so that a record is emitted once,
    which also means a handler registered only on the root would never see their
    records. Each therefore needs the file handler listed by name.
    """
    logging_config["handlers"]["file"] = handler
    logging_config["root"]["handlers"].append("file")
    for logger_config in logging_config["loggers"].values():
        logger_config["handlers"].append("file")
    return logging_config
