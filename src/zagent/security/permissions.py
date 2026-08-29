from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from zagent.domain.errors import PermissionRequiredError
from zagent.storage import SqliteStore


class PermissionBroker:
    """Persistent deny-by-default broker shared by MCP and extension tools."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def require(
        self,
        session_id: Optional[str],
        subject_type: str,
        subject_id: str,
        action: str,
        arguments: Dict[str, Any],
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        session_id = session_id or None
        arguments_sha256 = self.arguments_sha256(arguments)
        permission = self._store.consume_permission(
            session_id, subject_type, subject_id, action, arguments_sha256
        )
        if permission is not None:
            return permission
        request = self._store.create_permission_request(
            session_id,
            subject_type,
            subject_id,
            action,
            arguments_sha256,
            details or {},
        )
        raise PermissionRequiredError(
            f"操作需要用户授权：{subject_type} {subject_id} / {action}；"
            f"permission_request_id={request['request_id']}",
            request["request_id"],
        )

    def approve_inline_once(
        self,
        session_id: Optional[str],
        subject_type: str,
        subject_id: str,
        action: str,
        arguments: Dict[str, Any],
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        session_id = session_id or None
        digest = self.arguments_sha256(arguments)
        request = self._store.create_permission_request(
            session_id, subject_type, subject_id, action, digest, details or {}
        )
        if request["status"] == "pending":
            self._store.decide_permission_request(request["request_id"], "approved", "once")
        return {"request_id": request["request_id"]}

    @staticmethod
    def arguments_sha256(arguments: Dict[str, Any]) -> str:
        canonical = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
