"""Version cache partitions whenever their admission or output policy changes."""

import hashlib
import json
from pathlib import Path

from app.core.config import get_settings


def policy_fingerprint() -> str:
    settings = get_settings()
    payload = [
        Path(settings.policy_file).read_text(encoding="utf-8"),
        settings.output_secret_scan_enabled,
        settings.output_moderation_enabled,
        settings.input_moderation_enabled,
        settings.moderation_fail_closed,
        settings.pii_detection_enabled,
        settings.prompt_injection_detection_enabled,
        "cache-schema-v2",
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()
