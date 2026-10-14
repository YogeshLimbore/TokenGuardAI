[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[^@\s]+@[^@\s]+\.[^@\s]+$')]
    [string]$Email,

    [string]$Region = 'ap-south-1',
    [string]$StackName = 'tokenguard-free-tier',
    [string]$GitRef = 'codex/production-aws-ready',
    [string]$RepositoryUrl = 'https://github.com/YogeshLimbore/TokenGuardAI.git',
    [string]$AllowedSshCidr,
    [string]$KeyName = 'tokenguard-free-tier-key'
)

$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$repoRoot = Split-Path -Parent $PSScriptRoot
$template = Join-Path $repoRoot 'deploy/aws/cloudformation.yml'
$keyDirectory = Join-Path $repoRoot '.aws-deploy'
$keyPath = Join-Path $keyDirectory "$KeyName.pem"

function Invoke-AwsJson {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = & aws @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($output -join [Environment]::NewLine)
    }
    return ($output -join [Environment]::NewLine) | ConvertFrom-Json
}

$awsCommand = Get-Command aws -ErrorAction SilentlyContinue
if (-not $awsCommand) {
    throw 'AWS CLI v2 is required: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html'
}

$versionText = (& aws --version 2>&1) -join ''
if ($versionText -notmatch 'aws-cli/2\.(?<minor>\d+)\.') {
    throw "AWS CLI v2 is required; found '$versionText'."
}
if ([int]$Matches.minor -lt 32) {
    throw "AWS CLI 2.32 or newer is required for browser login and Free-plan verification; found '$versionText'. Update it from an Administrator PowerShell with: aws update"
}

$identity = Invoke-AwsJson -Arguments @('sts', 'get-caller-identity', '--output', 'json')
Write-Host "AWS account: $($identity.Account)"

# The account-plan API is the only reliable programmatic proof that the new
# AWS Free plan is active. Refuse to provision when an old CLI or paid account
# prevents that proof; the caller can upgrade the CLI and run this again.
$plan = Invoke-AwsJson -Arguments @('freetier', 'get-account-plan-state', '--region', 'us-east-1', '--output', 'json')
if ($plan.accountPlanType -ne 'FREE' -or $plan.accountPlanStatus -ne 'ACTIVE') {
    throw "Deployment refused: AWS reports plan type '$($plan.accountPlanType)' with status '$($plan.accountPlanStatus)'. This script creates resources only on an active FREE plan."
}

$remainingCredits = [decimal]$plan.accountPlanRemainingCredits.amount
if ($remainingCredits -lt 25) {
    throw "Deployment refused: only $remainingCredits $($plan.accountPlanRemainingCredits.unit) of Free Tier credit remains. At least 25 USD is required as a safety floor."
}
Write-Host "Free plan verified. Remaining credit: $remainingCredits $($plan.accountPlanRemainingCredits.unit)"
Write-Host "Free plan expires: $($plan.accountPlanExpirationDate)"

if (-not $AllowedSshCidr) {
    $publicIp = (Invoke-RestMethod -Uri 'https://checkip.amazonaws.com' -TimeoutSec 15).Trim()
    if ($publicIp -notmatch '^\d{1,3}(?:\.\d{1,3}){3}$') {
        throw 'Could not determine a safe public IPv4 address for the SSH rule. Pass -AllowedSshCidr x.x.x.x/32.'
    }
    $AllowedSshCidr = "$publicIp/32"
}
if ($AllowedSshCidr -notmatch '^\d{1,3}(?:\.\d{1,3}){3}/32$') {
    throw 'AllowedSshCidr must contain exactly one IPv4 address, for example 203.0.113.8/32.'
}

$keyExists = $true
try {
    Invoke-AwsJson -Arguments @('ec2', 'describe-key-pairs', '--region', $Region, '--key-names', $KeyName, '--output', 'json') | Out-Null
} catch {
    $keyExists = $false
}

if (-not $keyExists) {
    New-Item -ItemType Directory -Force -Path $keyDirectory | Out-Null
    $keyMaterial = & aws ec2 create-key-pair --region $Region --key-name $KeyName --key-type ed25519 --query KeyMaterial --output text
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the EC2 key pair.' }
    [System.IO.File]::WriteAllText($keyPath, ($keyMaterial -join [Environment]::NewLine))
    & icacls $keyPath /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
    Write-Host "Created EC2 key pair. Private key: $keyPath"
} elseif (-not (Test-Path $keyPath)) {
    throw "EC2 key pair '$KeyName' exists, but $keyPath is missing. Pass another -KeyName or supply the original private key."
}

Write-Host "Creating stack '$StackName' in $Region. No AWS resources are created before this point."
& aws cloudformation deploy `
    --region $Region `
    --stack-name $StackName `
    --template-file $template `
    --no-fail-on-empty-changeset `
    --parameter-overrides `
        "KeyName=$KeyName" `
        "AllowedSshCidr=$AllowedSshCidr" `
        'InstanceType=t3.small' `
        "RepositoryUrl=$RepositoryUrl" `
        "GitRef=$GitRef" `
        "TlsContactEmail=$Email"
if ($LASTEXITCODE -ne 0) { throw 'CloudFormation deployment failed. Check the stack events and EC2 bootstrap log.' }

$outputs = Invoke-AwsJson -Arguments @(
    'cloudformation', 'describe-stacks', '--region', $Region, '--stack-name', $StackName,
    '--query', 'Stacks[0].Outputs', '--output', 'json'
)
$values = @{}
foreach ($item in $outputs) { $values[$item.OutputKey] = $item.OutputValue }

$ready = $false
Write-Host 'Waiting for the instance to build the images and obtain HTTPS certificates (up to 25 minutes)...'
for ($attempt = 1; $attempt -le 100; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri "$($values.ApiUrl)/ready" -TimeoutSec 10 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        if ($attempt % 10 -eq 0) { Write-Host "Still bootstrapping (attempt $attempt/100)..." }
    }
    Start-Sleep -Seconds 15
}

if (-not $ready) {
    Write-Host "Bootstrap did not become healthy. Inspect it with: ssh -i `"$keyPath`" ubuntu@$($values.PublicIp) 'sudo tail -n 300 /var/log/tokenguard-bootstrap.log'"
    throw 'The EC2 instance exists, but TokenGuard did not pass its readiness check.'
}

Write-Host ''
Write-Host "Dashboard: $($values.DashboardUrl)"
Write-Host "API:       $($values.ApiUrl)"
Write-Host "SSH:       ssh -i `"$keyPath`" ubuntu@$($values.PublicIp)"
Write-Host ''
Write-Host 'Create the first administrator with:'
Write-Host "ssh -t -i `"$keyPath`" ubuntu@$($values.PublicIp) 'cd /opt/tokenguard/app && docker compose -f docker-compose.aws.yml exec api uv run --no-sync python scripts/bootstrap_admin.py --email admin@example.com'"
