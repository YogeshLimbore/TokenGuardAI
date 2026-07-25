from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    Project,
    ProjectAPIKey,
    ProjectAuditLog,
)
from app.services.project_key_service import (
    ProjectKeyService,
)


class ProjectNotFoundError(LookupError):
    pass


class ProjectAPIKeyNotFoundError(LookupError):
    pass


class DuplicateProjectError(ValueError):
    pass


class ProjectService:
    def __init__(self) -> None:
        self.key_service = ProjectKeyService()

    @staticmethod
    def get_by_id(
        database: Session,
        project_id: int,
    ) -> Project:
        project = database.get(
            Project,
            project_id,
        )

        if project is None:
            raise ProjectNotFoundError(f"Project {project_id} was not found.")

        return project

    @staticmethod
    def write_audit(
        database: Session,
        *,
        project_id: int | None,
        action: str,
        succeeded: bool = True,
        details: str | None = None,
    ) -> None:
        database.add(
            ProjectAuditLog(
                project_id=project_id,
                action=action,
                succeeded=succeeded,
                details=details,
            )
        )

    def create_project(
        self,
        database: Session,
        **values: object,
    ) -> Project:
        project = Project(**values)

        try:
            database.add(project)
            database.flush()

            self.write_audit(
                database,
                project_id=project.id,
                action="project_created",
                details="Project created.",
            )

            database.commit()
            database.refresh(project)

            return project

        except IntegrityError as error:
            database.rollback()

            raise DuplicateProjectError(
                "A project with this name already exists."
            ) from error

    @staticmethod
    def list_projects(
        database: Session,
    ) -> list[Project]:
        statement = select(Project).order_by(Project.created_at.desc())

        return list(database.scalars(statement).all())

    def update_project(
        self,
        database: Session,
        *,
        project_id: int,
        updates: dict[str, object],
    ) -> Project:
        project = self.get_by_id(
            database,
            project_id,
        )

        changed_fields: list[str] = []

        for field_name, value in updates.items():
            if value is None:
                continue

            setattr(
                project,
                field_name,
                value,
            )

            changed_fields.append(field_name)

        project.updated_at = datetime.now(UTC)

        try:
            self.write_audit(
                database,
                project_id=project.id,
                action="project_updated",
                details=("Changed fields: " + ", ".join(changed_fields)),
            )

            database.commit()
            database.refresh(project)

            return project

        except IntegrityError as error:
            database.rollback()

            raise DuplicateProjectError(
                "A project with this name already exists."
            ) from error

    def create_api_key(
        self,
        database: Session,
        *,
        project_id: int,
        name: str,
        expires_at: datetime | None,
    ) -> tuple[ProjectAPIKey, str]:
        project = self.get_by_id(
            database,
            project_id,
        )

        if not project.is_active:
            raise ValueError("Cannot create a key for an inactive project.")

        plaintext_key = self.key_service.generate_key()

        key_record = ProjectAPIKey(
            project_id=project.id,
            name=name.strip(),
            key_prefix=(self.key_service.extract_prefix(plaintext_key)),
            key_hash=(self.key_service.hash_key(plaintext_key)),
            masked_key=(self.key_service.mask_key(plaintext_key)),
            is_active=True,
            expires_at=expires_at,
        )

        try:
            database.add(key_record)
            database.flush()

            self.write_audit(
                database,
                project_id=project.id,
                action="api_key_created",
                details=(f"Project API key {key_record.id} created."),
            )

            database.commit()
            database.refresh(key_record)

            return key_record, plaintext_key

        except IntegrityError:
            database.rollback()
            raise

    @staticmethod
    def list_api_keys(
        database: Session,
        *,
        project_id: int,
    ) -> list[ProjectAPIKey]:
        statement = (
            select(ProjectAPIKey)
            .where(ProjectAPIKey.project_id == project_id)
            .order_by(ProjectAPIKey.created_at.desc())
        )

        return list(database.scalars(statement).all())

    def revoke_api_key(
        self,
        database: Session,
        *,
        project_id: int,
        key_id: int,
    ) -> ProjectAPIKey:
        statement = select(ProjectAPIKey).where(
            ProjectAPIKey.id == key_id,
            ProjectAPIKey.project_id == project_id,
        )

        key_record = database.scalar(statement)

        if key_record is None:
            raise ProjectAPIKeyNotFoundError(f"Project API key {key_id} was not found.")

        key_record.is_active = False

        self.write_audit(
            database,
            project_id=project_id,
            action="api_key_revoked",
            details=(f"Project API key {key_id} revoked."),
        )

        database.commit()
        database.refresh(key_record)

        return key_record
