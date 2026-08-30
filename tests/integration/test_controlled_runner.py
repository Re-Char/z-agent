import pytest

from zagent.agent.runner_tools import ControlledRunnerExecutor
from zagent.domain.errors import PermissionRequiredError, ToolExecutionError
from zagent.security import PermissionBroker


def _approve(broker, session_id, profile, timeout):
    broker.approve_inline_once(
        session_id,
        "runner",
        profile,
        "execute",
        {"profile": profile, "timeout_seconds": timeout, "network": False},
        {"source": "test"},
    )


def test_controlled_runner_executes_approved_template_on_secret_free_snapshot(
    store, session_id, tmp_path
):
    workspace = tmp_path / "project"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (workspace / ".env").write_text("API_KEY=must-not-enter-snapshot", encoding="utf-8")
    (tests_dir / "test_safe.py").write_text(
        "from pathlib import Path\n"
        "import unittest\n\n"
        "class SafeSnapshotTest(unittest.TestCase):\n"
        "    def test_secret_was_removed(self):\n"
        "        self.assertFalse(Path('.env').exists())\n",
        encoding="utf-8",
    )
    store.update_workspace(
        store.get_session(session_id)["workspace_id"], path=str(workspace)
    )
    broker = PermissionBroker(store)
    runner = ControlledRunnerExecutor(
        lambda _session_id: str(workspace), broker, sandbox_enabled=False
    )
    _approve(broker, session_id, "python_unittest", 10)

    result = runner.execute(
        session_id,
        "runner_execute",
        {"profile": "python_unittest", "timeout_seconds": 10},
    )

    assert result["ok"] is True
    assert result["provenance"] == "controlled-runner"
    assert result["network"] is False
    assert result["snapshot_files"] == 1
    assert "OK" in result["output"]
    assert "must-not-enter-snapshot" not in result["output"]
    assert not (workspace / ".zagent-runner").exists()


def test_controlled_runner_requires_permission_and_enforces_timeout(store, session_id, tmp_path):
    workspace = tmp_path / "slow-project"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_slow.py").write_text(
        "import time\nimport unittest\n\n"
        "class SlowTest(unittest.TestCase):\n"
        "    def test_slow(self):\n"
        "        time.sleep(5)\n",
        encoding="utf-8",
    )
    broker = PermissionBroker(store)
    runner = ControlledRunnerExecutor(
        lambda _session_id: str(workspace), broker, sandbox_enabled=False
    )
    arguments = {"profile": "python_unittest", "timeout_seconds": 1}

    with pytest.raises(PermissionRequiredError):
        runner.execute(session_id, "runner_execute", arguments)

    request = store.list_permission_requests("pending")[0]
    store.decide_permission_request(request["request_id"], "approved", "once")
    result = runner.execute(session_id, "runner_execute", arguments)
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["duration_seconds"] < 4


def test_controlled_runner_caps_output_and_rejects_unavailable_sandbox(
    store, session_id, tmp_path
):
    workspace = tmp_path / "output-project"
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_output.py").write_text(
        "import unittest\n\n"
        "class OutputTest(unittest.TestCase):\n"
        "    def test_output(self):\n"
        "        print('中' * 10000)\n",
        encoding="utf-8",
    )
    broker = PermissionBroker(store)
    runner = ControlledRunnerExecutor(
        lambda _session_id: str(workspace),
        broker,
        sandbox_enabled=False,
        output_limit=1024,
    )
    _approve(broker, session_id, "python_unittest", 10)
    result = runner.execute(
        session_id,
        "runner_execute",
        {"profile": "python_unittest", "timeout_seconds": 10},
    )
    assert result["output_truncated"] is True
    assert len(result["output"].encode("utf-8")) <= 1026

    class MissingSandbox:
        def wrap(self, *_args, **_kwargs):
            raise RuntimeError("missing")

    strict = ControlledRunnerExecutor(
        lambda _session_id: str(workspace), broker, launcher=MissingSandbox()
    )
    _approve(broker, session_id, "python_unittest", 10)
    with pytest.raises(ToolExecutionError, match="沙箱不可用"):
        strict.execute(
            session_id,
            "runner_execute",
            {"profile": "python_unittest", "timeout_seconds": 10},
        )


def test_runner_snapshot_rejects_size_limits(store, session_id, tmp_path):
    workspace = tmp_path / "large-project"
    workspace.mkdir()
    (workspace / "one.py").write_text("print('one')", encoding="utf-8")
    (workspace / "two.py").write_text("print('two')", encoding="utf-8")
    broker = PermissionBroker(store)
    runner = ControlledRunnerExecutor(
        lambda _session_id: str(workspace),
        broker,
        sandbox_enabled=False,
        snapshot_file_limit=1,
    )
    _approve(broker, session_id, "python_unittest", 10)
    with pytest.raises(ToolExecutionError, match="快照超过"):
        runner.execute(
            session_id,
            "runner_execute",
            {"profile": "python_unittest", "timeout_seconds": 10},
        )
    assert not (workspace / ".zagent-runner").exists()
