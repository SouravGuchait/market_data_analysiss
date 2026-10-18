"""Django settings for the Market Data Intelligence API."""

import sys
from datetime import timedelta
from pathlib import Path

import environ

from config.logging_config import add_file_handler, file_log_handler

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "apps.accounts.apps.AccountsConfig",
    "apps.common.apps.CommonConfig",
    "apps.api.apps.ApiConfig",
    "apps.instruments.apps.InstrumentsConfig",
    "apps.marketdata.apps.MarketdataConfig",
    "apps.ingestion.apps.IngestionConfig",
    "apps.analytics.apps.AnalyticsConfig",
    "apps.audit.apps.AuditConfig",
    "apps.reports.apps.ReportsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Second so it brackets the whole request -- auth, view, response -- and can
    # read request.user on the way out (Django's middleware is LIFO on the way
    # back, so everything registered below has already run).
    "apps.common.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

# Email is the login identifier for this project, not username. Must be set
# before any migration is applied to auth or to the accounts app.
AUTH_USER_MODEL = "accounts.User"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST", default="127.0.0.1"),
        "PORT": env("DB_PORT", default="3306"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
        "TEST": {"NAME": env("DB_TEST_NAME", default="test_market_data")},
    }
}

# Tests run against in-memory SQLite. This keeps the suite usable without a
# MySQL server and avoids the test-database create/drop races that flake
# against a live MySQL instance. Dev and production stay on MySQL.
TESTING = "pytest" in sys.modules or "test" in sys.argv

if TESTING:
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
    # PBKDF2 at production cost dominates the suite's runtime; MD5 is the
    # documented test-only hasher and is never used outside tests.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication"
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "apps.common.exceptions.market_data_exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
        "register": "5/hour",
    },
    # Every list endpoint is paginated and filterable by default. An endpoint
    # that genuinely cannot be paginated sets pagination_class = None.
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardResultsPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=env.int("JWT_ACCESS_MINUTES", default=30)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

# --------------------------------------------------------------------------- #
# Read-through cache for the analytics endpoints
# --------------------------------------------------------------------------- #
# The cache never holds the only copy of anything: every entry is a memoised view
# of rows that are still in the database. That is what makes a Redis outage a
# performance event rather than an outage -- the request recomputes from MySQL.
# apps/common/cache.py enforces the same rule at call level by treating any
# backend error as a miss.
#
# Redis is opt-in so a laptop without one still gets a working cache, and so the
# default request path never pays a connect timeout. Production sets
# REDIS_CACHE_URL. A separate logical database (…/1) is used because the Celery
# broker already owns db 0: flushing one must not silence the other.
REDIS_CACHE_URL = env("REDIS_CACHE_URL", default="")

if REDIS_CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_CACHE_URL,
            "KEY_PREFIX": env("CACHE_KEY_PREFIX", default="market_data"),
            # One second, not the five-to-ten a socket will otherwise wait. A
            # cache slower than the query it is meant to save is a liability, so
            # a stalled connection gives up and lets the database answer.
            "OPTIONS": {
                "socket_connect_timeout": env.int("REDIS_CACHE_TIMEOUT", default=1),
                "socket_timeout": env.int("REDIS_CACHE_TIMEOUT", default=1),
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "market-data-dev",
            "KEY_PREFIX": env("CACHE_KEY_PREFIX", default="market_data"),
        }
    }

# Cached reads are switchable without a code change, so a deployment can turn
# them off if a stale answer is ever worse than a slow one.
API_CACHE_ENABLED = env.bool("API_CACHE_ENABLED", default=True)

# Snapshots are append-only and written by the pipeline, so this TTL is a
# backstop behind explicit invalidation rather than the primary mechanism.
API_CACHE_ANALYTICS_TTL = env.int("API_CACHE_ANALYTICS_TTL", default=300)
# The usage summary aggregates the whole price table for one owner: expensive
# enough to be worth caching, cheap enough that a 60s TTL goes unnoticed.
API_CACHE_USAGE_TTL = env.int("API_CACHE_USAGE_TTL", default=60)
# Stampede control. A request that misses takes the lock and computes; the others
# wait this long for the winner before computing for themselves, so a cold key
# costs one heavy query instead of one per concurrent request. The lock is an
# optimisation, never a correctness dependency: no request is ever blocked
# indefinitely by another.
API_CACHE_LOCK_SECONDS = env.int("API_CACHE_LOCK_SECONDS", default=5)
API_CACHE_STAMPEDE_WAIT_SECONDS = env.float("API_CACHE_STAMPEDE_WAIT_SECONDS", default=0.5)

if TESTING:
    # In-process, and cold for every test. Cached reads stay off unless a test
    # opts in with ``override_settings(API_CACHE_ENABLED=True)``.
    #
    # This is not just convenience. Django does *not* clear caches between tests,
    # and ``TestCase`` rolls the database back while primary keys restart, so a
    # warm entry keyed on "user 1" could otherwise serve one test's rows to the
    # next. Worse, several existing tests assert exact query counts
    # (``assertNumQueries(4)``) that a cache hit would silently invalidate. Making
    # the cache explicit in the tests that exercise it keeps both honest.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "market-data-tests",
        }
    }
    API_CACHE_ENABLED = False

