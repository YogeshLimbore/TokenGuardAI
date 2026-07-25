from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    sessionmaker,
)

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


engine_options: dict[str, object] = {
    "pool_pre_ping": True,
}

if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {
        "check_same_thread": False,
    }
else:
    engine_options.update(
        {
            "pool_size": 10,
            "max_overflow": 20,
            "pool_timeout": 30,
            "pool_recycle": 1800,
        }
    )


engine = create_engine(
    settings.database_url,
    **engine_options,
)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_database_session() -> Generator[Session]:
    """
    Create one database session for each API request.

    The session is always closed after the request finishes.
    """

    database = SessionLocal()

    try:
        yield database
    finally:
        database.close()
