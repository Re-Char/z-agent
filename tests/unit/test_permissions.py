from __future__ import annotations

import pytest

from zagent.domain.errors import PermissionRequiredError
from zagent.security import PermissionBroker


def test_once_permission_is_argument_bound_and_consumed(store, session_id):
    broker = PermissionBroker(store)
    with pytest.raises(PermissionRequiredError) as caught:
        broker.require(
            session_id, "mcp", "files", "tool:read", {"path": "README.md"}
        )
    request_id = caught.value.request_id
    store.decide_permission_request(request_id, "approved", "once")
    allowed = broker.require(
        session_id, "mcp", "files", "tool:read", {"path": "README.md"}
    )
    assert allowed["scope"] == "once"
    with pytest.raises(PermissionRequiredError) as second:
        broker.require(
            session_id, "mcp", "files", "tool:read", {"path": "README.md"}
        )
    assert second.value.request_id != request_id


def test_session_grant_can_be_revoked_and_does_not_cross_sessions(store, session_id):
    broker = PermissionBroker(store)
    with pytest.raises(PermissionRequiredError) as caught:
        broker.require(session_id, "extension", "demo", "tool:format", {})
    store.decide_permission_request(caught.value.request_id, "approved", "session")
    assert broker.require(session_id, "extension", "demo", "tool:format", {})["scope"] == "session"

    other = store.create_session("另一个会话")["session_id"]
    with pytest.raises(PermissionRequiredError):
        broker.require(other, "extension", "demo", "tool:format", {})
    grant = store.list_permission_grants()[0]
    assert store.revoke_permission_grant(grant["grant_id"])
    with pytest.raises(PermissionRequiredError):
        broker.require(session_id, "extension", "demo", "tool:format", {})


def test_inline_confirmation_is_audited_and_consumed_by_require(store):
    broker = PermissionBroker(store)
    result = broker.approve_inline_once(
        None, "mcp", "remote", "tool:ping", {"value": 1}, {"source": "api"}
    )
    assert result["request_id"]
    permission = broker.require(None, "mcp", "remote", "tool:ping", {"value": 1})
    assert permission["scope"] == "once"
    request = store.get_permission_request(result["request_id"])
    assert request["status"] == "consumed"
