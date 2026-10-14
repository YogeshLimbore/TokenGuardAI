FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.20 /uv /uvx /bin/

RUN python -m pip uninstall -y pip

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev

COPY . .

RUN groupadd --system tokenguard \
    && useradd --system --gid tokenguard --home-dir /app tokenguard \
    && chown -R tokenguard:tokenguard /app

USER tokenguard

EXPOSE 8000

CMD ["sh", "-c", "uv run --no-sync alembic upgrade head && uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port 8000"]
