import re
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class OutputSecurityResult:
    safe: bool
    findings: list[str]


class OutputSecurityService:
    PATTERNS: ClassVar[dict[str, str]] = {
        "openai_style_key": (r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "tokenguard_project_key": (r"\btg_live_[A-Za-z0-9_-]{20,}\b"),
        "tokenguard_admin_key": (r"\btg_admin_[A-Za-z0-9_-]{20,}\b"),
        "bearer_token": (r"\bBearer\s+[A-Za-z0-9._~-]{20,}\b"),
        "private_key": (
            r"-----BEGIN (?:RSA |EC |OPENSSH )?"
            r"PRIVATE KEY-----"
        ),
    }

    def analyze(
        self,
        text: str,
    ) -> OutputSecurityResult:
        findings = [
            name for name, pattern in self.PATTERNS.items() if re.search(pattern, text)
        ]

        return OutputSecurityResult(
            safe=not findings,
            findings=findings,
        )
