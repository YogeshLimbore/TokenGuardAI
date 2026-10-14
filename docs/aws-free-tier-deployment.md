# AWS Free Tier staging deployment

This deployment runs TokenGuard on one `t3.small` EC2 instance. It is intended
for staging, product demos, and early testing. It is not a highly available
production topology: the database, cache, API, dashboard, and seven days of
local backups share one encrypted EBS volume and one failure domain.

## What “free” means

AWS accounts created on or after July 15, 2025 can choose a Free account plan.
The plan supplies credits for up to six months and prevents normal usage bills
until the account is upgraded to Paid. The credits and plan expire earlier if
the credits are exhausted. This repository's deployment script refuses to
create resources unless the `GetAccountPlanState` API reports all of the
following:

- plan type `FREE`
- plan status `ACTIVE`
- at least USD 25 in remaining credits

The stack still consumes credit for EC2, its 30 GiB gp3 volume, the public IPv4
address, and outbound data. Delete the CloudFormation stack when you no longer
need it. An account created before July 15, 2025 does not qualify for this
guarded path because its legacy EC2 allowance covers only 1 GiB micro
instances, which cannot run this application safely.

## Architecture and limitations

The default stack contains PostgreSQL, Redis, the API, the Streamlit dashboard,
and Caddy. Caddy gets public certificates for temporary `sslip.io` hostnames,
so a paid Route 53 hosted zone is not required. PostgreSQL and Redis have no
public ports. SSH is restricted to the public `/32` address of the computer
that runs the deployment script.

To fit 2 GiB RAM, the deployment disables embedding-based semantic caching,
OpenTelemetry, Prometheus/Grafana/Jaeger, and Celery workers. Exact caching,
authentication, governance, budgets, provider routing, provider credentials,
and the dashboard remain available. The worker and scheduler are defined under
the `workers` Compose profile, but enabling them requires a larger instance.

Daily PostgreSQL backups are kept for seven days under
`/opt/tokenguard/backups` on the same EBS volume. Before real customer traffic,
move backups to a separate encrypted account or bucket and deploy the data tier
across independent infrastructure.

## Prerequisites

1. Create a new AWS account with the **Free** plan, or use an existing active
   Free plan account.
2. Install a current AWS CLI v2. The CLI must include the command
   `aws freetier get-account-plan-state`.
3. Authenticate the CLI and verify that `aws sts get-caller-identity` succeeds.
4. Push the Git ref you want to deploy to the public GitHub repository. Never
   commit `.env`, `.env.production`, provider keys, or generated EC2 keys.

## Deploy

From PowerShell in the repository root:

```powershell
.\scripts\deploy_aws_free_tier.ps1 `
  -Email you@example.com `
  -Region ap-south-1 `
  -GitRef codex/production-aws-ready
```

The script verifies the AWS Free plan before the first resource mutation. It
then creates a local EC2 private key under the ignored `.aws-deploy` directory,
deploys CloudFormation, and waits for the public readiness endpoint. Initial
image compilation on a `t3.small` can take 10–25 minutes.

The final output contains the HTTPS dashboard and API URLs, plus the SSH
command used to create the first administrator interactively. The temporary
`sslip.io` names will change if the instance is stopped and later receives a
new public IP; update `.env.production` or use your own DNS name in that case.

## Operate and remove

On the instance:

```bash
cd /opt/tokenguard/app
docker compose -f docker-compose.aws.yml ps
docker compose -f docker-compose.aws.yml logs --tail=200
sudo tail -n 300 /var/log/tokenguard-bootstrap.log
sudo systemctl list-timers tokenguard-backup.timer
```

Delete all provisioned AWS resources when the staging environment is no longer
needed:

```powershell
aws cloudformation delete-stack --region ap-south-1 --stack-name tokenguard-free-tier
aws cloudformation wait stack-delete-complete --region ap-south-1 --stack-name tokenguard-free-tier
```

The EC2 key pair is separate from the stack. After deletion, remove it from AWS
and then delete the matching local private key:

```powershell
aws ec2 delete-key-pair --region ap-south-1 --key-name tokenguard-free-tier-key
```
