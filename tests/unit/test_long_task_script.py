import importlib.util
from pathlib import Path

import pytest

from zagent.domain.errors import AgentLimitError

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "long_task_e2e.py"
SPEC = importlib.util.spec_from_file_location("zagent_long_task_e2e", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
_send_with_continuations = MODULE._send_with_continuations


class _CheckpointAgent:
    def __init__(self, checkpoint_count: int):
        self.checkpoint_count = checkpoint_count
        self.prompts: list[str] = []

    def send(self, _session_id: str, prompt: str):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.checkpoint_count:
            index = len(self.prompts)
            raise AgentLimitError(
                "limit",
                {"checkpoint_id": f"ckpt_{index}", "reason": "max_tool_rounds"},
            )
        return {"done": True}


def test_long_task_runner_resumes_checkpoint_with_bounded_prompt():
    agent = _CheckpointAgent(checkpoint_count=2)

    result, checkpoints = _send_with_continuations(agent, "session", "开始", 2)

    assert result == {"done": True}
    assert [item["checkpoint_id"] for item in checkpoints] == ["ckpt_1", "ckpt_2"]
    assert agent.prompts[0] == "开始"
    assert "ckpt_1" in agent.prompts[1]
    assert "避免重复副作用" in agent.prompts[2]


def test_long_task_runner_stops_when_continuation_budget_is_exhausted():
    agent = _CheckpointAgent(checkpoint_count=2)

    with pytest.raises(AgentLimitError) as caught:
        _send_with_continuations(agent, "session", "开始", 1)

    assert caught.value.checkpoint["checkpoint_id"] == "ckpt_2"
    assert len(agent.prompts) == 2
