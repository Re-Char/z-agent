from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
DEEPSEEK_OFFICIAL_BASE_URL = "https://api.deepseek.com"


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: "model_" + secrets.token_hex(4))
    name: str = ""
    provider: str = "echo"
    model: str = "zagent-local"
    base_url: str = ""
    api_key_env: str = "ZAGENT_API_KEY"
    context_window: int = Field(default=32_768, ge=4_096)
    soft_limit_ratio: float = Field(default=0.70, gt=0, lt=1)
    hard_limit_ratio: float = Field(default=0.82, gt=0, lt=1)
    native_tool_calls: bool = True
    temperature: float = Field(default=0.2, ge=0, le=2)

    @model_validator(mode="after")
    def validate_provider_settings(self) -> "ModelSettings":
        self.provider = self.provider.strip().lower()
        self.model = self.model.strip()
        self.base_url = self.base_url.strip().rstrip("/")
        if self.provider == "deepseek" and self.base_url in {
            DEEPSEEK_OFFICIAL_BASE_URL,
            f"{DEEPSEEK_OFFICIAL_BASE_URL}/v1",
        }:
            # The early UI suggested the provider name as a model name.  Migrate that
            # invalid value and normalize the official endpoint to DeepSeek's documented form.
            if self.model.lower() == "deepseek":
                self.model = DEEPSEEK_DEFAULT_MODEL
            self.base_url = DEEPSEEK_OFFICIAL_BASE_URL
        if self.soft_limit_ratio >= self.hard_limit_ratio:
            raise ValueError("soft_limit_ratio must be lower than hard_limit_ratio")
        if self.provider != "echo" and not self.base_url:
            raise ValueError("base_url is required for non-echo providers")
        if not self.model:
            raise ValueError("model is required")
        return self

    def display_name(self) -> str:
        return self.name or f"{self.provider} · {self.model}"


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: str
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    blob_threshold: int = Field(default=32_768, ge=1_024)
    recent_event_limit: int = Field(default=24, ge=4, le=200)
    max_tool_rounds: int = Field(default=8, ge=1, le=50)
    task_timeout_seconds: float = Field(default=300, ge=5, le=3600)
    # Backward-compatible mirror of the active profile; new configs read `models`.
    model: ModelSettings = Field(default_factory=ModelSettings)
    models: List[ModelSettings] = Field(default_factory=list)
    active_model_id: Optional[str] = None

    @model_validator(mode="after")
    def ensure_model_profiles(self) -> "AppSettings":
        if not self.models:
            # v1 configs stored a single `model` object; promote it to a profile.
            self.models = [self.model]
        if not self.active_model_id or self.active_model_id not in {m.id for m in self.models}:
            self.active_model_id = self.models[0].id
        # Keep the single `model` field in sync as the active profile mirror.
        self.model = self.active_model
        return self

    @property
    def active_model(self) -> ModelSettings:
        for profile in self.models:
            if profile.id == self.active_model_id:
                return profile
        return self.models[0] if self.models else ModelSettings()

    def upsert_model(self, patch: Dict[str, Any]) -> ModelSettings:
        """Insert or update a profile by its id (taken from patch when present).

        A newly created profile becomes the active one.
        """
        model_id = patch.pop("id", None)
        existing = next((m for m in self.models if m.id == model_id), None) if model_id else None
        if existing is not None:
            merged = ModelSettings.model_validate({**existing.model_dump(), **patch})
            self.models[self.models.index(existing)] = merged
            result = merged
        else:
            profile = ModelSettings.model_validate(patch)
            if "api_key_env" not in patch:
                # Per-model key slot so each profile can hold its own credential.
                profile.api_key_env = f"ZAGENT_MODEL_{profile.id}"
            self.models.append(profile)
            self.active_model_id = profile.id
            result = profile
        self.model = self.active_model
        return result

    def delete_model(self, model_id: str) -> bool:
        before = len(self.models)
        self.models = [m for m in self.models if m.id != model_id]
        if len(self.models) == before:
            return False
        if self.active_model_id == model_id:
            self.active_model_id = self.models[0].id if self.models else None
        self.model = self.active_model
        return True

    def activate_model(self, model_id: str) -> bool:
        if model_id not in {m.id for m in self.models}:
            return False
        self.active_model_id = model_id
        self.model = self.active_model
        return True

    @classmethod
    def load(cls, data_dir: Optional[str] = None) -> "AppSettings":
        root = data_dir or os.environ.get("ZAGENT_DATA_DIR") or str(Path.home() / ".zagent")
        config_path = Path(root) / "config.json"
        if not config_path.exists():
            return cls(data_dir=root)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["data_dir"] = root
        return cls.model_validate(payload)

    def save(self) -> None:
        root = Path(self.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        config_path = root / "config.json"
        config_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        os.chmod(config_path, 0o600)

    def update_model(self, patch: Dict[str, Any]) -> None:
        """Backward-compatible helper: apply a patch to the active profile."""
        patch = {**patch, "id": self.active_model_id}
        self.upsert_model(patch)
