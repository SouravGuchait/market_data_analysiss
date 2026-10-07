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
# In another terminal:
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/
python manage.py test apps.health
```

The health endpoint returns `{"status": "ok", "database": "ok"}` only after a
real `SELECT 1` succeeds against MySQL.

## File ownership

```text
market_data_analysiss/
├── manage.py                 # Django management commands
├── config/                   # Project settings and root routing
├── apps/
│   └── health/               # Operational endpoint only
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
- `apps/health/`: the first self-contained application, limited to operational health checks.
- `apps/`: the namespace for future domain apps such as `accounts`, `instruments`,
  `ingestion`, `marketdata`, `analytics`, `audit`, and `common`.
- `templates/`, `static/`, and `media/`: project templates, static assets, and local uploaded media.
- `.env`: ignored local configuration. `.env.example`: safe key-only template.

## Commands Django would normally generate

For a fresh empty directory, Django can generate this same base layout:

```powershell
django-admin startproject config .
New-Item -ItemType Directory apps
New-Item -ItemType File apps\__init__.py
python manage.py startapp health apps\health
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
Added a health-check endpoint and a minimal API URL structure.
Create a requirements.txt with compatible, deliberately selected dependency versions.
Provide a .gitignore and explain what must never be committed.
Add a minimal smoke test and the commands to run it.
Use small steps. For each file, explain its purpose and provide the complete contents. Do not introduce Celery tasks, business logic, or all database models yet.
Never hardcoded real credentials. Verify that the project starts, the database connection works, and the health check passes before proceeding.