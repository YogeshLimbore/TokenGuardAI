<#
.SYNOPSIS
    Runs every local quality gate: lint, format, types, security scans, tests.

.DESCRIPTION
    Mirrors the checks enforced by .github/workflows/ci.yml and
    .github/workflows/security.yml so failures are found before pushing.
    PostgreSQL (tokenguard_test) and Redis must be running; start them with
    `docker compose up -d postgres redis`.

.PARAMETER SkipTests
    Run only the static checks.

.PARAMETER SkipMigrationCheck
    Skip `alembic check`, which needs a reachable database.

.PARAMETER CoverageThreshold
    Minimum total coverage percentage. Defaults to the project baseline.

.EXAMPLE
    .\scripts\quality_check.ps1

.EXAMPLE
    .\scripts\quality_check.ps1 -SkipTests
#>

param(
    [switch]$SkipTests,
    [switch]$SkipMigrationCheck,
    [int]$CoverageThreshold = 85
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )

    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan

    # Tools such as bandit and pytest write progress to stderr. Windows
    # PowerShell turns that into a terminating error while
    # $ErrorActionPreference is "Stop", so the exit code is the only reliable
    # success signal here.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & $Action
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

Invoke-Step "1. Ruff lint" { uv run ruff check . }

Invoke-Step "2. Ruff formatting" { uv run ruff format --check . }

Invoke-Step "3. Type checking" { uv run mypy app }

Invoke-Step "4. Static security scan" {
    uv run bandit -r app -x tests -ll
}

Invoke-Step "5. Dependency audit" { uv run pip-audit }

if (-not $SkipTests) {
    Invoke-Step "6. Tests and coverage" {
        uv run pytest `
            --cov=app `
            --cov-branch `
            --cov-report=term-missing `
            --cov-report=xml `
            --cov-fail-under=$CoverageThreshold
    }
}

if (-not $SkipMigrationCheck) {
    # Catches a model change that has no corresponding migration, which
    # would otherwise only surface during a deployment.
    Invoke-Step "7. Migration drift" { uv run alembic check }
}

Invoke-Step "8. Release files" {
    uv run python scripts/verify_release.py
}

Write-Host ""
Write-Host "All quality checks passed." -ForegroundColor Green
