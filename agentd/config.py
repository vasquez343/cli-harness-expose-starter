from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str
    workspace: Path
    provider: str = "codex"
    allow_unsafe_cli: bool = False
    idle_timeout: float = 120
    total_timeout: float = 900
    max_output_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if self.provider not in {"codex", "claude"}:
            raise ValueError("HARNESS_PROVIDER must be codex or claude")
        if min(self.idle_timeout, self.total_timeout, self.max_output_bytes) <= 0:
            raise ValueError("Timeouts and output limit must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            token=os.getenv("AGENTD_TOKEN", ""),
            workspace=Path(os.getenv("HARNESS_WORKSPACE", "workspace")).resolve(),
            provider=os.getenv("HARNESS_PROVIDER", "codex"),
            allow_unsafe_cli=os.getenv("HARNESS_ALLOW_UNSAFE_CLI") == "1",
            idle_timeout=float(os.getenv("HARNESS_IDLE_TIMEOUT", "120")),
            total_timeout=float(os.getenv("HARNESS_TOTAL_TIMEOUT", "900")),
            max_output_bytes=int(os.getenv("HARNESS_MAX_OUTPUT_BYTES", "8388608")),
        )
