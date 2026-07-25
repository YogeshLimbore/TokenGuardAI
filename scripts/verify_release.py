"""
Pre-release checks that do not need a running application.

Verifies that the files a release depends on exist, that the version is
consistent across the places that declare it, and that no secret-bearing file
is tracked by git.

    uv run python scripts/verify_release.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    ".env.example",
    ".env.production.example",
    ".dockerignore",
    ".gitleaks.toml",
    "Dockerfile",
    "Dockerfile.dashboard",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    "deploy/Caddyfile",
    "observability/prometheus.yml",
    "observability/alert_rules.yml",
    "observability/otel-collector-config.yml",
    "config/budget_policy.json",
    "config/governance_policy.json",
    "config/model_pricing.json",
    "config/routing_policy.json",
    "docs/architecture.md",
    "docs/api-guide.md",
    "docs/deployment.md",
    "docs/operations.md",
    "docs/performance.md",
    "docs/security.md",
    "docs/troubleshooting.md",
    ".github/workflows/ci.yml",
    ".github/workflows/security.yml",
    ".github/workflows/docker.yml",
    ".github/workflows/deploy.yml",
    ".github/workflows/release.yml",
    "scripts/quality_check.ps1",
    "scripts/smoke_test.py",
    "scripts/seed_demo.py",
    "scripts/backup_postgres.ps1",
]

# Tracking any of these would publish real secrets or local database state.
FORBIDDEN_TRACKED = [
    ".env",
    ".env.production",
    ".env.staging",
    "tokenguard.db",
    "bandit-report.json",
    "pip-audit-report.json",
    "coverage.xml",
]

FORBIDDEN_TRACKED_PATTERNS = [
    "*.dump",
    "*.pem",
    "*.key",
    "backups/*",
]


class ReleaseCheckError(RuntimeError):
    pass


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def check_required_files(problems: list[str]) -> None:
    missing = [path for path in REQUIRED_FILES if not (ROOT / path).exists()]

    if missing:
        _fail(
            problems,
            "Missing required release files:\n  - " + "\n  - ".join(missing),
        )


def _tracked_files() -> list[str] | None:
    """Return every git-tracked path, or None when git is unavailable."""
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def check_tracked_secrets(problems: list[str]) -> None:
    tracked = _tracked_files()

    if tracked is None:
        print("  ! git is unavailable; skipped the tracked-secret check.")
        return

    tracked_set = set(tracked)

    committed = [path for path in FORBIDDEN_TRACKED if path in tracked_set]

    for pattern in FORBIDDEN_TRACKED_PATTERNS:
        committed.extend(path for path in tracked if Path(path).match(pattern))

    if committed:
        _fail(
            problems,
            "These files are tracked by git but must never be committed:\n  - "
            + "\n  - ".join(sorted(set(committed))),
        )

    # A local .env is expected; it just must not be tracked.
    for path in FORBIDDEN_TRACKED:
        if path not in tracked_set and (ROOT / path).exists():
            print(f"  - {path} exists locally and is correctly untracked.")


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as file:
        data = tomllib.load(file)

    version = data.get("project", {}).get("version")

    if not isinstance(version, str):
        raise ReleaseCheckError("pyproject.toml does not declare project.version.")

    return version


def check_version_consistency(problems: list[str]) -> None:
    version = _project_version()

    print(f"  - pyproject version: {version}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    if f"[{version}]" not in changelog:
        _fail(
            problems,
            f"CHANGELOG.md has no entry for version {version}.",
        )

    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    match = re.search(
        r"^APP_VERSION=(.+)$",
        example,
        re.MULTILINE,
    )

    if match is None:
        _fail(
            problems,
            ".env.example does not define APP_VERSION.",
        )
    elif match.group(1).strip() != version:
        _fail(
            problems,
            f".env.example APP_VERSION is {match.group(1).strip()!r} "
            f"but pyproject declares {version!r}.",
        )


def check_no_placeholder_domains(problems: list[str]) -> None:
    """The templates may say example.com; committed real config may not."""
    production_env = ROOT / ".env.production"

    if not production_env.exists():
        return

    content = production_env.read_text(encoding="utf-8")

    if "replace_me" in content:
        _fail(
            problems,
            ".env.production still contains 'replace_me' placeholders.",
        )


def main() -> int:
    checks = (
        ("Required files", check_required_files),
        ("Tracked secrets", check_tracked_secrets),
        ("Version consistency", check_version_consistency),
        ("Production config", check_no_placeholder_domains),
    )

    problems: list[str] = []

    for name, check in checks:
        print(f"{name}...")
        check(problems)

    if problems:
        print("\nRelease verification failed:\n", file=sys.stderr)

        for problem in problems:
            print(f"* {problem}\n", file=sys.stderr)

        return 1

    print("\nRelease files verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
