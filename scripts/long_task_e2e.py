from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from zagent.agent.runtime import AgentRuntimeLimits  # noqa: E402
from zagent.bootstrap import ApplicationContainer  # noqa: E402
from zagent.domain.errors import AgentLimitError  # noqa: E402

BUILD_PROMPT = """TASKBOARD_ACCEPTANCE_V1：请在当前空工作区完成一个可运行的 Python 3.12 项目 taskboard。

功能要求：
1. 只使用标准库；采用 src/taskboard 包结构。
2. Task 数据至少包含 id、title、completed、dependencies；Board 支持新增任务、按依赖完成任务、
   列出可执行任务，拒绝重复 ID、未知依赖、循环依赖和依赖未完成时提前完成。
3. JSON 文件持久化必须可恢复，写入采用临时文件 + os.replace，损坏 JSON 返回清晰错误。
4. argparse CLI 支持 add/list/next/done，统一 --db 参数；python -m taskboard 可运行。
5. 使用 unittest 编写不少于 8 个有效测试，覆盖正常流程、依赖、循环、持久化和 CLI。
6. 提供 pyproject.toml 与中文 README，README 写明运行和测试命令。

执行要求：先调用 fs_project_overview 确认工作区；用 fs_mkdir 创建 src/taskboard 与 tests；
再用 fs_write 创建完整文件。不要声称运行过测试，因为你没有 shell 工具。
完成后列出创建文件、设计决策和仍需外部验证的项目。"""


AUDIT_PROMPT = """继续 TASKBOARD_ACCEPTANCE_V1。请重新读取项目概览和所有关键源码/测试，
审计以下内容并直接修复：导入路径、循环依赖检测、依赖完成规则、JSON 原子保存、CLI 返回码、
测试是否真的能由 unittest discover 找到。修改已有文件必须先 fs_read 并携带最新 expected_sha256。

随后使用 context_search 找到最初包含 TASKBOARD_ACCEPTANCE_V1 的用户事件并 context_pin；
调用 context_status 获取序列范围，把已完成的实现阶段用 context_archive 归档，
state_update 必须写清目标、已完成、决策、风险和下一步。最终只总结实际检查与修改结果。"""


EXTEND_PROMPT = """进入 TASKBOARD_ACCEPTANCE_V1 第二阶段：在现有代码上增加 export 子命令，
支持 --format json 和 --format csv，输出稳定按任务 ID 排序；
CSV 必须包含 id/title/completed/dependencies。补充对应 unittest 和 README。
必须先读取最新文件和 sha256，再使用 fs_replace 或带 expected_sha256 的 fs_write 修改，
不能覆盖未读取的新版本。完成后说明外部应运行哪些命令验证。"""


FINALIZE_PROMPT = """完成 TASKBOARD_ACCEPTANCE_V1 最终审计：读取项目结构与关键文件，
检查需求是否全部落地，修复发现的问题。然后检查 context_status，
取消不再需要的固定证据（若仍需保留最初需求则说明理由），并归档第二阶段。
最终按“已实现、测试覆盖、已知限制、外部验收命令”四部分给出简短报告，
不得虚构测试执行结果。"""


CHECKPOINT_CHAIN_PROMPT = """CHECKPOINT_CHAIN_ACCEPTANCE_V1：这是 checkpoint 恢复故障注入验收。
必须严格顺序执行，不能批量、跳步或只口头描述：
1. 调用 fs_project_overview；
2. 创建 checkpoint-ledger.txt，内容恰好为 step=0 加换行；
3. 重新 fs_read 该文件取得最新 sha256；
4. 用 fs_replace 和该 expected_sha256 把 step=0 改为 step=1；
5. 再次 fs_read 取得新 sha256；
6. 用 fs_replace 和新 expected_sha256 把 step=1 改为 step=2；
7. 最后 fs_read，确认内容恰好为 step=2，再总结实际证据。
每一步必须等待前一步工具结果。不要归档，不要创建其他文件。"""


PHASES = {
    "build": BUILD_PROMPT,
    "audit": AUDIT_PROMPT,
    "extend": EXTEND_PROMPT,
    "finalize": FINALIZE_PROMPT,
    "checkpoint_chain": CHECKPOINT_CHAIN_PROMPT,
}


