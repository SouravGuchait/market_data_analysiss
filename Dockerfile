# syntax=docker/dockerfile:1
#
# Market Data Intelligence & Analytics Pipeline -- application image.
#
# One image runs all three application roles (web, worker, migrate); the command
# is chosen per service in docker-compose.yml. The build is multi-stage so the
# compiler toolchain and the MySQL client *headers* never reach the runtime
# layer, and it runs as a non-root user.
#
# Nothing secret is baked in: configuration arrives at run time through the
# environment (docker-compose passes it from the git-ignored .env). .dockerignore
# keeps .env, the virtualenv, and generated artifacts out of the build context.

# --------------------------------------------------------------------------- #
# Stage 1: build the virtualenv (compiles mysqlclient against the MySQL headers)
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# mysqlclient has no dependable wheel for every platform, so it is compiled here.
# These packages stay in this stage and are never copied to the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        default-libmysqlclient-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# --------------------------------------------------------------------------- #
# Stage 2: runtime -- only the interpreter, the venv, and the shared library
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# libmariadb3 is the runtime half of the mysqlclient build (the headers stayed in
# the builder). curl is deliberately absent; health checks use the interpreter.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmariadb3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Application code. --chown so the non-root user can read it.
COPY --chown=app:app . .

# Writable locations the app needs at run time (logs, uploads, collected static).
# Created here and owned by the app user so a bind-mount-free production run does
# not need root. The entrypoint runs collectstatic into /app/staticfiles.
RUN mkdir -p /app/logs /app/media /app/staticfiles \
    && chown -R app:app /app/logs /app/media /app/staticfiles

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER app

EXPOSE 8000

# No HEALTHCHECK here on purpose: this one image is also the worker, which serves
# no HTTP. Per-service health checks live in docker-compose.yml.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
