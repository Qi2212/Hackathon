from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./resource_control.db")
    test_key: str = os.getenv("TEST_KEY", "campus-test-key")
    admin_key: str = os.getenv("ADMIN_KEY", "campus-admin-key")
    resources_file: Path = Path(os.getenv("RESOURCES_FILE", Path(__file__).parents[1] / "resources.json"))
    queue_timeout_seconds: int = int(os.getenv("QUEUE_TIMEOUT_SECONDS", "1800"))


settings = Settings()
