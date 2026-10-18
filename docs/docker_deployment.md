# Dockerised deployment

How the Market Data Intelligence & Analytics Pipeline is built, run, and operated
in containers locally, and what changes for AWS. The short version:

- **One image, three roles.** `Dockerfile` builds a single application image; the
  web server, the Celery worker, and the one-shot migration job are the same image
  with different commands.
- **`docker-compose.yml` is the production shape.** No bind mounts, nothing but
  the web port published, `DEBUG` off. `docker-compose.override.yml` layers the
  development conveniences on top and is loaded automatically by a bare
  `docker compose up`.
- **Configuration is environment-only.** Secrets live in the git-ignored `.env`
  (or a secret store in the cloud), never in the image — `.dockerignore` keeps
  `.env` out of the build context.

---

## 1. Services

| Service  | Image / build      | Command (default)                                  | Ports (dev)        | Waits for                                   | Health check                          |
|----------|--------------------|----------------------------------------------------|--------------------|---------------------------------------------|---------------------------------------|
| `mysql`  | `mysql:8.0`        | mysqld (utf8mb4)                                    | `3306` (→3307 dev) | —                                           | `mysqladmin ping`                     |
| `redis`  | `redis:7-alpine`   | `redis-server --appendonly yes`                     | `6379` (→6380 dev) | —                                           | `redis-cli ping`                      |
| `migrate`| app image          | `python manage.py migrate --noinput`                | —                  | `mysql` healthy                             | exits `0` (one-shot)                  |
| `web`    | app image          | `gunicorn config.wsgi:application`                  | `8000`             | `mysql` healthy, `redis` healthy, `migrate` completed | `GET /readyz` == 200          |
| `worker` | app image          | `celery -A config worker --concurrency=4`           | —                  | `mysql` healthy, `redis` healthy, `migrate` completed | `celery inspect ping`         |

Named volumes `mysql-data` and `redis-data` hold the database and the broker's
append-only log. Both survive `docker compose down`; only `down -v` removes them.

The dependency graph is deliberately strict: **no web or worker container starts
until `migrate` has exited zero**, so a request is never served or a task run
against a half-migrated schema.

---

## 2. Quick start (development)

```powershell
# 1. Create the local configuration and fill in the placeholders.
Copy-Item .env.example .env

# 2. Optional: the development overlay publishes MySQL and Redis on host ports
#    3307 and 6380 (not the standard 3306/6379) precisely so it coexists with a
#    native install. Override with MYSQL_HOST_PORT / REDIS_HOST_PORT /
#    WEB_HOST_PORT if you prefer different ones.

# 3. Build and start everything. `migrate` runs automatically and gates web/worker.
docker compose up --build

# 4. In another terminal:
docker compose logs -f web worker        # follow the application logs
```

Once `web` reports healthy:

| URL                                   | What                                |
|---------------------------------------|-------------------------------------|
| `http://127.0.0.1:8000/healthz`       | liveness (process up)               |
| `http://127.0.0.1:8000/readyz`        | readiness + per-dependency state    |
| `http://127.0.0.1:8000/docs/`         | Swagger UI                          |
| `http://127.0.0.1:8000/admin/`        | Django admin                        |

Create an administrator and queue a load:

```powershell
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py ingestion_status     # database/broker/queue
docker compose exec worker celery -A config inspect ping
```

Stop with `docker compose down` (data kept) or `docker compose down -v` (data
destroyed).

### 2.1 Shortcuts

Typing `docker compose ...` for every routine action gets old, so the same set of
commands is provided twice: as PowerShell functions and as Make targets.

**PowerShell (works here today).** Dot-source once per session and the names
become functions:

```powershell
. .\scripts\dev.ps1
dev_build              # build the image (also after requirements.txt changes)
dev_up                 # start the stack, detached
dev_logs               # follow the stack logs (Ctrl+C to stop)
dev_manage migrate     # any manage.py command
dev_down
```

A single command can be run without touching the session:
`.\scripts\dev.ps1 dev_up`.

**GNU make (Linux, CI, the AWS hosts).** The repository-root `Makefile` exposes
the same names:

```bash
make dev_build
make dev_up
make dev_test
```

