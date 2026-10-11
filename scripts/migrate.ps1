<#
.SYNOPSIS
    Database migration workflow for the Market Data Intelligence pipeline.

.DESCRIPTION
    Wraps the Django management commands that build and inspect the schema.
    Run with no arguments to apply pending migrations.

.PARAMETER Fresh
    Drops and recreates the database, then migrates. Destroys all data.

.PARAMETER Status
    Lists every migration and whether it has been applied.

.PARAMETER Sql
    Writes the DDL for every project migration to docs/schema.sql. Useful as a
    reference or for a DBA review that cannot run Django.

.PARAMETER Check
    Verifies there are no ungenerated model changes.

.EXAMPLE
    .\scripts\migrate.ps1
    .\scripts\migrate.ps1 -Status
    .\scripts\migrate.ps1 -Fresh
    .\scripts\migrate.ps1 -Sql
    .\scripts\migrate.ps1 -Check
#>

[CmdletBinding()]
param(
    [switch]$Fresh,
    [switch]$Status,
    [switch]$Sql,
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$Python = if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" } else { ".\venv\Scripts\python.exe" }
$SchemaFile = "docs\schema.sql"

function Invoke-Manage {
    param([string[]]$Arguments)
    & $Python manage.py @Arguments
    if ($LASTEXITCODE -ne 0) { throw "manage.py $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

if (-not (Test-Path $Python)) {
    throw "Python interpreter not found at '$Python'. Activate the virtual environment first."
}

if ($Check) {
    Write-Host "Checking for ungenerated model changes..."
    Invoke-Manage @("makemigrations", "--check", "--dry-run")
    Write-Host "No ungenerated model changes." -ForegroundColor Green
    exit 0
}

if ($Status) {
    Write-Host "Migration status"
    Write-Host "----------------"
    Invoke-Manage @("showmigrations")
    exit 0
}

if ($Sql) {
    # The app migrations that build the project's own schema, in dependency
    # order. Third-party apps are excluded so the file stays reviewable.
    $apps = @(
        @{ Name = "accounts";     Migration = "0001_initial" },
        @{ Name = "instruments";  Migration = "0001_initial" },
        @{ Name = "marketdata";   Migration = "0001_initial" },
        @{ Name = "ingestion";    Migration = "0001_initial" },
        @{ Name = "analytics";    Migration = "0001_initial" },
        @{ Name = "audit";        Migration = "0001_initial" }
    )

    $out = @()
    $out += "-- Market Data Intelligence & Analytics Pipeline"
    $out += "-- Schema DDL generated from Django migrations on $(Get-Date -Format 'yyyy-MM-dd')"
    $out += "-- Target: MySQL 8.0 / InnoDB"
    $out += ""
    $out += "-- Regenerate with: .\scripts\migrate.ps1 -Sql"
    $out += ""
    $out += "SET NAMES utf8mb4;"
    $out += "SET sql_mode = 'STRICT_TRANS_TABLES';"
    $out += ""

    foreach ($app in $apps) {
        $out += "-- ============================================================"
        $out += "-- $($app.Name)"
        $out += "-- ============================================================"
        $out += (& $Python manage.py sqlmigrate $app.Name $app.Migration 2>&1 | Where-Object { $_ -notmatch "^\s*$" })
        $out += ""
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $SchemaFile) | Out-Null
    $out | Set-Content $SchemaFile -Encoding utf8
    Write-Host "Schema written to $SchemaFile ($($out.Count) lines)" -ForegroundColor Green
    exit 0
}

if ($Fresh) {
    Write-Host "Dropping and recreating the database..." -ForegroundColor Yellow
    Invoke-Manage @("flush", "--no-input")
    Write-Host "Note: 'flush' clears data. To drop and recreate the schema entirely," -ForegroundColor Yellow
    Write-Host "delete the database in MySQL and re-run this script." -ForegroundColor Yellow
}

Write-Host "Applying migrations..."
Invoke-Manage @("migrate", "--noinput")
Write-Host "Migration complete." -ForegroundColor Green
