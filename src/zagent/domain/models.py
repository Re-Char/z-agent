from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    session_id: str
    sequence: int
    timestamp: str
    kind: str
    role: str
    payload: Any
    payload_sha256: str
    token_estimate: int
    parent_event_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    sensitivity: str = "normal"
    provenance: str = "runtime"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    reasoning_content: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class WorkingSet:
    messages: List[Dict[str, Any]]
    token_estimate: int
    budget: int
    included_event_ids: List[str]
    pinned_event_ids: List[str]
    dropped_pinned_ids: List[str] = field(default_factory=list)
    pinned_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TokenStats:
    """Aggregated usage across a task: cache efficiency and generation speed."""

    total_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_hit_rate: float = 0.0  # percent, 0-100
    elapsed_seconds: float = 0.0
    tokens_per_second: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentResult:
    final_event: EventRecord
    working_set: WorkingSet
    model_usage: Optional[Dict[str, Any]]
    tool_rounds: int
    stats: TokenStats = field(default_factory=TokenStats)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.final_event.to_dict(),
            "working_set": self.working_set.to_dict(),
            "usage": self.model_usage,
            "tool_rounds": self.tool_rounds,
            "stats": self.stats.to_dict(),
        }