def _send_with_continuations(agent, session_id: str, prompt: str, max_continuations: int):
    """Resume evidence-backed checkpoints without hiding or bypassing per-turn limits."""
    checkpoints: list[dict] = []
    next_prompt = prompt
    while True:
        try:
            return agent.send(session_id, next_prompt), checkpoints
        except AgentLimitError as error:
            checkpoint = error.checkpoint or {}
            checkpoints.append(checkpoint)
            print(json.dumps({
                "checkpoint_saved": True,
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "reason": checkpoint.get("reason"),
                "continuation": len(checkpoints),
            }, ensure_ascii=False), flush=True)
            if len(checkpoints) > max_continuations:
                raise
            checkpoint_id = checkpoint.get("checkpoint_id", "unknown")
            next_prompt = (
                f"继续当前阶段并完成未完成的工作。请先依据系统提示中的 checkpoint "
                f"{checkpoint_id} 核对已完成证据，避免重复副作用。"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real-provider long-task acceptance flow")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--feedback-file")
    parser.add_argument(
        "--max-continuations",
        type=int,
        default=0,
        help="automatically resume at most N saved checkpoints (default: 0)",
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        choices=range(1, 9),
        help="acceptance-only per-turn tool limit for deterministic checkpoint injection",
    )
    args = parser.parse_args()

    if args.max_continuations < 0:
        parser.error("--max-continuations must be non-negative")

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace must be an existing directory: {workspace}")

    runtime_limits = (
        AgentRuntimeLimits(max_tool_rounds=args.max_tool_rounds, task_timeout_seconds=300)
        if args.max_tool_rounds is not None
        else None
    )
    container = ApplicationContainer(
        args.data_dir, str(workspace), runtime_limits=runtime_limits
    )
    try:
        model = container.settings.active_model
        if model.provider == "echo":
            raise SystemExit("real-provider acceptance requires a non-echo active model")
        if args.session_id:
            session_id = args.session_id
            session = container.store.get_session(session_id)
            bound = container.store.get_workspace(session["workspace_id"])
            if Path(bound["path"]).resolve() != workspace:
                raise SystemExit("session workspace does not match --workspace")
        else:
            workspace_record = container.store.create_workspace(
                f"长程验收-{workspace.name}", str(workspace)
            )
            session_id = container.store.create_session(
                "TASKBOARD_ACCEPTANCE_V1", workspace_record["workspace_id"]
            )["session_id"]

        print(json.dumps({
            "started": True,
            "session_id": session_id,
            "phase": args.phase,
            "provider": model.provider,
            "model": model.model,
        }, ensure_ascii=False), flush=True)

        prompt = PHASES[args.phase]
        if args.feedback_file:
            feedback_path = Path(args.feedback_file)
            feedback = feedback_path.read_text(encoding="utf-8", errors="replace")[:20_000]
            prompt += "\n\n这是外部测试器的真实输出，请据此修复后再完成本阶段：\n" + feedback
        try:
            result, checkpoints = _send_with_continuations(
                container.agent, session_id, prompt, args.max_continuations
            )
        except AgentLimitError as error:
            checkpoint = error.checkpoint or {}
            print(json.dumps({
                "completed": False,
                "session_id": session_id,
                "phase": args.phase,
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "message": str(error),
                "resume_hint": (
                    "rerun this phase with --session-id and optionally "
                    "--max-continuations"
                ),
            }, ensure_ascii=False, indent=2))
            raise SystemExit(2) from None
        events = container.store.list_events(session_id, limit=1000)
        context = container.context.execute(session_id, "context_status", {})
        print(json.dumps({
            "session_id": session_id,
            "phase": args.phase,
            "provider": model.provider,
            "model": model.model,
            "final": result.final_event.payload,
            "tool_rounds": result.tool_rounds,
            "checkpoint_continuations": len(checkpoints),
            "checkpoint_ids": [item.get("checkpoint_id") for item in checkpoints],
            "tool_names": [event.tool_name for event in events if event.kind == "tool_result"],
            "event_count": len(events),
            "latest_archive": (
                context["latest_archive"]["archive_id"] if context["latest_archive"] else None
            ),
            "pinned_event_ids": context["working_set"]["pinned_event_ids"],
            "working_set_tokens": context["working_set"]["tokens"],
        }, ensure_ascii=False, indent=2))
    finally:
        container.close()


if __name__ == "__main__":
    main()
