from __future__ import annotations

import logging
import logging.config
import sys
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from app.core.config import get_settings
from app.observability.context import (
    get_correlation_id,
    get_project_id,
)


class TokenGuardJSONFormatter(JsonFormatter):
    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(
            log_record,
            record,
            message_dict,
        )

        log_record["level"] = record.levelname

        log_record["logger"] = record.name

        log_record["correlation_id"] = get_correlation_id()

        log_record["project_id"] = get_project_id()

        if not log_record.get("message"):
            log_record["message"] = record.getMessage()


def configure_logging() -> None:
    settings = get_settings()

    formatter_name = "json" if settings.log_format.lower() == "json" else "standard"

    configuration = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": TokenGuardJSONFormatter,
                "format": ("%(asctime)s %(levelname)s %(name)s %(message)s"),
            },
            "standard": {
                "format": ("%(asctime)s %(levelname)s %(name)s %(message)s"),
            },
        },
        "handlers": {
            "console": {
                "class": ("logging.StreamHandler"),
                "formatter": formatter_name,
                "stream": sys.stdout,
            },
        },
        "root": {
            "handlers": ["console"],
            "level": settings.log_level.upper(),
        },
        "loggers": {
            # Presidio echoes the text it analyzes (including the PII it
            # just detected) at DEBUG level. Pinning these loggers above
            # DEBUG keeps raw PII out of logs even when the rest of the
            # application is switched to debug logging.
            **{
                pii_logger_name: {
                    "level": "WARNING",
                    "propagate": True,
                }
                for pii_logger_name in (
                    "presidio-analyzer",
                    "presidio-anonymizer",
                )
            },
            "uvicorn.access": {
                "handlers": ["console"],
                "level": settings.log_level.upper(),
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["console"],
                "level": settings.log_level.upper(),
                "propagate": False,
            },
        },
    }

    logging.config.dictConfig(configuration)
