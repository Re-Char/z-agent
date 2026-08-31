from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Iterator, Optional

from zagent.context.orchestrator import ContextOrchestrator
from zagent.domain.errors import (
    AgentLimitError,
    ModelTransportError,
    PermissionRequiredError,
    ZAgentError,
)
from zagent.domain.models import AgentResult, ModelResponse, TokenStats
from zagent.providers.base import ModelProvider
from zagent.storage.sqlite_store import SqliteStore

from .tools import ToolExecutor


@dataclass(frozen=True)
class AgentRuntimeLimits:
    max_tool_rounds: int = 8
    task_timeout_seconds: float = 300


def _usage_int(usage: Optional[Dict[str, object]], *keys: str) -> int:
    """Defensive extraction of integer counters from provider-specific usage dicts."""
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return 0


@dataclass
class _RoundState:
    """Mutable counters shared across tool rounds of one task."""

    tool_rounds: int = 0
    total_tokens: int = 0
    completion_tokens: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    latest_usage: Optional[Dict[str, Any]] = None
    tool_evidence: list[Dict[str, Any]] = field(default_factory=list)
    checkpoint: Optional[Dict[str, Any]] = None
    started: float = field(default_factory=time.monotonic)

    def record(self, usage: Optional[Dict[str, Any]]) -> None:
        self.latest_usage = usage
        self.total_tokens += _usage_int(usage, "total_tokens")
        self.completion_tokens += _usage_int(usage, "completion_tokens")
        self.cache_hit += _usage_int(usage, "prompt_cache_hit_tokens", "prompt_cache_hit")
        self.cache_miss += _usage_int(usage, "prompt_cache_miss_tokens", "prompt_cache_miss")

    def stats(self) -> TokenStats:
        elapsed = time.monotonic() - self.started
        return TokenStats(
            total_tokens=self.total_tokens,
            completion_tokens=self.completion_tokens,
            cache_hit_tokens=self.cache_hit,
            cache_miss_tokens=self.cache_miss,
            cache_hit_rate=round(self.cache_hit / (self.cache_hit + self.cache_miss) * 100, 1)
            if (self.cache_hit + self.cache_miss) > 0 else 0.0,
            elapsed_seconds=round(elapsed, 2),
            tokens_per_second=round(self.completion_tokens / elapsed, 2) if elapsed > 0 else 0.0,
        )


