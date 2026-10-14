"""
Seed a demo organization, project, and plausible usage history.

Everything created here is synthetic. The script never creates provider
credentials, so no real API key is involved and no provider is ever called.

    uv run python scripts/seed_demo.py
    uv run python scripts/seed_demo.py --reset   # delete and recreate

The generated project API key is printed once and is not recoverable
afterwards, because only its SHA-256 hash is stored.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

project_root = Path(__file__).resolve().parent.parent

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.connection import SessionLocal
from app.database.models import (
    LLMRequestLog,
    ModelRoutingLog,
    Organization,
    Project,
    SecurityAuditEvent,
    User,
)
from app.services.identity_service import IdentityService
from app.services.organization_service import (
    OrganizationService,
)
from app.services.project_service import ProjectService

DEMO_USER_EMAIL = "demo.owner@example.com"

# Printed to the console, so it must be obviously non-production.
DEMO_USER_PASSWORD = "Demo-Password-Not-For-Production-1!"

DEMO_SECURITY_REQUEST_PREFIX = "demo-security-"

DEMO_ORGANIZATION_SLUG = "demo-organization"
DEMO_ORGANIZATION_NAME = "Demo Organization"
DEMO_PROJECT_NAME = "Demo Project"

# Deterministic history so screenshots and demos are reproducible.
RANDOM_SEED = 20260725

DEMO_PROMPTS = [
    "Summarize the benefits of semantic caching.",
    "What is an API gateway?",
    "Explain rate limiting in two sentences.",
    "Write a haiku about token budgets.",
    "Describe the difference between a proxy and a gateway.",
    "How does a circuit breaker improve reliability?",
]

DEMO_RESPONSE = (
    "This is a synthetic demo response created by "
    "scripts/seed_demo.py. No provider was called."
)

DEMO_MODELS = [
    ("ollama", "llama3.2", 0.0),
    ("openrouter", "openai/gpt-4.1-mini", 0.000_18),
    ("openrouter", "anthropic/claude-3.5-haiku", 0.000_26),
]


class DemoSecurityEvent(TypedDict):
    event_type: str
    severity: str
    action: str
    categories: list[str]
    summary: str
    risk_score: float | None


DEMO_SECURITY_EVENTS: list[DemoSecurityEvent] = [
    {
        "event_type": "governance_input_blocked",
        "severity": "high",
        "action": "block",
        "categories": ["instruction_override"],
        "summary": ("Prompt-injection pattern detected in the request."),
        "risk_score": 92.0,
    },
    {
        "event_type": "pii_redacted",
        "severity": "medium",
        "action": "redact",
        "categories": ["EMAIL_ADDRESS", "PHONE_NUMBER"],
        "summary": (
            "Contact details were redacted before the request left the gateway."
        ),
        "risk_score": None,
    },
    {
        "event_type": "output_secret_detected",
        "severity": "critical",
        "action": "block",
        "categories": ["api_key"],
        "summary": (
            "The model response contained a credential-shaped "
            "string and was not returned or cached."
        ),
        "risk_score": None,
    },
]


class DemoSeedError(RuntimeError):
    pass


def _guard_environment() -> None:
    settings = get_settings()

    if settings.environment.lower() == "production":
        raise DemoSeedError(
            "Demo seeding cannot run in production. The demo user's "
            "password is printed to the console and is publicly known."
        )


def _find_demo_project(
    database: Session,
) -> Project | None:
    return database.scalars(
        select(Project).where(Project.name == DEMO_PROJECT_NAME)
    ).first()


def _reset(database: Session) -> None:
    """
    Remove demo data.

    Request and routing logs cascade from the project, but
    security_audit_events.project_id is ON DELETE SET NULL, so those rows
    survive the project deletion and have to be removed by request ID.
    """
    database.execute(
        delete(SecurityAuditEvent).where(
            SecurityAuditEvent.request_id.like(f"{DEMO_SECURITY_REQUEST_PREFIX}%")
        )
    )

    project = _find_demo_project(database)

    if project is not None:
        database.execute(delete(Project).where(Project.id == project.id))

    organization = database.scalars(
        select(Organization).where(Organization.slug == DEMO_ORGANIZATION_SLUG)
    ).first()

    if organization is not None:
        database.execute(delete(Organization).where(Organization.id == organization.id))

    user = database.scalars(select(User).where(User.email == DEMO_USER_EMAIL)).first()

    if user is not None:
        database.execute(delete(User).where(User.id == user.id))

    database.commit()
    print("Removed existing demo data.")


def _ensure_user(database: Session) -> User:
    identity_service = IdentityService()

    existing = database.scalars(
        select(User).where(
            User.email == identity_service.normalize_email(DEMO_USER_EMAIL)
        )
    ).first()

    if existing is not None:
        print(f"Demo user already exists: {existing.email}")
        return existing

    user = identity_service.create_user(
        database,
        email=DEMO_USER_EMAIL,
        display_name="Demo Owner",
        password=DEMO_USER_PASSWORD,
        is_platform_admin=False,
    )

    print(f"Demo user created: {user.email}")
    return user


def _ensure_organization(
    database: Session,
    owner: User,
) -> Organization:
    existing = database.scalars(
        select(Organization).where(Organization.slug == DEMO_ORGANIZATION_SLUG)
    ).first()

    if existing is not None:
        print(f"Demo organization already exists: {existing.slug}")
        return existing

    organization = OrganizationService().create(
        database,
        creator=owner,
        name=DEMO_ORGANIZATION_NAME,
        slug=DEMO_ORGANIZATION_SLUG,
    )

    print(f"Demo organization created: {organization.slug}")
    return organization


def _ensure_project(
    database: Session,
    organization: Organization,
) -> Project:
    existing = _find_demo_project(database)

    if existing is not None:
        print(f"Demo project already exists: {existing.name}")
        return existing

    project = ProjectService().create_project(
        database,
        name=DEMO_PROJECT_NAME,
        description=("Synthetic project created by scripts/seed_demo.py."),
        organization_id=organization.id,
        is_active=True,
        daily_token_budget=200_000,
        daily_cost_budget_usd=2.0,
        maximum_tokens_per_request=8_000,
        maximum_cost_per_request_usd=0.05,
        requests_per_minute=60,
    )

    print(f"Demo project created: {project.name}")
    return project


def _seed_usage_history(
    database: Session,
    project: Project,
    *,
    days: int = 7,
    requests_per_day: int = 12,
) -> int:
    generator = random.Random(RANDOM_SEED)  # noqa: S311

    existing = database.scalars(
        select(LLMRequestLog.id).where(LLMRequestLog.project_id == project.id)
    ).first()

    if existing is not None:
        print("Usage history already present; skipping. Use --reset to regenerate.")
        return 0

    now = datetime.now(UTC)
    created = 0

    for day_offset in range(days):
        for index in range(requests_per_day):
            provider, model, cost_per_token = generator.choice(DEMO_MODELS)

            prompt = generator.choice(DEMO_PROMPTS)

            input_tokens = generator.randint(40, 260)
            output_tokens = generator.randint(30, 400)

            # About one in eight requests is served from cache, which shows
            # up as zero cost and very low latency in the dashboard.
            cache_hit = generator.random() < 0.125

            created_at = now - timedelta(
                days=day_offset,
                hours=generator.randint(0, 23),
                minutes=generator.randint(0, 59),
            )

            request_id = f"demo-{day_offset:02d}-{index:02d}"

            database.add(
                LLMRequestLog(
                    project_id=project.id,
                    request_id=request_id,
                    provider=provider,
                    model=model,
                    prompt=prompt,
                    response=DEMO_RESPONSE,
                    input_tokens=input_tokens,
                    output_tokens=(0 if cache_hit else output_tokens),
                    total_tokens=(
                        input_tokens if cache_hit else input_tokens + output_tokens
                    ),
                    estimated_cost_usd=(
                        0.0
                        if cache_hit
                        else round(
                            (input_tokens + output_tokens) * cost_per_token,
                            6,
                        )
                    ),
                    latency_ms=(
                        generator.uniform(8.0, 45.0)
                        if cache_hit
                        else generator.uniform(180.0, 2_400.0)
                    ),
                    status="completed",
                    created_at=created_at,
                )
            )

            database.add(
                ModelRoutingLog(
                    project_id=project.id,
                    request_id=request_id,
                    automatic_routing_used=True,
                    routing_preference="balanced",
                    routing_tier=("simple" if provider == "ollama" else "balanced"),
                    original_provider=provider,
                    original_model=model,
                    selected_provider=provider,
                    selected_model=model,
                    complexity_score=generator.randint(5, 85),
                    confidence=round(generator.uniform(0.6, 0.95), 2),
                    reasons_json=json.dumps(
                        ["Synthetic routing record created by seed_demo.py."]
                    ),
                    created_at=created_at,
                )
            )

            created += 1

    database.commit()
    print(f"Created {created} synthetic request logs.")
    return created


def _seed_security_events(
    database: Session,
    project: Project,
) -> int:
    existing = database.scalars(
        select(SecurityAuditEvent.id).where(SecurityAuditEvent.project_id == project.id)
    ).first()

    if existing is not None:
        print("Security events already present; skipping.")
        return 0

    now = datetime.now(UTC)

    for offset, event in enumerate(DEMO_SECURITY_EVENTS):
        database.add(
            SecurityAuditEvent(
                project_id=project.id,
                request_id=(f"{DEMO_SECURITY_REQUEST_PREFIX}{offset:02d}"),
                correlation_id=(f"demo-correlation-{offset:02d}"),
                event_type=str(event["event_type"]),
                severity=str(event["severity"]),
                action=str(event["action"]),
                provider="openrouter",
                model="openai/gpt-4.1-mini",
                risk_score=event["risk_score"],
                detected_categories_json=json.dumps(event["categories"]),
                # Only a sanitized summary is stored; the demo deliberately
                # mirrors production behaviour of never keeping raw content.
                sanitized_summary=str(event["summary"]),
                created_at=now - timedelta(hours=offset * 5),
            )
        )

    database.commit()

    count = len(DEMO_SECURITY_EVENTS)
    print(f"Created {count} synthetic security events.")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Seed synthetic demo data for TokenGuard AI.")
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=("Delete existing demo data before seeding."),
    )
    arguments = parser.parse_args()

    _guard_environment()

    database = SessionLocal()

    try:
        if arguments.reset:
            _reset(database)

        owner = _ensure_user(database)
        organization = _ensure_organization(
            database,
            owner,
        )
        project = _ensure_project(
            database,
            organization,
        )

        _seed_usage_history(database, project)
        _seed_security_events(database, project)

        key_record, plaintext_key = ProjectService().create_api_key(
            database,
            project_id=project.id,
            name="Demo key",
            expires_at=None,
        )
    finally:
        database.close()

    print("\n" + "=" * 68)
    print("Demo data ready.")
    print("=" * 68)
    print(f"  Organization : {DEMO_ORGANIZATION_NAME}")
    print(f"  Project      : {DEMO_PROJECT_NAME}")
    print(f"  Login email  : {DEMO_USER_EMAIL}")
    print(f"  Login password: {DEMO_USER_PASSWORD}")
    print(f"  Key name     : {key_record.name}")
    print(f"\n  Project API key: {plaintext_key}")
    print(
        "\n  Save this key now. Only its hash is stored, so it cannot be shown again."
    )
    print(
        "  No provider credential was created; add one through "
        "/v1/credentials to send real requests."
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DemoSeedError as error:
        print(f"Demo seeding failed: {error}", file=sys.stderr)
        sys.exit(1)
