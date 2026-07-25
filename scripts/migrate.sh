#!/usr/bin/env sh
# Run database migrations independently of the API process.
# Prefer this in production rollouts so schema changes land before traffic
# hits a new application version.
set -eu

cd "$(dirname "$0")/.."

echo "Applying Alembic migrations to head..."
uv run alembic upgrade head
echo "Migrations complete."
