from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from zagent.agent.runtime import AgentRuntime, AgentRuntimeLimits
from zagent.agent.tools import ContextToolExecutor
from zagent.config import AppSettings
from zagent.context.orchestrator import ContextOrchestrator
from zagent.context.working_set import WorkingSetBuilder
from zagent.extensions import ExtensionRegistry, MCPConfigRegistry
from zagent.providers import EchoProvider, OpenAICompatibleProvider
from zagent.providers.base import ModelProvider
from zagent.security import SecretStore
from zagent.storage import SqliteStore


class ApplicationContainer:
    """Composition root. Business modules do not import the HTTP or desktop layers."""

    def __init__(self, data_dir: Optional[str] = None, project_dir: Optional[str] = None) -> None:
        self.project_dir = project_dir or str(Path.cwd())
        self.settings = AppSettings.load(data_dir)
        self.store = SqliteStore(self.settings.data_dir, self.settings.blob_threshold)
        self.secrets = SecretStore(self.settings.data_dir)
        self.extensions = ExtensionRegistry(self.settings.data_dir, self.project_dir)
        self.mcp = MCPConfigRegistry(self.settings.data_dir)
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
        self.agent = AgentRuntime(
            self.store,
            self.context,
            self._provider,
            ContextToolExecutor(self.context),
            AgentRuntimeLimits(
                max_tool_rounds=self.settings.max_tool_rounds,
                task_timeout_seconds=self.settings.task_timeout_seconds,
            ),
        )

    def _create_provider(self) -> ModelProvider:
        model = self.settings.model
        if model.provider == "echo":
            return EchoProvider()
        return OpenAICompatibleProvider(
            base_url=model.base_url,
            model=model.model,
            api_key=self.secrets.get(model.api_key_env),
            temperature=model.temperature,
        )

    def update_model(self, patch: Dict[str, Any], api_key: Optional[str] = None) -> Dict[str, Any]:
        self.settings.update_model(patch)
        if api_key:
            self.secrets.set(self.settings.model.api_key_env, api_key)
        self.settings.save()
        self._build_runtime()
        return self.settings.model_dump()

    def _close_provider(self) -> None:
        if self._provider is not None and hasattr(self._provider, "close"):
            self._provider.close()  # type: ignore[union-attr]

    def close(self) -> None:
        self._close_provider()
        self.store.close()

