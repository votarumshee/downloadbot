from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class Principal:
    id: str
    api_key: str
    is_admin: bool = False
    allowed_account_ids: frozenset[str] = frozenset()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOWNLOADBOT_", env_file=".env", extra="ignore", hide_input_in_errors=True)

    api_key: SecretStr
    encryption_key: SecretStr
    clients_json: SecretStr = SecretStr("[]")
    database_url: SecretStr
    data_dir: Path = Path("data")
    max_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1024, le=10 * 1024**3)
    max_duration_seconds: int = Field(default=1800, ge=1, le=24 * 3600)
    download_timeout_seconds: int = Field(default=300, ge=5, le=3600)
    retention_seconds: int = Field(default=86400, ge=60)
    max_active_jobs: int = Field(default=100, ge=1)
    max_active_jobs_per_client: int = Field(default=10, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=5)
    worker_poll_seconds: float = Field(default=1.0, ge=0.05, le=30)
    lease_seconds: int = Field(default=60, ge=15)
    account_cooldown_seconds: int = Field(default=10, ge=0)
    min_free_disk_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    ffmpeg_location: str | None = None

    @model_validator(mode="after")
    def validate_secrets(self) -> Settings:
        if len(self.api_key.get_secret_value()) < 24:
            raise ValueError("DOWNLOADBOT_API_KEY must contain at least 24 characters")
        try:
            Fernet(self.encryption_key.get_secret_value().encode("ascii"))
        except (ValueError, UnicodeError):
            raise ValueError("DOWNLOADBOT_ENCRYPTION_KEY must be a Fernet key") from None
        self.principals()
        return self

    def principals(self) -> list[Principal]:
        import re

        principals = [Principal("admin", self.api_key.get_secret_value(), True)]
        try:
            items = json.loads(self.clients_json.get_secret_value())
            if not isinstance(items, list):
                raise ValueError
            for item in items:
                if not isinstance(item, dict) or set(item) - {"id", "api_key", "allowed_account_ids"}:
                    raise ValueError
                name, key = item["id"], item["api_key"]
                accounts = item.get("allowed_account_ids", [])
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
                    raise ValueError
                if not isinstance(key, str) or len(key) < 24:
                    raise ValueError
                if not isinstance(accounts, list) or not all(isinstance(x, str) for x in accounts):
                    raise ValueError
                principals.append(Principal(name, key, False, frozenset(accounts)))
            if len({p.id for p in principals}) != len(principals):
                raise ValueError
            if len({p.api_key for p in principals}) != len(principals):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid DOWNLOADBOT_CLIENTS_JSON (unique ids and keys required)") from None
        return principals

    @property
    def files_dir(self) -> Path:
        return self.data_dir.resolve() / "files"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir.resolve() / "private"
