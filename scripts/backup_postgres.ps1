<#
.SYNOPSIS
    Creates a compressed PostgreSQL backup of the TokenGuard database.

.DESCRIPTION
    Runs pg_dump inside the postgres container and copies the resulting
    custom-format dump out with `docker cp`. Binary data is never piped
    through PowerShell, which would corrupt it.

    The dump does NOT contain TOKEN_GUARD_MASTER_KEY. Provider credentials
    inside it stay encrypted, so the key must be backed up separately in a
    secrets manager — without it, restored credentials cannot be decrypted.

.EXAMPLE
    .\scripts\backup_postgres.ps1
    .\scripts\backup_postgres.ps1 -Database tokenguard -OutputDirectory D:\backups
#>

param(
    [string]$OutputDirectory = "backups",
    [string]$Service = "postgres",
    [string]$User = "tokenguard",
    [string]$Database = "tokenguard"
)

$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$fileName = "$Database-$timestamp.dump"
$containerPath = "/tmp/$fileName"

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

Write-Host "Dumping $Database inside the $Service container..."

docker compose exec -T $Service pg_dump `
    -U $User `
    -d $Database `
    -Fc `
    -f $containerPath

if ($LASTEXITCODE -ne 0) {
    throw "pg_dump failed with exit code $LASTEXITCODE."
}

$containerId = (docker compose ps -q $Service).Trim()

if (-not $containerId) {
    throw "Could not resolve the container ID for service '$Service'."
}

$localPath = Join-Path $OutputDirectory $fileName

docker cp "${containerId}:${containerPath}" $localPath

if ($LASTEXITCODE -ne 0) {
    throw "docker cp failed with exit code $LASTEXITCODE."
}

docker compose exec -T $Service rm -f $containerPath | Out-Null

$sizeMb = [math]::Round((Get-Item $localPath).Length / 1MB, 2)

Write-Host "Backup created: $localPath ($sizeMb MB)" -ForegroundColor Green
Write-Host "Validate it with: .\scripts\restore_test.ps1 -DumpPath $localPath"
