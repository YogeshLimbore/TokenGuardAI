from contextvars import ContextVar, Token

correlation_id_context: ContextVar[str | None] = ContextVar(
    "correlation_id",
    default=None,
)

project_id_context: ContextVar[int | None] = ContextVar(
    "project_id",
    default=None,
)


def get_correlation_id() -> str | None:
    return correlation_id_context.get()


def set_correlation_id(
    correlation_id: str,
) -> Token[str | None]:
    return correlation_id_context.set(correlation_id)


def reset_correlation_id(
    token: Token[str | None],
) -> None:
    correlation_id_context.reset(token)


def get_project_id() -> int | None:
    return project_id_context.get()


def set_project_id(
    project_id: int,
) -> Token[int | None]:
    return project_id_context.set(project_id)


def reset_project_id(
    token: Token[int | None],
) -> None:
    project_id_context.reset(token)
