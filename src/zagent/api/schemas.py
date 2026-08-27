from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionRequest(StrictRequest):
    title: str = Field(default="新任务", max_length=200)


class SendMessageRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=200_000)


class ExecuteContextToolRequest(StrictRequest):
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class UpdateModelRequest(StrictRequest):
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key: Optional[str] = None
    context_window: Optional[int] = None
    soft_limit_ratio: Optional[float] = None
    hard_limit_ratio: Optional[float] = None
    native_tool_calls: Optional[bool] = None
    temperature: Optional[float] = None

    def model_patch(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude={"api_key"})

