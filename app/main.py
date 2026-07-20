from fastapi import FastAPI

from app.api import chat, health
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name, version=settings.app_version)

app.include_router(health.router)
app.include_router(chat.router)


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.debug)


if __name__ == "__main__":
    main()
