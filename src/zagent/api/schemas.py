from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionRequest(StrictRequest):
    title: str = Field(default="新任务", max_length=200)
    workspace_id: Optional[str] = None


class CreateWorkspaceRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(default="", max_length=1000)


class UpdateWorkspaceRequest(StrictRequest):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    path: Optional[str] = Field(default=None, max_length=1000)


class SendMessageRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=200_000)
    expected_context_version: Optional[int] = Field(default=None, ge=0)


class ExecuteContextToolRequest(StrictRequest):
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class CreateMemoryRequest(StrictRequest):
    memory_type: str = Field(pattern=r"^(episodic|semantic|procedural)$")
    memory_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=8000)
    source_event_ids: List[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)
    scope: str = Field(default="workspace", pattern=r"^(workspace|user)$")
    confidence: float = Field(default=0.8, ge=0, le=1)
    confirmed: bool = False
    pinned: bool = False
    expires_at: Optional[str] = None


class ConfirmMemoryRequest(StrictRequest):
    supersedes_memory_id: Optional[str] = Field(default=None, min_length=5, max_length=80)


class UpdateMemoryRequest(StrictRequest):
    pinned: bool
    expected_pinned: bool


class CorrectMemoryRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=8000)
    reason: str = Field(min_length=1, max_length=1000)


class ForgetMemoryRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=1000)


class UpdateModelRequest(StrictRequest):
    name: Optional[str] = None
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


class CreateExtensionRequest(StrictRequest):
    id: str = Field(min_length=3, max_length=128)
    name: str = Field(default="", max_length=120)
    version: str = Field(default="0.0.0", max_length=32)
    runtime: str = Field(default="declarative", max_length=24)
    entry: Optional[str] = Field(default=None, max_length=500)
    contributes: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    enabled: bool = True

    def spec(self) -> Dict[str, Any]:
        return self.model_dump()


class ImportExtensionRequest(StrictRequest):
    source_path: str = Field(min_length=1, max_length=2000)
    enabled: bool = False
    replace: bool = False


class UpdateExtensionRequest(StrictRequest):
    enabled: bool


class ExtensionHostRequest(StrictRequest):
    session_id: Optional[str] = None


class CallExtensionToolRequest(StrictRequest):
    arguments: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None
    confirmed: bool = False


class AddMcpServerRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=128)
    transport: str = Field(default="stdio", max_length=8)
    command: Optional[str] = Field(default=None, max_length=500)
    args: List[str] = Field(default_factory=list)
    cwd: Optional[str] = Field(default=None, max_length=1000)
    env: List[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=15.0, ge=0.1, le=300)
    sandbox: bool = True
    sandbox_read_roots: List[str] = Field(default_factory=list)
    sandbox_write_roots: List[str] = Field(default_factory=list)
    network: bool = False
    url: Optional[str] = Field(default=None, max_length=1000)
    oauth: bool = False
    oauth_client_id: str = Field(default="", max_length=500)
    oauth_scopes: List[str] = Field(default_factory=list)
    oauth_redirect_uri: str = Field(default="", max_length=1000)
    enabled: bool = True
    approved: bool = False

    def spec(self) -> Dict[str, Any]:
        return self.model_dump()


class UpdateMcpServerRequest(StrictRequest):
    enabled: Optional[bool] = None
    approved: Optional[bool] = None


class CallMcpToolRequest(StrictRequest):
    arguments: Dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class DecidePermissionRequest(StrictRequest):
    decision: str = Field(pattern=r"^(approved|denied)$")
    scope: str = Field(default="once", pattern=r"^(once|session|always)$")


class BeginMcpOAuthRequest(StrictRequest):
    redirect_uri: Optional[str] = Field(default=None, max_length=1000)


class CompleteMcpOAuthRequest(StrictRequest):
    state: str = Field(min_length=16, max_length=500)
    code: str = Field(min_length=1, max_length=4000)


class ImportMcpRegistryRequest(StrictRequest):
    server_name: str = Field(min_length=1, max_length=500)
    version: str = Field(default="latest", min_length=1, max_length=128)
    local_name: Optional[str] = Field(default=None, min_length=1, max_length=128)
