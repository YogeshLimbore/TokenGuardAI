# AWS Free Tier staging deployment

This deployment runs TokenGuard on one `t3.small` EC2 instance in the AWS Free
Plan project account. It is intended
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

The default stack contains PostgreSQL, Redis, the API, and the Streamlit
dashboard. No application, database, or cache port is public. The API and
dashboard bind only to the EC2 loopback interface and are reached through an
encrypted SSH tunnel. SSH is restricted to the public `/32` address of the
computer that runs the deployment script.

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

1. Finish AWS Free Plan signup and create a project. A project contains the AWS
   account in which the stack is deployed. For an Asia Pacific contact address,
   AWS currently assigns the project account to `ap-southeast-2` (Sydney).
2. Install a current AWS CLI v2. The CLI must include the command
   `aws freetier get-account-plan-state`.
3. Authenticate the CLI and verify that `aws sts get-caller-identity` succeeds.
4. Push the Git ref you want to deploy to the public GitHub repository. Never
   commit `.env`, `.env.production`, provider keys, or generated EC2 keys.

## Deploy

From PowerShell in the repository root:

```powershell
.\scripts\deploy_aws_free_tier.ps1 `
  -Profile Tokenguard `
  -Region ap-southeast-2 `
  -GitRef codex/production-aws-ready
```

The script verifies the AWS Free plan before the first resource mutation. It
then creates a local EC2 private key under the ignored `.aws-deploy` directory,
deploys CloudFormation, and checks readiness through restricted SSH. Initial
image compilation on a `t3.small` can take 10–30 minutes.

The final output contains the SSH tunnel command, local dashboard and API URLs,
and the SSH command used to create the first administrator interactively. Keep
the tunnel window open while using TokenGuard:

```powershell
ssh -N -L 8000:127.0.0.1:8000 -L 8501:127.0.0.1:8501 `
  -i .\.aws-deploy\tokenguard-free-tier-key.pem ubuntu@PUBLIC_IP
```

Then open `http://127.0.0.1:8501` for the dashboard or
`http://127.0.0.1:8000` for the API. The SSH tunnel encrypts traffic between
your computer and EC2 without exposing either service to the internet.

## Operate and remove

On the instance:

```bash
cd /opt/tokenguard/app
docker compose --env-file .env.production -f docker-compose.aws.yml ps
docker compose --env-file .env.production -f docker-compose.aws.yml logs --tail=200
sudo tail -n 300 /var/log/tokenguard-bootstrap.log
sudo systemctl list-timers tokenguard-backup.timer
```

Delete all provisioned AWS resources when the staging environment is no longer
needed:

```powershell
aws cloudformation delete-stack --profile Tokenguard --region ap-southeast-2 --stack-name tokenguard-free-tier
aws cloudformation wait stack-delete-complete --profile Tokenguard --region ap-southeast-2 --stack-name tokenguard-free-tier
```

The EC2 key pair is separate from the stack. After deletion, remove it from AWS
and then delete the matching local private key:

```powershell
aws ec2 delete-key-pair --profile Tokenguard --region ap-southeast-2 --key-name tokenguard-free-tier-key
```
