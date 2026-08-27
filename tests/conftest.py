from __future__ import annotations

import pytest

from zagent.context.orchestrator import ContextOrchestrator
from zagent.context.working_set import WorkingSetBuilder
from zagent.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    repository = SqliteStore(str(tmp_path / "data"), blob_threshold=128)
    yield repository
    repository.close()


@pytest.fixture
def session_id(store):
    return store.create_session("中文测试")["session_id"]


@pytest.fixture
def context(store):
    return ContextOrchestrator(
        store,
        WorkingSetBuilder(store, context_window=4096, hard_limit_ratio=0.8, recent_event_limit=12),
    )

