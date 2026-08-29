from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from zagent.agent.extension_tools import ExtensionToolExecutor
from zagent.agent.fs_tools import FileSystemToolExecutor
from zagent.agent.mcp_tools import MCPToolExecutor
from zagent.agent.runtime import AgentRuntime, AgentRuntimeLimits
from zagent.agent.tools import CombinedToolExecutor, ContextToolExecutor
from zagent.config import AppSettings
from zagent.context.orchestrator import ContextOrchestrator
from zagent.context.working_set import WorkingSetBuilder
from zagent.domain.errors import NotFoundError
from zagent.extensions import (
    ExtensionHostManager,
    ExtensionRegistry,
    MCPConfigRegistry,
    MCPManager,
    MCPRegistryClient,
)
from zagent.extensions.oauth import MCPOAuthManager
from zagent.providers import EchoProvider, OpenAICompatibleProvider
from zagent.providers.base import ModelProvider
from zagent.security import PermissionBroker, SecretStore
from zagent.storage import SqliteStore


class ApplicationContainer:
    """Composition root. Business modules do not import the HTTP or desktop layers."""

    def __init__(self, data_dir: Optional[str] = None, project_dir: Optional[str] = None) -> None:
        self.project_dir = project_dir or str(Path.cwd())
        self.settings = AppSettings.load(data_dir)
        self.store = SqliteStore(self.settings.data_dir, self.settings.blob_threshold)
        self.secrets = SecretStore(self.settings.data_dir)
        self.permissions = PermissionBroker(self.store)
        self.extensions = ExtensionRegistry(self.settings.data_dir, self.project_dir)
        self.extension_hosts = ExtensionHostManager(
            self.extensions, self.permissions, self.settings.data_dir
        )
        self.oauth = MCPOAuthManager(self.settings.data_dir, self.secrets)
        self.mcp = MCPManager(MCPConfigRegistry(self.settings.data_dir), self.oauth)
        self.mcp_registry = MCPRegistryClient()
        self._provider: Optional[ModelProvider] = None
        self._build_runtime()

    def _build_runtime(self) -> None:
        self._close_provider()
        model = self.settings.model
        working_sets = WorkingSetBuilder(
            self.store,
            context_window=model.context_window,
            hard_limit_ratio=model.hard_limit_ratio,
            recent_event_limit=self.settings.recent_event_limit,
        )
        self.context = ContextOrchestrator(self.store, working_sets)
        self._provider = self._create_provider()
        tools = CombinedToolExecutor(
            ContextToolExecutor(self.context),
            FileSystemToolExecutor(self._workspace_path_for),
            MCPToolExecutor(self.mcp, self.permissions),
            ExtensionToolExecutor(self.extensions, self.extension_hosts),
        )
        self.agent = AgentRuntime(
            self.store,
            self.context,
            self._provider,
            tools,
            AgentRuntimeLimits(
                max_tool_rounds=self.settings.max_tool_rounds,
                task_timeout_seconds=self.settings.task_timeout_seconds,
            ),
        )

    def _workspace_path_for(self, session_id: str) -> str:
        """Security boundary: file tools only reach the session's workspace root."""
        session = self.store.get_session(session_id)
        workspace_id = session.get("workspace_id")
        if not workspace_id:
            return ""
        try:
            return self.store.get_workspace(workspace_id).get("path", "")
        except NotFoundError:
            return ""

    def _create_provider(self) -> ModelProvider:
        model = self.settings.active_model
        if model.provider == "echo":
            return EchoProvider()
        return OpenAICompatibleProvider(
            base_url=model.base_url,
            model=model.model,
            api_key=self.secrets.get(model.api_key_env),
            temperature=model.temperature,
        )

    def update_model(self, patch: Dict[str, Any], api_key: Optional[str] = None) -> Dict[str, Any]:
        """Backward-compatible update of the active profile."""
        self.settings.update_model(patch)
        if api_key:
            self.secrets.set(self.settings.active_model.api_key_env, api_key)
        self.settings.save()
        self._build_runtime()
        return self.settings.model_dump()

    def add_model(self, patch: Dict[str, Any], api_key: Optional[str] = None) -> Dict[str, Any]:
        profile = self.settings.upsert_model(patch)
        if api_key:
            self.secrets.set(profile.api_key_env, api_key)
        self.settings.save()
        self._build_runtime()
        return self.settings.model_dump()

    def update_model_by_id(
        self, model_id: str, patch: Dict[str, Any], api_key: Optional[str] = None
    ) -> Dict[str, Any]:
        target = next((m for m in self.settings.models if m.id == model_id), None)
        if target is None:
            raise NotFoundError(f"model not found: {model_id}")
        self.settings.upsert_model({**patch, "id": model_id})
        if api_key:
            env = patch.get("api_key_env") or target.api_key_env
            self.secrets.set(env, api_key)
        self.settings.save()
        self._build_runtime()
        return self.settings.model_dump()

    def delete_model(self, model_id: str) -> Dict[str, Any]:
        if not self.settings.delete_model(model_id):
            raise NotFoundError(f"model not found: {model_id}")
        self.settings.save()
        self._build_runtime()
        return self.settings.model_dump()

    def activate_model(self, model_id: str) -> Dict[str, Any]:
        if not self.settings.activate_model(model_id):
            raise NotFoundError(f"model not found: {model_id}")
        self.settings.save()
        self._build_runtime()
        return self.settings.model_dump()

    def _close_provider(self) -> None:
        if self._provider is not None and hasattr(self._provider, "close"):
            self._provider.close()  # type: ignore[union-attr]

    def close(self) -> None:
        self._close_provider()
        self.extension_hosts.close()
        self.mcp.close()
        self.oauth.close()
        self.mcp_registry.close()
        self.store.close()
