from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
        if self.soft_limit_ratio >= self.hard_limit_ratio:
            raise ValueError("soft_limit_ratio must be lower than hard_limit_ratio")
        if self.provider != "echo" and not self.base_url:
            raise ValueError("base_url is required for non-echo providers")
        return self


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: str
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    blob_threshold: int = Field(default=32_768, ge=1_024)
    recent_event_limit: int = Field(default=24, ge=4, le=200)
    max_tool_rounds: int = Field(default=8, ge=1, le=50)
    task_timeout_seconds: float = Field(default=300, ge=5, le=3600)
    model: ModelSettings = Field(default_factory=ModelSettings)

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
        value = self.model.model_dump()
        value.update(patch)
        self.model = ModelSettings.model_validate(value)