`make` is not installed on this Windows box (and `choco`/`scoop` are not present),
so install it with `winget install ezwinports.make` (GNU make 4.4; `GnuWin32.Make`
is a lighter 3.81 alternative), or just use the PowerShell script -- they are
equivalent. There is also a no-install route: run make inside a throwaway
container, which is how these recipes were verified here.

```powershell
docker run --rm -v "${PWD}:/app" -w /app alpine:3.20 `
  sh -c "apk add --no-cache make && make dev_build"
```

| Command                | Does                                                   |
|------------------------|--------------------------------------------------------|
| `dev_build`            | Build the image without starting                       |
| `dev_up`               | Start the stack detached (image must already exist)    |
| `dev_rebuild`          | Build the image, then start the stack                  |
| `dev_down`             | Stop and remove containers (volumes kept)              |
| `dev_restart`          | Restart the `web` and `worker` processes               |
| `dev_logs`             | Follow the stack logs                                  |
| `dev_ps`               | Container status and health                            |
| `dev_shell`            | Django shell in the `web` container                    |
| `dev_manage <args>`    | Run an arbitrary `manage.py` command                   |
| `dev_migrate`          | Apply migrations via the one-shot `migrate` service    |
| `dev_makemigrations`   | Create migration files from model changes              |
| `dev_test`             | Run the test suite in the `web` container              |
| `dev_clean`            | Stop and **delete volumes** (destructive)              |
| `prod_config`          | Render the production configuration                    |
| `prod_build`           | Build the production image                             |
| `prod_up`              | Start the production shape (image must already exist)  |
| `prod_rebuild`         | Build the production image, then start                 |
| `prod_down`            | Stop the production shape (no override file)           |

Notes:

- **Building and starting are separate.** `dev_up` only starts the stack; the
  image must already exist (so `dev_build` first, or `dev_rebuild` to do both).
  This is deliberately explicit — `docker compose up` with an implicit rebuild is
  a slow, surprising default when a source edit already reloads through the bind
  mount.
- `dev_clean` is the only destructive command: it drops the named volumes and
  with them the database. Everything else keeps data.
- `dev_test` needs no MySQL test database -- the suite switches to in-memory
  SQLite when it detects pytest (see `TESTING` in `config/settings.py`), so it
  runs the same inside the container as on the host.
- `prod_config` prints the **fully resolved** environment, secrets included.
  Treat its output as sensitive.

---

## 3. Configuration

Two families of variables, both fed from `.env`:

- **Application settings** (`config/settings.py`, via `django-environ`):
  `DJANGO_SECRET_KEY`, `DB_*`, `CELERY_BROKER_URL`, `REDIS_CACHE_URL`, the
  `API_CACHE_*` knobs, and so on. See `.env.example` and the tables in
  [api_optimisation.md](api_optimisation.md) and [celery_async.md](celery_async.md).
- **Compose-only substitution**: `MYSQL_*` (the server and its least-privilege
  app user), `MYSQL_HOST_PORT`, `REDIS_HOST_PORT`, `WEB_HOST_PORT`. The Django app
  does not read these.

**Precedence.** `docker-compose.yml` passes `.env` with `env_file`, then overlays
an `x-app-env` block with `environment`. Compose's `environment` wins over
`env_file`, which is why the host-oriented values in `.env` (`DB_HOST=127.0.0.1`,
`DB_USER=root`) are replaced inside the network by `DB_HOST=mysql` and the
dedicated `DB_USER=${MYSQL_APP_USER}`. Verified in the running container:

```
DB=mysql:3306/market_data user=market_data
BROKER=redis://redis:6379/0
CACHE=redis://redis:6379/1
```

The cache uses logical Redis database **1**; the broker owns **0**, so a broker
`FLUSHDB` cannot silence the cache.

---

## 4. Startup, migrations, and shutdown

1. `mysql` and `redis` start and report healthy.
2. `migrate` runs `manage.py migrate --noinput` to completion and exits.
3. `web` and `worker` start, only after step 2 succeeds.
4. `web` runs `collectstatic` in its entrypoint (WhiteNoise serves `STATIC_ROOT`),
   then `gunicorn`.
5. `worker` waits for the broker and starts consuming the default queue.

The entrypoint (`docker/entrypoint.sh`) additionally waits (bounded, default 60s)
for the database to accept a TCP connection before running the command. Compose
already gates on health checks, but the entrypoint is what makes the same image
safe under `docker run` or against a managed database that takes a while to
accept connections.

**Why migrations are their own service, not part of `web`:** if every web replica
ran `migrate` on boot, N replicas would race the same DDL. One one-shot job — and
`depends_on: service_completed_successfully` to hold the rest back — is the
serialised version. On AWS the equivalent is one ECS task definition run to
completion before the new service revision rolls out.

---

## 5. Health checks: readiness vs dependency availability

Two endpoints, on purpose (see `apps/common/health.py`):

| Endpoint    | Touches dependencies? | `200` means                       | `503` means                |
|-------------|-----------------------|-----------------------------------|----------------------------|
| `/healthz`  | no                    | the process is running            | the process is gone/wedged |
| `/readyz`   | yes                   | the database answers (see body)   | the database is down       |

`/readyz` reports each dependency separately rather than one red light:

```json
{"status": "ready", "dependencies": {"database": "up", "broker": "up", "cache": "up"}}
```

Readiness is gated on the **database alone** — the web tier can answer reads and
non-queued writes without a broker, and a cache miss is already handled by falling
back to the database. A down broker or cache is reported but does not pull a
healthy web instance out of a load balancer. The compose health checks mirror
this separation: `web` probes `/readyz`; `worker` probes the broker
(`celery inspect ping`), because a worker that cannot reach Redis can do no work;
`mysql`/`redis` probe themselves. Liveness never checks a dependency, so a
database blip cannot restart a healthy web process.

For a human, `manage.py ingestion_status` prints the same three lines plus queue
depth, job counts, and stale processing jobs.

---

## 6. Development vs production

| Concern            | Development (`docker-compose.override.yml`)     | Production (`docker-compose.yml`)                    |
|--------------------|------------------------------------------------|------------------------------------------------------|
| Source code        | bind-mounted over `/app`, `gunicorn --reload`  | baked into the image (immutable)                     |
| `DEBUG`            | on                                             | off                                                  |
| Static files       | served by gunicorn (`runserver`-like)          | WhiteNoise from `STATIC_ROOT` (collected on start)   |
| DB / Redis ports   | published on 3307 / 6380 (configurable)        | not published; reachable only on the compose network |
| Web port           | published (`8000`)                             | `expose` only; a proxy/LB terminates traffic         |
| Restart policy     | default                                        | `unless-stopped`                                     |
| Secrets            | `.env` on the host                             | secret store, injected as environment                |

Render the production shape (no override) to check it:

```powershell
docker compose -f docker-compose.yml config
```

To add a further overlay (e.g. `docker-compose.aws.yml`) append another `-f`; the
later file wins.

---

## 7. Reliability: what was verified

Each item below was checked against the running compose stack on 2026-10-18.

### 7.1 Worker and Django connect to the correct database and broker

- `docker compose exec web env` / resolved settings show `db_host=mysql`,
  `db_name=market_data`, `db_user=market_data`, broker `redis://redis:6379/0`,
  cache `redis://redis:6379/1`.
