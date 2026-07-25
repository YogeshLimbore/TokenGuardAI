"""
factory_boy factories for TokenGuard models.

These build unsaved ORM instances; add them to a session and commit when a
test needs persistence. Use them to avoid repeating large object-construction
blocks across tests.
"""

from __future__ import annotations

import factory

from app.database.models import (
    Organization,
    OrganizationMembership,
    Project,
    User,
)
from app.services.password_service import PasswordService

TEST_PASSWORD = "Secure-Test-Password-123!"


class UserFactory(factory.Factory):
    class Meta:
        model = User

    email = factory.Sequence(lambda number: f"user{number}@example.com")
    display_name = factory.Faker("name")
    password_hash = factory.LazyFunction(
        lambda: PasswordService().hash_password(TEST_PASSWORD)
    )
    is_active = True
    is_email_verified = True
    is_platform_admin = False


class OrganizationFactory(factory.Factory):
    class Meta:
        model = Organization

    name = factory.Sequence(lambda number: f"Test Organization {number}")
    slug = factory.Sequence(lambda number: f"test-organization-{number}")
    is_active = True


class OrganizationMembershipFactory(factory.Factory):
    class Meta:
        model = OrganizationMembership

    role = "owner"
    is_active = True


class ProjectFactory(factory.Factory):
    class Meta:
        model = Project

    name = factory.Sequence(lambda number: f"Project {number}")
    description = "Test project"
    is_active = True
    daily_token_budget = 100_000
    daily_cost_budget_usd = 1.0
    maximum_tokens_per_request = 10_000
    maximum_cost_per_request_usd = 0.05
    requests_per_minute = 30
