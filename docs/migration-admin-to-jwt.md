# Migrating from shared admin keys to JWT

Phase 18 introduced enterprise identity. Shared admin keys remain available for
emergency recovery but should not be the primary human authentication path.

## Target state

| Operation                         | Preferred auth              |
|-----------------------------------|-----------------------------|
| Login / refresh / logout          | Public auth endpoints       |
| Organization + member management  | Bearer JWT + RBAC           |
| Create/list org projects          | Bearer JWT + `projects:*`   |
| Chat completions                  | Project API key             |
| Emergency project CRUD            | Admin key (temporary)       |

## Migration steps

1. Bootstrap a platform admin:
   ```bash
   uv run python scripts/bootstrap_admin.py
   ```
2. Remove `BOOTSTRAP_ADMIN_PASSWORD` from the environment.
3. Log in via `POST /v1/auth/login` and store the refresh token securely.
4. Create an organization (`POST /v1/organizations`).
5. Create projects under that organization:
   `POST /v1/organizations/{organization_id}/projects`
6. Issue project API keys for applications.
7. Update dashboards and scripts to use JWT for human actions.
8. Keep the admin key offline for break-glass recovery only.
9. After a soak period, disable admin-key access to day-to-day routes in a
   follow-up release.

## Compatibility notes

- Existing projects may have `organization_id = NULL` until assigned.
- Assign them to an organization before relying solely on org-scoped routes.
- Project API keys continue to work for chat regardless of JWT adoption.
