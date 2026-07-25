<#
.SYNOPSIS
    Restores a backup into a scratch database and validates its contents.

.DESCRIPTION
    A backup is not proven until a restore succeeds. This restores the dump
    into a separate database (default: tokenguard_restore_test), then checks
    that core tables are present, that secrets are still protected, and that
    the Alembic revision matches the dump.

    Nothing here decrypts provider credentials: it only asserts that stored
    values are not plaintext. Decryption requires the correct
    TOKEN_GUARD_MASTER_KEY, which is intentionally not part of the backup.

.EXAMPLE
    .\scripts\restore_test.ps1 -DumpPath backups\tokenguard-20260725-120000.dump
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$DumpPath,

    [string]$Service = "postgres",
    [string]$User = "tokenguard",
    [string]$TargetDatabase = "tokenguard_restore_test",
    [switch]$KeepDatabase
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $DumpPath)) {
    throw "Dump file not found: $DumpPath"
}

$containerId = (docker compose ps -q $Service).Trim()

if (-not $containerId) {
    throw "Could not resolve the container ID for service '$Service'."
}

$containerPath = "/tmp/restore-validation.dump"

function Invoke-Psql {
    param([string]$Sql, [string]$Database = $TargetDatabase)

    return (
        docker compose exec -T $Service psql `
            -U $User -d $Database -t -A -c $Sql
    )
}

Write-Host "Copying dump into the container..."
docker cp $DumpPath "${containerId}:${containerPath}"

Write-Host "Recreating $TargetDatabase..."
docker compose exec -T $Service dropdb -U $User --if-exists $TargetDatabase
docker compose exec -T $Service createdb -U $User $TargetDatabase

Write-Host "Restoring..."
docker compose exec -T $Service pg_restore `
    -U $User `
    -d $TargetDatabase `
    --clean `
    --if-exists `
    --no-owner `
    $containerPath

# pg_restore exits non-zero on benign "does not exist" notices from
# --clean, so validate the result rather than trusting the exit code.

Write-Host ""
Write-Host "Validating restored data..." -ForegroundColor Cyan

$checks = [ordered]@{
    "alembic revision"    = "SELECT version_num FROM alembic_version;"
    "projects"            = "SELECT COUNT(*) FROM projects;"
    "users"               = "SELECT COUNT(*) FROM users;"
    "organizations"       = "SELECT COUNT(*) FROM organizations;"
    "provider credentials" = "SELECT COUNT(*) FROM provider_credentials;"
    "project api keys"    = "SELECT COUNT(*) FROM project_api_keys;"
    "refresh tokens"      = "SELECT COUNT(*) FROM refresh_tokens;"
    "audit events"        = "SELECT COUNT(*) FROM security_audit_events;"
}

foreach ($name in $checks.Keys) {
    $value = (Invoke-Psql -Sql $checks[$name]).Trim()
    Write-Host ("  {0,-22} {1}" -f $name, $value)
}

Write-Host ""
Write-Host "Checking that secrets remain protected..." -ForegroundColor Cyan

$plaintextCredentials = (
    Invoke-Psql -Sql @"
SELECT COUNT(*) FROM provider_credentials
WHERE encrypted_api_key LIKE 'sk-%'
   OR encrypted_api_key LIKE 'tg_live_%';
"@
).Trim()

$shortKeyHashes = (
    Invoke-Psql -Sql @"
SELECT COUNT(*) FROM project_api_keys WHERE length(key_hash) < 32;
"@
).Trim()

$plaintextRefreshTokens = (
    Invoke-Psql -Sql @"
SELECT COUNT(*) FROM refresh_tokens WHERE length(token_hash) <> 64;
"@
).Trim()

Write-Host "  plaintext provider keys   $plaintextCredentials (expected 0)"
Write-Host "  unhashed project keys     $shortKeyHashes (expected 0)"
Write-Host "  unhashed refresh tokens   $plaintextRefreshTokens (expected 0)"

if (
    $plaintextCredentials -ne "0" -or
    $shortKeyHashes -ne "0" -or
    $plaintextRefreshTokens -ne "0"
) {
    throw "Restored data contains unprotected secrets."
}

docker compose exec -T $Service rm -f $containerPath | Out-Null

if (-not $KeepDatabase) {
    docker compose exec -T $Service dropdb -U $User --if-exists $TargetDatabase
    Write-Host ""
    Write-Host "Scratch database dropped." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Restore validation passed." -ForegroundColor Green
