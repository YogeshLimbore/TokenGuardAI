"""
Permission constants and role→permission mappings for TokenGuard RBAC.

Authorization decisions must always be based on permissions, never on
direct role-string comparisons scattered across endpoint code.
"""

from enum import StrEnum


class Permission(StrEnum):
    ORGANIZATIONS_READ = "organizations:read"
    ORGANIZATIONS_UPDATE = "organizations:update"

    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"

    PROJECTS_CREATE = "projects:create"
    PROJECTS_READ = "projects:read"
    PROJECTS_UPDATE = "projects:update"
    PROJECTS_DELETE = "projects:delete"

    PROJECT_KEYS_MANAGE = "project_keys:manage"

    CREDENTIALS_READ = "credentials:read"
    CREDENTIALS_MANAGE = "credentials:manage"

    ANALYTICS_READ = "analytics:read"
    SECURITY_EVENTS_READ = "security_events:read"

    GOVERNANCE_READ = "governance:read"
    GOVERNANCE_MANAGE = "governance:manage"

    JOBS_CREATE = "jobs:create"
    JOBS_READ = "jobs:read"
    JOBS_CANCEL = "jobs:cancel"


# Maps each role to its set of allowed permissions.
# Extend here — never scatter role checks across endpoint code.
ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "owner": set(Permission),
    "admin": {
        Permission.ORGANIZATIONS_READ,
        Permission.ORGANIZATIONS_UPDATE,
        Permission.MEMBERS_READ,
        Permission.MEMBERS_MANAGE,
        Permission.PROJECTS_CREATE,
        Permission.PROJECTS_READ,
        Permission.PROJECTS_UPDATE,
        Permission.PROJECT_KEYS_MANAGE,
        Permission.CREDENTIALS_READ,
        Permission.CREDENTIALS_MANAGE,
        Permission.ANALYTICS_READ,
        Permission.SECURITY_EVENTS_READ,
        Permission.GOVERNANCE_READ,
        Permission.GOVERNANCE_MANAGE,
        Permission.JOBS_CREATE,
        Permission.JOBS_READ,
        Permission.JOBS_CANCEL,
    },
    "developer": {
        Permission.ORGANIZATIONS_READ,
        Permission.MEMBERS_READ,
        Permission.PROJECTS_READ,
        Permission.CREDENTIALS_READ,
        Permission.CREDENTIALS_MANAGE,
        Permission.ANALYTICS_READ,
        Permission.GOVERNANCE_READ,
        Permission.JOBS_CREATE,
        Permission.JOBS_READ,
        Permission.JOBS_CANCEL,
    },
    "analyst": {
        Permission.ORGANIZATIONS_READ,
        Permission.PROJECTS_READ,
        Permission.ANALYTICS_READ,
        Permission.SECURITY_EVENTS_READ,
        Permission.GOVERNANCE_READ,
        Permission.JOBS_READ,
    },
    "viewer": {
        Permission.ORGANIZATIONS_READ,
        Permission.PROJECTS_READ,
        Permission.ANALYTICS_READ,
    },
}