- `docker compose exec worker celery -A config inspect ping` → `pong`, `1 node
  online`.
- `docker compose exec worker python manage.py ingestion_status` → `database: up`,
  `broker: up`, `queue: 0`.
- A full round trip through the broker completed: submit → `202`, worker →
  `completed` (`accepted=3`, `attempts=1`), and the three price rows were present
  in MySQL. This proves web → broker → worker → database across three containers.

### 7.2 Secrets are not committed

- `.env` is in `.gitignore` (`git check-ignore .env` confirms); only
  `.env.example` is tracked.
- `.dockerignore` excludes `.env`/`.env.*`, so credentials cannot enter an image
  layer. Verified against the built image:
  `docker run --rm --entrypoint sh market-data-web -c "test -f /app/.env"` reports
  the file is absent, and `id` shows the process runs as non-root (`uid=999(app)`).
- Container logs were searched for the DB password and the Django secret key; no
  matches.

### 7.3 Database constraints remain effective after retries

The price table's `uniq_price_instrument_trading_date`, `chk_price_high_gte_low`,
and `chk_price_volume_non_negative` were exercised directly on the container MySQL,
and the re-ingest path was run twice through `apps.ingestion.etl.persistence`:

```
PASS duplicate (instrument, trading_date) rejected
PASS high >= low enforced
PASS volume >= 0 enforced
PASS first persist inserts
PASS second persist updates, does not insert
PASS row count stable after re-ingest
PASS exactly one row for the re-ingested date
PASS re-ingest refreshed the values
```

