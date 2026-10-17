<#
.SYNOPSIS
    Run and inspect the Celery worker for the ingestion queue.

.DESCRIPTION
    A thin wrapper around the `celery` CLI so the flags that matter to this
    project (the app path, the queue, and the Windows pool) live in one place
    instead of in everyone's shell history.

    `-Action worker` is the one you leave running in a terminal. Everything else
    is a short-lived inspection.

    On Windows the default pool is `solo`, because Celery's default `prefork`
    pool relies on `fork()`, which Windows does not have. `solo` runs one task at
    a time in the worker process — fine for development, and the reason a real
    deployment runs Linux workers.

.PARAMETER Action
    worker   run a worker (blocks; Ctrl+C to stop)
    status   one-shot health check: broker, database, queue depth, job counts
    inspect  ask the running workers what they are doing (active/reserved/stats)
    purge    delete every message waiting on the queue (asks for confirmation)
    list     list the registered task names

.PARAMETER Queue
    Queue to consume from or inspect. Defaults to the Celery default queue.

.PARAMETER Pool
    Worker pool implementation. Defaults to `solo` on Windows, `prefork`
    elsewhere.

.PARAMETER Concurrency
    Number of worker child processes (ignored by `solo`).

.PARAMETER LogLevel
    Celery log level.

.EXAMPLE
    .\scripts\run_worker.ps1
    .\scripts\run_worker.ps1 -LogLevel debug
    .\scripts\run_worker.ps1 -Action status
    .\scripts\run_worker.ps1 -Action inspect
    .\scripts\run_worker.ps1 -Queue ingestion -Concurrency 8
#>

[CmdletBinding()]
param(
    [ValidateSet("worker", "status", "inspect", "purge", "list")]
    [string]$Action = "worker",

    [string]$Queue = "celery",

    [ValidateSet("solo", "prefork", "threads", "gevent", "eventlet")]
    [string]$Pool = $(if ($IsWindows -or $env:OS -eq "Windows_NT") { "solo" } else { "prefork" }),

    [int]$Concurrency = 4,

    [ValidateSet("debug", "info", "warning", "error")]
    [string]$LogLevel = "info"
)

$ErrorActionPreference = "Stop"

$Python = if ($env:VIRTUAL_ENV) {
    Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
} else {
    ".\venv\Scripts\python.exe"
}
if (-not (Test-Path $Python)) {
    $Python = if ($env:VIRTUAL_ENV) {
        Join-Path $env:VIRTUAL_ENV "bin\python"
    } else {
        ".\venv\bin\python"
    }
}
if (-not (Test-Path $Python)) {
    throw "Python interpreter not found. Activate the virtual environment first."
}

# `-A config` resolves through config/__init__.py to config/celery.py's app.
$App = "config"

function Invoke-Celery {
    param([string[]]$Arguments)
    & $Python -m celery -A $App @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "celery $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

switch ($Action) {
    "worker" {
        Write-Host "Celery worker" -ForegroundColor Cyan
        Write-Host "  app        : $App"
        Write-Host "  queue      : $Queue"
        Write-Host "  pool       : $Pool (concurrency $Concurrency)"
        Write-Host "  log level  : $LogLevel"
        Write-Host ""
        Write-Host "Reading tasks from: apps/ingestion/tasks.py" -ForegroundColor DarkGray
        Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
        Write-Host ""
        Invoke-Celery @(
            "worker",
            "--loglevel=$LogLevel",
            "--pool=$Pool",
            "--concurrency=$Concurrency",
            "--queues=$Queue",
            # Recycle a child after each job so a leaked file handle or an
            # exhausted connection cannot accumulate across a long-lived worker.
            "--max-tasks-per-child=100"
        )
    }

    "status" {
        Write-Host "Queue and dependency state" -ForegroundColor Cyan
        Write-Host "--------------------------"
        # The Django command, not Celery's: it separates broker from database,
        # which Celery's own `status` does not even try to do.
        & $Python manage.py ingestion_status
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "The database is not answering; the numbers above are stale." -ForegroundColor Yellow
        }
    }

    "inspect" {
        Write-Host "Asking live workers ($Queue)..." -ForegroundColor Cyan
        # `inspect` needs a worker to answer; an empty reply usually means none
        # is running rather than that the queue is empty.
        Invoke-Celery @("inspect", "active")
        Invoke-Celery @("inspect", "reserved")
        Invoke-Celery @("inspect", "stats")
    }

    "purge" {
        Write-Host "Deleting every message on '$Queue'." -ForegroundColor Yellow
        Write-Host "IngestionJob rows are NOT touched; pending jobs stay pending." -ForegroundColor Yellow
        Invoke-Celery @("purge", "--force", "--queue=$Queue")
    }

    "list" {
        Invoke-Celery @("list")
    }
}