# A request slower than this is logged at WARNING by
# apps.common.middleware.RequestContextMiddleware, so slow endpoints surface in
# the log stream without a separate monitoring stack. 0 disables the escalation.
API_SLOW_REQUEST_MS = env.int("API_SLOW_REQUEST_MS", default=1000)

# --------------------------------------------------------------------------- #
# Request log file
# --------------------------------------------------------------------------- #
# Beside the console, every log line is appended to a rotating plain-text file
# so the request history survives a container's stdout being lost and can be read
# with no log collector. Rotation is by size: when api.log reaches
# API_LOG_MAX_BYTES it is renamed to api.log.1 and a fresh file is started, with
# API_LOG_BACKUP_COUNT generations kept. The directory is ignored by Git. File
# logging is off during tests so the suite never writes to the working tree.
LOG_DIR = Path(env("DJANGO_LOG_DIR", default=str(BASE_DIR / "logs")))
API_LOG_FILENAME = env("API_LOG_FILENAME", default="api.log")
API_LOG_MAX_BYTES = env.int("API_LOG_MAX_BYTES", default=10 * 1024 * 1024)
API_LOG_BACKUP_COUNT = env.int("API_LOG_BACKUP_COUNT", default=10)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            # ``request_id`` is filled by the RequestIdFilter on the console
            # handler, so every line is attributable to one request.
            "format": "{asctime} {levelname} {name} [{request_id}] {message}",
            "style": "{",
        },
    },
    "filters": {
        # Referenced by the console handler below; defined here as a dotted
        # factory so dictConfig can build it. Without this section Django fails
        # to configure logging at all, which breaks every management command.
        "suppress_probe_not_found": {
            "()": "config.log_filters.SuppressProbeNotFound",
        },
        "request_id": {
            "()": "config.log_filters.RequestIdFilter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_id", "suppress_probe_not_found"],
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "market_data": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "market_data.tasks": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # One line per HTTP request: method, path, status, latency, trace id.
        "market_data.request": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # One line per on-demand metric/ranking computation, uncached and often
        # the expensive part of a request.
        "market_data.analytics": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # Cache degradation is reported here, throttled, so a Redis outage
        # produces one warning a minute instead of one per request.
        "market_data.cache": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# Add the rotating file handler on top of the console. Skipped under tests, and
# skipped silently if the log directory cannot be created (add_file_handler is
# only reached when file_log_handler returned a handler rather than None).
if not TESTING:
    _file_handler = file_log_handler(
        log_dir=LOG_DIR,
        filename=API_LOG_FILENAME,
        max_bytes=API_LOG_MAX_BYTES,
        backup_count=API_LOG_BACKUP_COUNT,
    )
    if _file_handler is not None:
        add_file_handler(LOGGING, _file_handler)

# --------------------------------------------------------------------------- #
# Celery / Redis
# --------------------------------------------------------------------------- #
# Redis is the broker. There is no result backend on purpose: business status
# lives on IngestionJob, so losing a Redis key can never lose a job.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=None)
CELERY_TASK_IGNORE_RESULT = True

# --- delivery semantics ---------------------------------------------------- #
# Acknowledge *after* the task returns, and hand the message back if the worker
# process dies. Together these mean at-least-once delivery: a crash re-delivers
# the job. That is only safe because processing is idempotent. Exactly-once is
# not a property a broker offers, and this project does not pretend otherwise.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# One message in flight per process. A worker that prefetches a bag of jobs
# holds them hostage if it dies.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# --- timeouts and broker resilience ---------------------------------------- #
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=30 * 60)
CELERY_TASK_SOFT_TIME_LIMIT = env.int("CELERY_TASK_SOFT_TIME_LIMIT", default=25 * 60)
# A worker must be able to start while Redis is briefly away, and reconnect.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = env.int("CELERY_BROKER_CONNECTION_MAX_RETRIES", default=10)

# --- serialization / clock ------------------------------------------------- #
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True

# Eager mode is off by default so an accidental real .delay() fails loudly in a
# test instead of silently running a load inside a request. Tests opt in per
# case, or call the task with .apply().
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = env.bool("CELERY_TASK_EAGER_PROPAGATES", default=False)

# --- domain knobs the worker reads ----------------------------------------- #
# A PROCESSING job with no heartbeat for this long is treated as abandoned.
INGESTION_STALE_AFTER_SECONDS = env.int("INGESTION_STALE_AFTER_SECONDS", default=15 * 60)
# How often the running task refreshes rows_processed/heartbeat_at.
INGESTION_HEARTBEAT_SECONDS = env.int("INGESTION_HEARTBEAT_SECONDS", default=5)
# Retry policy: attempts after the first, and the exponential base in seconds.
INGESTION_MAX_RETRIES = env.int("INGESTION_MAX_RETRIES", default=5)
INGESTION_RETRY_BACKOFF_SECONDS = env.int("INGESTION_RETRY_BACKOFF_SECONDS", default=10)
INGESTION_RETRY_BACKOFF_MAX_SECONDS = env.int(
    "INGESTION_RETRY_BACKOFF_MAX_SECONDS", default=15 * 60
)