At-least-once redelivery is therefore safe: a repeated file updates rows in place
rather than duplicating them, and a genuine duplicate insert is still refused by
the constraint (the app upserts; it does not disable the guard).

### 7.4 Health checks distinguish readiness from dependency availability

Covered in section 5 and asserted in `apps/common/tests/test_health.py`: liveness
returns `200` even when database and broker are patched down; readiness returns
`503` only when the database is down, and names the broker and cache separately.

### 7.5 Logs carry correlation IDs without exposing credentials

Every response carries `X-Request-ID` (echoed if the caller supplies one), and one
line per request is logged with it:

```
INFO market_data.request [db7e074bbfdf421cbebe6983790a0cda] POST /api/v1/ingestion/submit/ -> 202 in 123.48ms user=1
```

The worker logs the same job id and the Celery task id. A grep of all container
logs for the DB password and the secret key found nothing. (Note for a hardened
deployment: put credentials in a Redis URL only over TLS to a private endpoint, as
in section 8, and avoid `-v`/debug logging of the settings module.)

### 7.6 Backups, recovery, and schema-migration risks

Documented in sections 9 and 10. Not “verified” by a restore drill here — this is
the one reliability item that is a procedure, not an asserted fact.

### 7.7 Defect found and fixed during this verification

Running against **real Redis** surfaced a latent bug that LocMemCache had been
hiding: on the first invalidation of a namespace the version counter is absent, so
`apps/common/cache.py` seeded a 122-bit `uuid4().int`. Redis advances the counter
with `INCR`, a signed 64-bit operation, so the *next* invalidation failed with
`value is not an integer or out of range` and the namespace could never be
invalidated again — cached analytics would be served stale until TTL. LocMemCache
increments an unbounded Python int and never noticed.

Fixed by bounding the fallback version to 62 bits and re-seeding a counter that
cannot be incremented. Covered by two new tests in
`apps/common/tests/test_cache.py`; confirmed in the container that a seeded
version is now 61 bits and `INCR` succeeds twice. See also
[scalability_investigation.md](scalability_investigation.md) for the wider
evidence-rules context.

---

## 8. AWS deployment notes

The same image and compose file map onto AWS with the dependency services moved to
managed offerings.

| Local service    | AWS substitute                                  | Notes |
|------------------|-------------------------------------------------|-------|
| `mysql`          | Amazon RDS for MySQL 8.0                        | Managed backups/PITR; set `sql_mode=STRICT_TRANS_TABLES` in the parameter group; put it in a private subnet. |
| `redis` (broker) | ElastiCache for Redis (or Valkey) with AUTH+TLS | `rediss://:PASSWORD@endpoint:6379/0`. |
| `redis` (cache)  | Same cluster, db 1 (or a separate cluster)      | `rediss://:PASSWORD@endpoint:6379/1`. |
| image            | Amazon ECR                                      | Tag by git SHA; push the same image the local build produced. |
| `web`            | ECS Fargate service behind an ALB               | Target-group health check path `/readyz`, matcher `200`; scale on CPU/ALB latency. |
| `worker`         | ECS Fargate service                             | Scale on queue depth (Redis `LLEN`), not CPU. |
| `migrate`        | ECS task definition run to completion           | Run once per deploy, before the new service revisions. |

Environment remap for the cloud (compose `environment` or ECS task definition):

```
DB_HOST=<rds-endpoint>            DB_USER=<app-user>       DB_PASSWORD=<secret>
CELERY_BROKER_URL=rediss://:PASSWORD@<elasticache>:6379/0
REDIS_CACHE_URL=rediss://:PASSWORD@<elasticache>:6379/1
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=<alb-dns>,<api-host>
DJANGO_SECRET_KEY=<secret>
```

Provide the `*_PASSWORD`/`DJANGO_SECRET_KEY` values from **AWS Secrets Manager** or
**SSM Parameter Store**, injected with the ECS `secrets` block — never as plaintext
`environment` entries, and never in the image.

