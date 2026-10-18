<#
.SYNOPSIS
    Shortcuts for the Docker Compose stack: dev_up, dev_down, dev_logs, ...

.DESCRIPTION
    Two ways to use it.

    Dot-source once per session, then call the commands directly:

        . .\scripts\dev.ps1
        dev_up
        dev_logs
        dev_down

    Or run a single command without touching your session:

        .\scripts\dev.ps1 dev_up
        .\scripts\dev.ps1 dev_manage migrate

    The dev_* commands merge docker-compose.override.yml (bind-mounted source,
    DEBUG on, published ports). The prod_* commands pass -f docker-compose.yml,
    so the override is NOT loaded and you get the production shape.

    Set $env:COMPOSE to override the compose binary (default "docker compose").

.NOTES
    Mirrors the Makefile in the repository root, which provides the same targets
    for machines that have GNU make installed.
#>

# Not 'Stop': Docker writes its progress to stderr, and with ErrorActionPreference
# set to Stop PowerShell promotes that to a terminating error. Exit codes are
# checked explicitly instead (see Invoke-Stack).
$ErrorActionPreference = 'Continue'

# This file lives in <repo>\scripts, so the repository root is one level up.
$Script:RepoRoot = Split-Path -Parent $PSScriptRoot
$Script:ComposeBin = if ($env:COMPOSE) { $env:COMPOSE.Split(' ') } else { @('docker', 'compose') }

function Invoke-Stack {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Prod
    )

    $exe = $Script:ComposeBin[0]
    $prefix = @()
    if ($Script:ComposeBin.Count -gt 1) {
        $prefix = $Script:ComposeBin[1..($Script:ComposeBin.Count - 1)]
    }

    $all = @($prefix)
    if ($Prod) { $all += @('-f', 'docker-compose.yml') }
    $all += $Arguments

    Write-Host ("-> {0} {1}" -f $exe, ($all -join ' ')) -ForegroundColor DarkGray

    # Always run from the repository root, so the commands work from anywhere.
    Push-Location $Script:RepoRoot
    try {
        # Merge stderr into the host stream so Docker's progress output is shown
        # plainly rather than as red PowerShell error records. The exit code is
        # the signal that matters, and it is checked after the location is
        # restored.
        & $exe @all 2>&1 | ForEach-Object { Write-Host $_ }
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw "docker compose exited with code $exitCode"
    }
}

# --------------------------------------------------------------------------- #
# Development
# --------------------------------------------------------------------------- #

function dev_up { Invoke-Stack -Arguments @('up', '-d') }
function dev_down { Invoke-Stack -Arguments @('down') }
function dev_restart { Invoke-Stack -Arguments @('restart', 'web', 'worker') }
function dev_logs { Invoke-Stack -Arguments @('logs', '-f', '--tail=100') }
function dev_ps { Invoke-Stack -Arguments @('ps') }
function dev_shell { Invoke-Stack -Arguments @('exec', 'web', 'python', 'manage.py', 'shell') }
function dev_manage {
    $extra = @()
    if ($args) { $extra = $args }
    Invoke-Stack -Arguments (@('exec', 'web', 'python', 'manage.py') + $extra)
}
function dev_migrate { Invoke-Stack -Arguments @('run', '--rm', 'migrate') }
function dev_makemigrations { Invoke-Stack -Arguments @('exec', 'web', 'python', 'manage.py', 'makemigrations') }
function dev_test { Invoke-Stack -Arguments @('exec', 'web', 'python', '-m', 'pytest', '-q') }
function dev_build { Invoke-Stack -Arguments @('build') }

# Build, then start: the common case after changing requirements.txt or the Dockerfile.
function dev_rebuild {
    Invoke-Stack -Arguments @('build')
    Invoke-Stack -Arguments @('up', '-d')
}

# Destructive: removes the named volumes, so the database is wiped.
function dev_clean { Invoke-Stack -Arguments @('down', '-v', '--remove-orphans') }

# --------------------------------------------------------------------------- #
# Production shape (no override file)
# --------------------------------------------------------------------------- #

function prod_config { Invoke-Stack -Prod -Arguments @('config') }
function prod_build { Invoke-Stack -Prod -Arguments @('build') }
function prod_up { Invoke-Stack -Prod -Arguments @('up', '-d') }
function prod_rebuild {
    Invoke-Stack -Prod -Arguments @('build')
    Invoke-Stack -Prod -Arguments @('up', '-d')
}
function prod_down { Invoke-Stack -Prod -Arguments @('down') }

function dev_help {
    @'
Development (docker-compose.yml + docker-compose.override.yml):
  dev_build           Build the image without starting
  dev_up              Start the stack detached (image must already exist)
  dev_rebuild         Build the image, then start the stack
  dev_down            Stop and remove containers (volumes kept)
  dev_restart         Restart the web and worker processes
  dev_logs            Follow the stack logs (Ctrl+C to stop)
  dev_ps              Show container status and health
  dev_shell           Open a Django shell in the web container
  dev_manage <args>   Run an arbitrary manage.py command, e.g. dev_manage migrate
  dev_migrate         Apply migrations (the one-shot migrate service)
  dev_makemigrations  Create new migration files from model changes
  dev_test            Run the test suite in the web container
  dev_clean           Stop and DELETE volumes (destructive: wipes the database)

Production (docker-compose.yml only, no override):
  prod_config         Render the production configuration
  prod_build          Build the production image
  prod_up             Start the production shape (image must already exist)
  prod_rebuild        Build the production image, then start
  prod_down           Stop the production shape

Usage:
  . .\scripts\dev.ps1            # dot-source, then call dev_up, dev_down, ...
  .\scripts\dev.ps1 dev_up       # or run a single command
'@ | Write-Host
}

# --------------------------------------------------------------------------- #
# Dispatcher: `.\scripts\dev.ps1 dev_up` when run rather than dot-sourced.
# --------------------------------------------------------------------------- #

$dispatchable = @(
    'dev_build', 'dev_up', 'dev_rebuild', 'dev_down', 'dev_restart', 'dev_logs',
    'dev_ps', 'dev_shell', 'dev_manage', 'dev_migrate', 'dev_makemigrations',
    'dev_test', 'dev_clean', 'prod_config', 'prod_build', 'prod_up',
    'prod_rebuild', 'prod_down', 'dev_help'
)

if ($MyInvocation.InvocationName -ne '.' -and $args.Count -ge 1) {
    $name = [string]$args[0]
    if ($name -in @('help', '-h', '--help')) { $name = 'dev_help' }

    if ($dispatchable -notcontains $name) {
        Write-Host "Unknown command: $name" -ForegroundColor Red
        Write-Host ''
        dev_help
        exit 1
    }

    $rest = @()
    if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }
    & $name @rest
}
