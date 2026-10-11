# Market Data Intelligence & Analytics Pipeline

## Bootstrap

Create and activate a virtual environment, then install the project dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the local configuration file and replace the placeholders with local-only
values:

```powershell
Copy-Item .env.example .env
```

Create a MySQL database and a least-privilege local user. Run the following in a
MySQL client after replacing the angle-bracket placeholders; never put these
values in source control.

```sql
CREATE DATABASE market_data CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'market_data_user'@'127.0.0.1' IDENTIFIED BY '<local-password>';
GRANT ALL PRIVILEGES ON market_data.* TO 'market_data_user'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Apply framework migrations, verify configuration/database connectivity, start
the development server, and run the smoke test:

```powershell
python manage.py migrate
python manage.py check --database default
python manage.py runserver
```

## Running tests

```powershell
python manage.py test
python -m pytest            # pytest is configured in .vscode/settings.json
```

Tests run against in-memory SQLite with a fast MD5 password hasher, so no
MySQL server is needed and the whole suite finishes in about a second.
Development, the health check, and production all stay on MySQL — only
`config/settings.py` sees the swap, gated on whether tests are running.

## Authentication

The API authenticates with JWT bearer tokens and authorizes with Django
`Group`s. Tokens are sent in the `Authorization: Bearer <access>` header, never
in a cookie, so the JSON API carries no CSRF surface; `CsrfViewMiddleware`
remains in place because `/admin/` still uses Django sessions.

```powershell
# Create a user and receive an access/refresh token pair.
$body = @{ username = "analyst"; email = "a@example.com"; password = "drf-portfolio-42!" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/auth/register/ -Method Post -Body $body -ContentType application/json

# Exchange credentials for tokens; the response also carries the user profile.
# Call the protected endpoint with the returned access token.
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/auth/me/ -Headers @{ Authorization = "Bearer $access" }
```

| Method | Endpoint | Access |
|---|---|---|
| `POST` | `/api/v1/auth/register/` | anonymous |
| `POST` | `/api/v1/auth/login/` | anonymous, throttled |
| `POST` | `/api/v1/auth/logout/` | authenticated |
| `POST` | `/api/v1/auth/token/refresh/` | anonymous |
| `GET` | `/api/v1/auth/me/` | authenticated |

Every response body — success or failure — uses one envelope. Errors are shaped
as `{"error": {"code": ..., "message": ..., "fields": {...}}}`.

Passwords go through Django's `AUTH_PASSWORD_VALIDATORS` and are stored hashed.
Refresh tokens are rotated on use and blacklisted on logout, so a stolen refresh
token is rejected on its next use. Access tokens expire after 15 minutes
(`JWT_ACCESS_MINUTES`).

Roles live on the `User.role` column, not in Django `Group`s. The values are
`analyst` and `administrator`, and an administrator is created by
`createsuperuser`. Roles are edited in the Django admin user change form, which
is the natural place for them because it is where the account itself is managed.

## Database schema

Seven models across six apps. The shape is a star schema: `Instrument` is the
dimension, `PriceRecord` is the fact table, and everything else hangs off a job
or a user.

```
Exchange 1 ────< Instrument 1 ────< PriceRecord          (reference data)
                                   ^
                                   │ SET_NULL (history outlives the job)
IngestionJob 1 ────┬──< PriceRecord
                   ├──< IngestionFile
                   └──< IngestionValidationError
User 1 ────────────< IngestionJob       (PROTECT, never CASCADE)
User 1 ────────────< AuditEvent         (SET_NULL, the record outlives the user)
Instrument 1 ──────< AnalyticSnapshot   (CASCADE, a snapshot is worthless without it)
```

| Model | Primary key | Notable constraint |
|---|---|---|
| `User` | `BigAutoField` | `email` unique, `USERNAME_FIELD = "email"` |
| `Exchange` | `BigAutoField` | `code` unique |
| `Instrument` | `BigAutoField` | `symbol` unique; `asset_type` indexed |
| `PriceRecord` | `BigAutoField` | unique `(instrument, trading_date)`; `high >= low`; `volume >= 0` |
| `IngestionJob` | `UUID` | non-negative row counts |
| `IngestionFile` | `UUID` | unique `(job, checksum)` — the same bytes twice is a duplicate |
| `IngestionValidationError` | `BigAutoField` | `error_code` indexed |
| `AnalyticSnapshot` | `BigAutoField` | unique `(instrument, as_of_date, metric_type, lookback_days)` |
| `AuditEvent` | `BigAutoField` | `request_id` indexed |

**Deletion policy, decided per relationship rather than globally:**

- `PROTECT` where deleting the parent would destroy history no one can recreate
  (an instrument's prices, an exchange's instruments, a user's jobs).
- `SET_NULL` where the child outlives the parent's usefulness (a price record
  survives the job that imported it; an audit row survives its user).
- `CASCADE` only where the child is meaningless alone (a snapshot without its
  instrument, a file without its job).

**Keys.** Surrogate `BigAutoField` everywhere rather than natural keys, so a
foreign reference never has to carry `(symbol, trading_date)` and renaming an
instrument doesn't break anything. 64-bit rather than 32-bit because the price
table is the one that grows without bound: 5,000 instruments x 250 trading days
x 30 years is roughly 37.5M rows and `AutoField` runs out at 2.1B. Jobs and
files use UUID primary keys so they can be referenced in logs and URLs without
leaking a sequence estimate.

**Prices are `DecimalField(18, 8)`, never floats.** Binary floating point cannot
represent `0.1` exactly, so float arithmetic silently drifts on financial
figures. Eight decimal places covers every asset class in scope — crypto quotes
to 8, forex to 5, equities to 2. `volume` is a `BigIntegerField` because
equities trade whole shares; if fractional-volume crypto is added it becomes a
migration to `DecimalField`.

**Nullable means unknown, never zero.** `adjusted_close` is nullable because a
feed may not publish an adjustment; a zero would be a real value and would
corrupt return calculations. `AnalyticSnapshot.metadata` is nullable for the
same reason.

## Creating an administrator

```powershell
python manage.py createsuperuser   # prompts for email and password
```

The role is set to `administrator` automatically. To change another user's role,
edit it in the admin at `/admin/`.

## File ownership

```text
market_data_analysiss/
├── manage.py                 # Django management commands
├── config/                   # Project settings and root routing
├── apps/
│   ├── accounts/             # Custom user model, login, roles
│   ├── analytics/            # AnalyticSnapshot
│   ├── audit/                # AuditEvent
│   ├── common/               # Shared permissions and the error envelope
│   ├── ingestion/            # IngestionJob, IngestionFile, IngestionValidationError
│   ├── instruments/          # Exchange, Instrument
│   └── marketdata/           # PriceRecord, TradingCalendar
├── scripts/                  # Local utility scripts, including sample-data generation
├── templates/                # Project-wide templates
├── static/                   # Source static assets
├── media/                    # Local user-upload destination; ignored by Git
├── .env                      # Local secrets/configuration; ignored by Git
├── .env.example              # Safe environment-variable template
├── requirements.txt          # Python dependency constraints
└── .gitignore                # Files Git must not track
```

- `manage.py`: Django management-command entry point.
- `config/`: project-level settings, URL routing, and ASGI/WSGI server entry points.
- `apps/accounts/`: the custom `User` model (email login), JWT endpoints, and the
  analyst/administrator role field.
- `apps/common/`: `IsOwner` for object-level ownership checks and the single
  `{"error": {...}}` response envelope used by every endpoint.
- `apps/instruments/`: `Exchange` and `Instrument`, the reference-data dimension table.
- `apps/marketdata/`: `PriceRecord` (the fact table) and `TradingCalendar`.
- `apps/ingestion/`: `IngestionJob` with its `IngestionFile` and
  `IngestionValidationError` children.
- `apps/analytics/`: `AnalyticSnapshot`, one metric per instrument, date, and lookback.
- `apps/audit/`: `AuditEvent`, append-only records of privileged actions.
- `thealthemplates/`, `static/`, and `media/`: project templates, static assets, and local uploaded media.
- `.env`: ignored local configuration. `.env.example`: safe key-only template.

## Commands Django would normally generate

For a fresh empty directory, Django can generate this same base layout:

```powershell
django-admin startproject config .
New-Item -ItemType Directory apps
New-Item -ItemType File apps\__init__.py
```

Later, create domain apps only when their first endpoint/model is being built:

```powershell
python manage.py startapp accounts apps\accounts
python manage.py startapp instruments apps\instruments
python manage.py startapp ingestion apps\ingestion
python manage.py startapp marketdata apps\marketdata
python manage.py startapp analytics apps\analytics
python manage.py startapp audit apps\audit
python manage.py startapp common apps\common
```
Progress that I've made:
initialized a clean app-based project structure.
Explaind which files Django generates and which folders we create ourselves.
Set up the config package and the initial Django settings.
Configure environment variables using a .env file locally and provide a safe .env.example.
Configured MySQL, static/media settings, logging, and installed apps.
Create a requirements.txt with compatible, deliberately selected dependency versions.
Provide a .gitignore and explain what must never be committed.
Add a minimal smoke test and the commands to run it.
Use small steps. For each file, explain its purpose and provide the complete contents. Do not introduce Celery tasks, business logic, or all database models yet.
Never hardcoded real credentials. Verify that the project starts, the database connection works, and the health check passes before proceeding.