Two AWS-specific gaps to close before going live:

1. **Shared media storage.** Ingestion uploads are written to `MEDIA_ROOT`, a local
   path. On Fargate, `web` and `worker` are *separate tasks with separate ephemeral
   filesystems*, so a file uploaded through `web` is not visible to `worker`. Use
   EFS mounted at `MEDIA_ROOT` (both tasks) or S3 via `django-storages`. This does
   not arise in local compose, where the bind mount is shared — but it will break
   the async path in the cloud.
2. **Static files.** WhiteNoise (already enabled when `DEBUG=False`) serves the
   admin's assets from the image; for a CDN, add S3 + CloudFront and a
   `ManifestStaticFilesStorage`. `collectstatic` already runs in the web entrypoint.
3. **Logging.** Container stdout already carries the request/worker lines with
   correlation ids; ship them to CloudWatch Logs via the ECS `awslogs` driver.

---

## 9. Backups and recovery

**What must be backed up**

| Data              | Where              | Lose it and…                                            | Backup |
|-------------------|--------------------|--------------------------------------------------------|--------|
| Business rows     | MySQL `mysql-data` | you lose jobs, prices, analytics — irreplaceable        | required |
| Broker messages   | Redis `redis-data` | queued-but-unstarted jobs are stranded, but **not lost** | optional |
| Cache entries     | Redis db 1         | nothing; it is a memoised view of MySQL                 | not backed up |

The database is the single source of truth. The broker can be flushed and the
pending jobs re-enqueued from `IngestionJob` rows; the cache can be dropped
entirely and rebuilds on demand. That asymmetry is why only MySQL needs a real
backup.

**Backup (logical dump from the running container):**

```powershell
docker compose exec -T mysql sh -c `
  'exec mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers market_data' `
  | Set-Content -Encoding utf8 backups\market_data.sql
```

`--single-transaction` gives a consistent snapshot of InnoDB tables without
locking writers. On AWS, RDS automated backups + PITR replace this.

**Restore:**

```powershell
Get-Content backups\market_data.sql | docker compose exec -T mysql sh -c `
  'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" market_data'
```

**Recovering stranded jobs** after a broker outage or worker crash — the queue is
*not* the record of truth:

```powershell
docker compose exec web python manage.py ingestion_status        # see pending + stale
docker compose exec web python manage.py requeue_stale_jobs      # re-enqueue stalled jobs
```

`requeue_stale_jobs` finds `processing` jobs whose heartbeat has gone cold and
`pending` jobs with no message, and re-enqueues them; because the task is
idempotent, a job that actually completed is a no-op on re-run.

---

## 10. Schema-migration risks

The `migrate` service makes applying migrations reproducible, but not risk-free.
The risks that matter for this schema:

- **DDL is not transactional on MySQL.** Each `ALTER TABLE` auto-commits, so a
  migration that changes several tables can fail halfway and leave the schema
  part-applied. Django will not roll back the completed statements. Prefer one
  logical change per migration and rehearse on a restored copy.
- **Table size.** `PriceRecord` is the only table that grows without bound
  (tens of millions of rows). Adding a column rewrites it; adding an index takes a
  long time and blocks writes on some MySQL versions. For large changes use
  MySQL 8 online DDL where possible, and consider a tool that does the copy out of
  band. Test the *duration*, not just the correctness.
- **Expand/contract.** Add a nullable column and backfill in a separate step; do
  not add a `NOT NULL` column with a default and an index in the same deploy as the
  code that uses it. This keeps the previous and the new application revision both
  able to run during a rolling deploy.
- **Rollback means re-migrating.** There are no down-migration scripts here; a
  rollback is restore-from-backup or a hand-written reverse migration. Decide which
  *before* applying, and take a backup first
  (`mysqldump` above, or an RDS snapshot).
- **Never run migrations from more than one place.** The compose `migrate` service
  and the AWS one-off task exist to serialise this; do not also let web replicas
  migrate on boot.
- **Validate before deploy.** `python manage.py makemigrations --check --dry-run`
  (wrapped by `scripts/migrate.ps1 -Check`) fails the build if a model changed
  without a migration, so the deployed schema cannot silently drift from the code.
