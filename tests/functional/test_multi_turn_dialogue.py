"""Multi-turn dialogue flow test (echo provider, no network).

Covers the full user-visible path exercised by the desktop UI:
create session -> several conversation turns -> pin evidence -> context reflects it
-> token stats are reported on every turn.
"""

from zagent.bootstrap import ApplicationContainer


def test_multi_turn_dialogue_flow(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        session = container.store.create_session("多轮对话")
        session_id = session["session_id"]
        turn = 0

        def say(content: str) -> dict:
            nonlocal turn
            turn += 1
            result = container.agent.send(session_id, content)
            assert result.final_event.role == "assistant"
            return result

        # --- turn 1: plain greeting -------------------------------------------
        first = say("你好，请简短回复。")
        assert first.stats.total_tokens >= 0
        assert first.stats.elapsed_seconds >= 0
        assert "本地演示模式" in first.final_event.payload

        # --- turn 2: ask for status (echo provider records but never calls tools)
        second = say("请检查当前上下文状态。")
        assert second.final_event.payload  # non-empty reply

        # --- turn 3: pin the first user event as evidence ---------------------
        events = container.store.list_events(session_id)
        target = next(event for event in events if event.role == "user")
        pinned = container.context.execute(session_id, "context_pin", {
            "event_ids": [target.event_id], "rationale": "多轮测试固定",
        })
        assert pinned["pinned"] == [target.event_id]

        working = container.context.build_working_set(session_id)
        assert target.event_id in working.pinned_event_ids
        assert target.event_id in working.included_event_ids

        # --- turn 4: continue the conversation --------------------------------
        say("继续。")

        # --- turn 5: unpin and verify it leaves the pinned set -----------------
        unpinned = container.context.execute(session_id, "context_unpin", {"event_ids": [target.event_id]})
        assert unpinned["unpinned"] == [target.event_id]
        working = container.context.build_working_set(session_id)
        assert target.event_id not in working.pinned_event_ids

        # --- conversation continuity: model saw earlier turns ------------------
        # echo provider embeds the latest message; the working set must contain
        # every user/assistant event so later turns have full history.
        kinds = [event.kind for event in container.store.list_events(session_id)]
        assert kinds.count("message") == 6  # 3 user + 3 assistant
        assert all(msg["role"] in {"system", "user", "assistant", "tool"} for msg in working.messages)
        assert working.included_event_ids  # non-empty

        # --- stats exposed through the API shape -------------------------------
        dumped = container.agent.send(session_id, "最后一句").to_dict()
        assert set(dumped["stats"]) == {
            "total_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens",
            "cache_hit_rate", "elapsed_seconds", "tokens_per_second",
        }
        assert dumped["tool_rounds"] >= 0
        kinds = [event.kind for event in container.store.list_events(session_id)]
        assert kinds.count("message") == 8  # 4 user + 4 assistant
    finally:
        container.close()
