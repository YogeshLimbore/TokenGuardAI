<#
.SYNOPSIS
    Deploys the production stack on the current host.

.DESCRIPTION
    Backs up PostgreSQL, pulls the pinned images, migrates the schema, brings
    the services up, and verifies the public health endpoint. Any failure
    leaves the backup in place so a restore is possible.

    Requires a populated .env.production; see .env.production.example.

    The equivalent CI path is .github/workflows/deploy.yml, which runs these
    same steps over SSH.

.PARAMETER HealthUrl
    Public health endpoint to verify. Defaults to the API_DOMAIN in
    .env.production.

.PARAMETER ImageTag
    Pin both images to this tag before deploying, e.g. 1.5.0.

.PARAMETER SkipBackup
    Skip the pre-migration backup. Only for a first deploy with no data.

.EXAMPLE
    .\scripts\deploy_production.ps1

.EXAMPLE
    .\scripts\deploy_production.ps1 -ImageTag 1.5.0
#>

param(
    [string]$HealthUrl,
    [string]$ImageTag,
    [switch]$SkipBackup,
    [string]$EnvironmentFile = ".env.production"
)

$ErrorActionPreference = "Stop"

$composeArguments = @(
    "-f", "docker-compose.yml",
    "-f", "docker-compose.prod.yml",
    "--env-file", $EnvironmentFile
)

function Invoke-Compose {
    param([string[]]$Arguments)

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & docker compose @composeArguments @Arguments
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Get-EnvironmentValue {
    param([string]$Name)

    $line = Get-Content $EnvironmentFile |
        Where-Object { $_ -match "^\s*$Name\s*=" } |
        Select-Object -First 1

    if (-not $line) {
        return $null
    }

    return ($line -split "=", 2)[1].Trim()
}

if (-not (Test-Path $EnvironmentFile)) {
    throw "$EnvironmentFile not found. Copy .env.production.example and fill it in."
}

if (Select-String -Path $EnvironmentFile -Pattern "replace_me" -Quiet) {
    throw "$EnvironmentFile still contains 'replace_me' placeholders."
}

if ($ImageTag) {
    Write-Host "==> Pinning images to $ImageTag" -ForegroundColor Cyan

    $content = Get-Content $EnvironmentFile -Raw

    $content = $content -replace `
        "(?m)^(TOKEN_GUARD_API_IMAGE=.*):[^:\r\n]*$", "`$1:$ImageTag"
    $content = $content -replace `
        "(?m)^(TOKEN_GUARD_DASHBOARD_IMAGE=.*):[^:\r\n]*$", "`$1:$ImageTag"

    # UTF-8 without a BOM: docker compose does not tolerate a BOM in an env file.
    [System.IO.File]::WriteAllText(
        (Resolve-Path $EnvironmentFile),
        $content,
        (New-Object System.Text.UTF8Encoding $false)
    )
}

$apiDomain = Get-EnvironmentValue "API_DOMAIN"

if (-not $HealthUrl) {
    if (-not $apiDomain) {
        throw "API_DOMAIN is not set in $EnvironmentFile; pass -HealthUrl instead."
    }

    $HealthUrl = "https://$apiDomain/health"
}

if (-not $SkipBackup) {
    # Runs before the migration, because that is the only point at which a
    # backup can still rescue a bad schema change.
    Write-Host "==> Backing up PostgreSQL" -ForegroundColor Cyan

    New-Item -ItemType Directory -Force -Path "backups" | Out-Null

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupPath = "backups/pre-deploy-$timestamp.dump"

    $postgresUser = Get-EnvironmentValue "POSTGRES_USER"
    $postgresDatabase = Get-EnvironmentValue "POSTGRES_DB"

    if (-not $postgresUser) { $postgresUser = "tokenguard" }
    if (-not $postgresDatabase) { $postgresDatabase = "tokenguard" }

    Invoke-Compose @(
        "exec", "-T", "postgres",
        "pg_dump", "-U", $postgresUser, "-d", $postgresDatabase,
        "-Fc", "-f", "/tmp/pre-deploy.dump"
    )

    Invoke-Compose @("cp", "postgres:/tmp/pre-deploy.dump", $backupPath)
    Invoke-Compose @("exec", "-T", "postgres", "rm", "-f", "/tmp/pre-deploy.dump")

    Write-Host "Backup written to $backupPath" -ForegroundColor Green
}

Write-Host "==> Pulling images" -ForegroundColor Cyan
Invoke-Compose @("pull")

Write-Host "==> Starting data services" -ForegroundColor Cyan
Invoke-Compose @("up", "-d", "postgres", "redis")

Write-Host "==> Running migrations" -ForegroundColor Cyan
Invoke-Compose @("run", "--rm", "api", "uv", "run", "alembic", "upgrade", "head")

Write-Host "==> Starting application services" -ForegroundColor Cyan
Invoke-Compose @("up", "-d")

Write-Host "==> Verifying $HealthUrl" -ForegroundColor Cyan

$maximumAttempts = 30

for ($attempt = 1; $attempt -le $maximumAttempts; $attempt++) {
    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 10

        if ($response.status -in @("healthy", "degraded")) {
            if ($response.status -eq "degraded") {
                Write-Host "Deployment is serving but degraded; check /health." `
                    -ForegroundColor Yellow
            }

            Write-Host ""
            Write-Host "Deployment completed successfully." -ForegroundColor Green
            Write-Host "Run the smoke test next:" -ForegroundColor Cyan
            Write-Host "  `$env:TOKEN_GUARD_API_BASE_URL = 'https://$apiDomain'"
            Write-Host "  uv run python scripts/smoke_test.py"
            exit 0
        }

        Write-Host "Attempt ${attempt}: health reported '$($response.status)'."
    }
    catch {
        Write-Host "Attempt ${attempt}: health check failed."
    }

    Start-Sleep -Seconds 5
}

Write-Host ""
Write-Host "Deployment failed health verification." -ForegroundColor Red
Write-Host "Roll back by restoring the previous image tag:" -ForegroundColor Yellow
Write-Host "  .\scripts\deploy_production.ps1 -ImageTag <previous-version> -SkipBackup"
Write-Host "See docs/deployment.md for the full rollback procedure."

throw "Deployment failed health verification."