class AgentRuntime:
    """Self-contained model/tool loop; no agent framework or hosted execution.

    send() and send_stream() share the same round machinery (_complete_round,
    _store_raw, _run_tool_round, _finalize); they differ only in how a round is
    produced (blocking vs streaming) and how output is delivered.
    """

    def __init__(
        self,
        store: SqliteStore,
        context: ContextOrchestrator,
        provider: ModelProvider,
        tools: ToolExecutor,
        limits: AgentRuntimeLimits | None = None,
    ) -> None:
        self._store = store
        self._context = context
        self._provider = provider
        self._tools = tools
        self._limits = limits or AgentRuntimeLimits()

    # --- public entry points -------------------------------------------------

    def send(
        self, session_id: str, content: str, expected_context_version: int | None = None
    ) -> AgentResult:
        user_event = self._store.append_event(
            session_id, "message", "user", content, provenance="user",
            expected_context_version=expected_context_version,
        )
        state = _RoundState()
        while True:
            response = self._complete_round(session_id, user_event.event_id, state)
            state.record(response.usage)
            self._store_raw(session_id, user_event.event_id, response)
            if not response.tool_calls:
                return self._finalize(session_id, user_event.event_id, response, state)
            for _ in self._run_tool_round(
                session_id, user_event.event_id, response, state
            ):
                pass

    def send_stream(
        self, session_id: str, content: str, expected_context_version: int | None = None
    ) -> Iterator[Dict[str, Any]]:
        """Streamed send: yields UI events, then a final {"type": "done", "result": ...}.

        The user message is stored up front so it can be rendered immediately;
        content deltas of the final reply round are forwarded for progressive output.
        """
        user_event = self._store.append_event(
            session_id, "message", "user", content, provenance="user",
            expected_context_version=expected_context_version,
        )
        state = _RoundState()
        while True:
            self._check_deadline(session_id, user_event.event_id, state)
            yield {
                "type": "status",
                "round": state.tool_rounds + 1,
                "message": f"第 {state.tool_rounds + 1} 轮：正在请求模型",
            }
            response: Optional[ModelResponse] = None
            suppress_round = False  # tool-call rounds forward no deltas
            announced_tools: set[tuple[int, str]] = set()
            tool_stream_announced = False
            for event in self._provider.complete_stream(
                self._context.build_working_set(session_id).messages, self._tools.schemas
            ):
                if event["type"] == "error":
                    raise ModelTransportError(event["message"])
                if event["type"] == "done":
                    response = event["response"]
                    continue
                if event["type"] == "tool_call":
                    suppress_round = True
                    if not tool_stream_announced:
                        tool_stream_announced = True
                        yield {
                            "type": "status",
                            "round": state.tool_rounds + 1,
                            "message": "模型正在生成工具调用参数",
                        }
                    name = str(event.get("name") or "")
                    marker = (int(event.get("index", 0)), name)
                    if name and marker not in announced_tools:
                        announced_tools.add(marker)
                        # Arguments may contain an entire source file. Keep renderer
                        # progress bounded and never forward argument content.
                        yield {
                            "type": "tool_call",
                            "name": name,
                            "call_id": str(event.get("call_id") or ""),
                        }
                    continue
                if not suppress_round and event["type"] in {"content", "reasoning"}:
                    yield {"type": event["type"], "text": event["text"]}
            if response is None:
                raise ModelTransportError("model stream ended without a response")
            state.record(response.usage)
            self._store_raw(session_id, user_event.event_id, response)
            if not response.tool_calls:
                result = self._finalize(session_id, user_event.event_id, response, state)
                yield {"type": "done", "result": result.to_dict()}
                return
            # Some compatible providers only expose tool names in the final
            # assembled response. Always emit a bounded fallback before execution.
            for index, call in enumerate(response.tool_calls):
                marker = (index, call.name)
                if marker not in announced_tools:
                    announced_tools.add(marker)
                    yield {
                        "type": "tool_call",
                        "name": call.name,
                        "call_id": call.call_id,
                    }
            evidence_start = len(state.tool_evidence)
            yield from self._run_tool_round(
                session_id,
                user_event.event_id,
                response,
                state,
                wait_for_permission=True,
            )
            for evidence in state.tool_evidence[evidence_start:]:
                progress: Dict[str, Any] = {
                    "type": "tool_result",
                    "name": evidence["tool"],
                    "ok": evidence["ok"],
                }
                # Keep progress useful but bounded: expose only a relative output
                # path or a short failure, never full tool output/source content.
                if evidence.get("path"):
                    progress["detail"] = str(evidence["path"])[:240]
                elif evidence.get("error"):
                    progress["detail"] = str(evidence["error"])[:160]
                yield progress

    # --- shared round machinery ----------------------------------------------

    def _complete_round(
        self, session_id: str, parent_event_id: str, state: _RoundState
    ) -> ModelResponse:
        self._check_deadline(session_id, parent_event_id, state)
        working_set = self._context.build_working_set(session_id)
        return self._provider.complete(working_set.messages, self._tools.schemas)

    def _check_deadline(
        self, session_id: str, parent_event_id: str, state: _RoundState
    ) -> None:
        if time.monotonic() - state.started > self._limits.task_timeout_seconds:
            checkpoint = self._create_checkpoint(
                session_id, parent_event_id, state, "task_timeout", []
            )
            raise AgentLimitError("任务超过本轮时间上限，已保存可恢复 checkpoint", checkpoint)

    def _store_raw(self, session_id: str, parent_event_id: str, response: ModelResponse) -> None:
        if response.raw is not None:
            self._store.append_event(
                session_id, "model_raw", "system", response.raw,
                parent_event_id=parent_event_id, sensitivity="internal", provenance="model-provider",
            )

    def _finalize(
        self, session_id: str, parent_event_id: str, response: ModelResponse, state: _RoundState
    ) -> AgentResult:
        if response.reasoning_content:
            # Final-round reasoning is stored separately for an explicitly
            # user-expandable audit view. It is excluded from future WorkingSets.
            self._store.append_event(
                session_id, "assistant_reasoning", "assistant", response.reasoning_content,
                parent_event_id=parent_event_id, provenance="model-reasoning",
            )
        final_event = self._store.append_event(
            session_id, "message", "assistant", response.content,
            parent_event_id=parent_event_id, provenance="model",
        )
        self._store.resolve_active_checkpoint(session_id, final_event.event_id)
        self._record_task_memory_candidate(
            session_id, parent_event_id, final_event.event_id, state
        )
        return AgentResult(
            final_event=final_event,
            working_set=self._context.build_working_set(session_id),
            model_usage=state.latest_usage,
            tool_rounds=state.tool_rounds,
            stats=state.stats(),
        )

    def _record_task_memory_candidate(
        self,
        session_id: str,
        objective_event_id: str,
        final_event_id: str,
        state: _RoundState,
    ) -> None:
        """Create a reviewable memory candidate after verified workspace changes.

        This is intentionally deterministic and candidate-only: ordinary chat,
        cancelled tasks and read-only analysis create nothing, while successful
        file mutations leave a cross-session result the user can confirm.
        """
        mutations = [
            item for item in state.tool_evidence
            if item.get("ok") and item.get("tool") in {"fs_write", "fs_replace"}
            and item.get("path")
        ]
        if not mutations:
            return
        paths = list(dict.fromkeys(str(item["path"]) for item in mutations))[:12]
        objective_event = self._store.get_event(objective_event_id)
        objective = (
            objective_event.payload.strip()
            if isinstance(objective_event.payload, str)
            else "完成工作区文件任务"
        )
        verified_paths = {
            str(item["path"])
            for item in state.tool_evidence
            if item.get("ok") and item.get("tool") == "fs_read" and item.get("path")
        }
        verification = "；已回读核验" if verified_paths.intersection(paths) else ""
        content = (
            f"已完成任务：{objective[:500]}。"
            f"工作区产物：{', '.join(paths)}{verification}。"
        )
        sources = [objective_event_id, final_event_id]
        sources.extend(
            str(item["tool_result_event_id"])
            for item in state.tool_evidence
            if item.get("tool_result_event_id") and item.get("ok")
        )
        try:
            self._context.execute(session_id, "memory_remember", {
                "memory_type": "episodic",
                "memory_key": ("任务产物：" + "、".join(paths))[:200],
                "content": content,
                "source_event_ids": list(dict.fromkeys(sources))[:20],
                "reason": "任务成功修改工作区后自动生成，等待用户确认",
                "scope": "workspace",
                "confidence": 0.75,
                "confirmed": False,
            })
        except ZAgentError:
            # Memory is optional metadata and must never turn a completed task
            # into a failure (for example when content resembles a secret).
            return

    def _run_tool_round(
        self,
        session_id: str,
        parent_event_id: str,
        response: ModelResponse,
        state: _RoundState,
        *,
        wait_for_permission: bool = False,
    ) -> Iterator[Dict[str, Any]]:
        if state.tool_rounds >= self._limits.max_tool_rounds:
            pending = [
                {"call_id": call.call_id, "tool": call.name}
                for call in response.tool_calls
            ]
            checkpoint = self._create_checkpoint(
                session_id, parent_event_id, state, "max_tool_rounds", pending
            )
            raise AgentLimitError("已达到本轮工具调用上限，已保存可恢复 checkpoint", checkpoint)
        state.tool_rounds += 1
        call_payload = {
            "content": response.content,
            "tool_calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ],
        }
        if response.reasoning_content is not None:
            # DeepSeek thinking-mode tool calls require this field verbatim on
            # the next request. The UI exposes it only in a default-collapsed disclosure.
            call_payload["reasoning_content"] = response.reasoning_content
        self._store.append_event(
            session_id, "assistant_tool_calls", "assistant", call_payload,
            parent_event_id=parent_event_id, provenance="model",
        )
        for call in response.tool_calls:
            claim = self._store.claim_tool_invocation(
                session_id, call.call_id, call.name, call.arguments
            )
            replayed = False
            if claim["action"] == "execute":
                try:
                    result = self._tools.execute(session_id, call.name, call.arguments)
                except PermissionRequiredError as exc:
                    if wait_for_permission:
                        try:
                            decision = yield from self._wait_for_permission(
                                exc.request_id, state
                            )
                        except GeneratorExit:
                            self._store.complete_tool_invocation(
                                session_id,
                                call.call_id,
                                call.name,
                                {
                                    "ok": False,
                                    "error": "用户在审批期间取消了任务",
                                    "tool": call.name,
                                },
                            )
                            raise
                        if decision["status"] == "approved":
                            try:
                                # The approved once/session/always permission is
                                # consumed by the same executor before side effects.
                                result = self._tools.execute(
                                    session_id, call.name, call.arguments
                                )
                            except Exception as retry_exc:  # noqa: BLE001
                                result = {
                                    "ok": False,
                                    "error": str(retry_exc),
                                    "tool": call.name,
                                }
                        else:
                            result = {
                                "ok": False,
                                "error": "用户拒绝了此工具执行请求",
                                "tool": call.name,
                            }
                    else:
                        result = {"ok": False, "error": str(exc), "tool": call.name}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "tool": call.name}
                result_event = self._store.complete_tool_invocation(
                    session_id, call.call_id, call.name, result
                )
            else:
                replayed = claim["action"] == "replay"
                result = self._nonexecuted_invocation_result(call.name, claim)
                result_event = self._store.append_event(
                    session_id, "tool_result", "tool", result,
                    tool_name=call.name, tool_call_id=call.call_id,
                    provenance="tool-invocation-guard",
                )
            evidence: Dict[str, Any] = {
                "call_id": call.call_id,
                "tool": call.name,
                "tool_result_event_id": result_event.event_id,
                "ok": not isinstance(result, dict) or result.get("ok") is not False,
                "replayed": replayed,
            }
            if isinstance(result, dict):
                if isinstance(result.get("path"), str):
                    evidence["path"] = result["path"]
                if isinstance(result.get("sha256"), str):
                    evidence["sha256"] = result["sha256"]
                if isinstance(result.get("snapshot_sha256"), str):
                    evidence["snapshot_sha256"] = result["snapshot_sha256"]
                if isinstance(result.get("profile"), str):
                    evidence["runner_profile"] = result["profile"]
                if result.get("error"):
                    evidence["error"] = str(result["error"])[:300]
            state.tool_evidence.append(evidence)

    def _wait_for_permission(
        self, request_id: str, state: _RoundState
    ) -> Generator[Dict[str, Any], None, Dict[str, Any]]:
        """Pause a streamed tool call until the renderer records a decision.

        Approval time is excluded from the task deadline. A periodic status frame
        keeps the SSE generator cancellable; closing the stream denies the orphaned
        request so it cannot be approved accidentally later.
        """
        wait_started = time.monotonic()
        last_heartbeat = wait_started
        request = self._store.get_permission_request(request_id)
        try:
            yield {"type": "permission_required", "request": request}
            while True:
                request = self._store.get_permission_request(request_id)
                if request["status"] != "pending":
                    return request
                time.sleep(0.1)
                now = time.monotonic()
                if now - last_heartbeat >= 1:
                    last_heartbeat = now
                    yield {
                        "type": "status",
                        "message": "等待用户批准工具执行",
                    }
        except GeneratorExit:
            request = self._store.get_permission_request(request_id)
            if request["status"] == "pending":
                self._store.decide_permission_request(request_id, "denied", "once")
            raise
        finally:
            # Human review is not model/tool execution time and must not cause a
            # checkpoint timeout merely because the dialog stayed open.
            state.started += time.monotonic() - wait_started

    @staticmethod
    def _nonexecuted_invocation_result(tool_name: str, claim: Dict[str, Any]) -> Dict[str, Any]:
        action = claim["action"]
        if action == "replay":
            original = claim["result"]
            original_ok = not isinstance(original, dict) or original.get("ok") is not False
            return {
                "ok": original_ok,
                "replayed": True,
                "tool": tool_name,
                "original_result_event_id": claim["result_event_id"],
                "result": original,
            }
        if action == "conflict":
            return {
                "ok": False,
                "tool": tool_name,
                "error": (
                    "工具 call_id 已被不同工具或参数使用，为防止重复副作用已拒绝执行；"
                    "请使用新 call_id。"
                ),
                "invocation_state": "conflict",
            }
        return {
            "ok": False,
            "tool": tool_name,
            "error": (
                "上次工具调用可能已产生副作用，但未来得及持久化结果；已阻止自动重试，"
                "请先读取当前文件状态后用新 call_id 继续。"
            ),
            "invocation_state": "uncertain",
        }

    def _create_checkpoint(
        self,
        session_id: str,
        parent_event_id: str,
        state: _RoundState,
        reason: str,
        pending: list[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if state.checkpoint is not None:
            return state.checkpoint
        objective_event = self._store.get_event(parent_event_id)
        objective = objective_event.payload if isinstance(objective_event.payload, str) else str(
            objective_event.payload
        )
        latest_archive = self._store.latest_archive(session_id)
        files = [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "evidence_event_id": item["tool_result_event_id"],
            }
            for item in state.tool_evidence
            if item.get("path") and item.get("sha256")
        ]
        checkpoint_state = {
            "schema_version": 1,
            "status": "paused",
            "objective": objective[:4000],
            "objective_event_id": parent_event_id,
            "completed": list(state.tool_evidence),
            "pending": pending,
            "file_versions": files,
            "tool_rounds_completed": state.tool_rounds,
            "last_sequence": self._store.session_stats(session_id)["latest"],
            "recoverable_archive_id": latest_archive["archive_id"] if latest_archive else None,
            "failure_reason": reason,
            "suggested_next_step": "继续任务前先根据 event ID 核对已完成工具结果和文件 SHA，再执行待办项。",
        }
        state.checkpoint = self._store.create_checkpoint(
            session_id, parent_event_id, reason, checkpoint_state
        )
        return state.checkpoint
