#!/bin/sh
#
# Container entrypoint. Waits for the database (and, when asked, the broker) to
# accept connections, optionally collects static files, then hands off to the
# real command with `exec` so the process becomes PID 1 and receives signals
# (Ctrl+C, `docker stop`) directly.
#
# Why wait at all, when compose already gates on health checks: the same image is
# intended to run against a managed database (RDS) and against `docker run`,
# where there is no depends_on. The wait is bounded, so a genuinely unreachable
# dependency fails fast and loudly instead of crash-looping in the background.
set -eu

# TCP reachability for one host:port, retried until STARTUP_TIMEOUT seconds
# elapse. Written in the interpreter rather than with nc/curl so the runtime
# image needs no extra tooling.
wait_for_tcp() {
    host="$1"
    port="$2"
    label="$3"

    echo "[entrypoint] waiting for ${label} at ${host}:${port} ..."
    attempt=1
    while [ "${attempt}" -le "${STARTUP_TIMEOUT:-60}" ]; do
        if python - "${host}" "${port}" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
with socket.create_connection((host, port), timeout=2):
    pass
PY
        then
            echo "[entrypoint] ${label} is accepting connections"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    echo "[entrypoint] timed out after ${STARTUP_TIMEOUT:-60}s waiting for ${label}" >&2
    return 1
}

if [ "${WAIT_FOR_DB:-true}" = "true" ] && [ -n "${DB_HOST:-}" ]; then
    wait_for_tcp "${DB_HOST}" "${DB_PORT:-3306}" "database"
fi

# The broker is optional: the cache falls back to the database, and a synchronous
# deployment needs no worker at all. So it is waited for only when explicitly
# asked (the worker service sets WAIT_FOR_BROKER=true; compose also gates on the
# redis health check, which is the primary guard).
if [ "${WAIT_FOR_BROKER:-false}" = "true" ] && [ -n "${REDIS_HOST:-}" ]; then
    wait_for_tcp "${REDIS_HOST}" "${REDIS_PORT:-6379}" "broker"
fi

# Static files are collected in the web container, where WhiteNoise serves them
# from STATIC_ROOT. Gated because the worker and the migrate container do not
# need them, and collecting three times over is wasted work.
if [ "${COLLECT_STATIC:-false}" = "true" ]; then
    echo "[entrypoint] collecting static files into STATIC_ROOT"
    python manage.py collectstatic --noinput
fi

exec "$@"
