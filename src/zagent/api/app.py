from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from zagent import __version__
from zagent.bootstrap import ApplicationContainer
from zagent.domain.errors import (
    AgentLimitError,
    ConcurrentUpdateError,
    NotFoundError,
    PermissionRequiredError,
    ValidationError,
    ZAgentError,
)

from .schemas import (
    AddMcpServerRequest,
    BeginMcpOAuthRequest,
    CallExtensionToolRequest,
    CallMcpToolRequest,
    CompleteMcpOAuthRequest,
    ConfirmMemoryRequest,
    CorrectMemoryRequest,
    CreateExtensionRequest,
    CreateMemoryRequest,
    CreateSessionRequest,
    CreateWorkspaceRequest,
    DecidePermissionRequest,
    ExecuteContextToolRequest,
    ExtensionHostRequest,
    ForgetMemoryRequest,
    ImportExtensionRequest,
    ImportMcpRegistryRequest,
    ImportMcpServerRequest,
    SendMessageRequest,
    UpdateExtensionRequest,
    UpdateMcpServerRequest,
    UpdateMemoryRequest,
    UpdateModelRequest,
    UpdateWorkspaceRequest,
)


def _container_from_request(request: Request) -> ApplicationContainer:
    return request.app.state.container


CoreDependency = Annotated[ApplicationContainer, Depends(_container_from_request)]


def _workspace_path(path: str) -> str:
    """Normalize configured roots early so an unusable boundary is never saved."""
    if not path.strip():
        return ""
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise ValidationError(f"工作区路径不存在：{path}")
    if not candidate.is_dir():
        raise ValidationError(f"工作区路径不是目录：{path}")
    return str(candidate.resolve())


def create_api(container: ApplicationContainer, auth_token: Optional[str] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(title="Z-Agent Core API", version=__version__, lifespan=lifespan)
    app.state.container = container

    def authorize(authorization: Optional[str] = Header(default=None)) -> None:
        if auth_token and authorization != f"Bearer {auth_token}":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.exception_handler(ZAgentError)
    async def handle_domain_error(_: Request, exc: ZAgentError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, ConcurrentUpdateError):
            code = status.HTTP_409_CONFLICT
        else:
            code = status.HTTP_400_BAD_REQUEST
        content = {"error": str(exc)}
        if isinstance(exc, AgentLimitError) and exc.checkpoint:
            content["checkpoint"] = exc.checkpoint
        if isinstance(exc, PermissionRequiredError):
            content["permission_request_id"] = exc.request_id
        return JSONResponse(status_code=code, content=content)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    protected = [Depends(authorize)]

    @app.get("/v1/config", dependencies=protected)
    def get_config(core: CoreDependency) -> dict:
        return core.settings.model_dump()

    @app.post("/v1/config/model", dependencies=protected)
    def update_model(body: UpdateModelRequest, core: CoreDependency) -> dict:
        return core.update_model(body.model_patch(), body.api_key)

    @app.post("/v1/models", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def add_model(body: UpdateModelRequest, core: CoreDependency) -> dict:
        return core.add_model(body.model_patch(), body.api_key)

    @app.patch("/v1/models/{model_id}", dependencies=protected)
    def update_model_by_id(model_id: str, body: UpdateModelRequest, core: CoreDependency) -> dict:
        return core.update_model_by_id(model_id, body.model_patch(), body.api_key)

    @app.delete("/v1/models/{model_id}", dependencies=protected)
    def delete_model(model_id: str, core: CoreDependency) -> dict:
        return core.delete_model(model_id)

    @app.post("/v1/models/{model_id}/activate", dependencies=protected)
    def activate_model(model_id: str, core: CoreDependency) -> dict:
        return core.activate_model(model_id)

    @app.get("/v1/workspaces", dependencies=protected)
    def list_workspaces(core: CoreDependency) -> dict:
        return {"workspaces": core.store.list_workspaces()}

    @app.post("/v1/workspaces", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def create_workspace(body: CreateWorkspaceRequest, core: CoreDependency) -> dict:
        return {"workspace": core.store.create_workspace(body.name, _workspace_path(body.path))}

    @app.patch("/v1/workspaces/{workspace_id}", dependencies=protected)
    def update_workspace(workspace_id: str, body: UpdateWorkspaceRequest, core: CoreDependency) -> dict:
        normalized_path = _workspace_path(body.path) if body.path is not None else None
        return {"workspace": core.store.update_workspace(workspace_id, body.name, normalized_path)}

    @app.get("/v1/sessions", dependencies=protected)
    def list_sessions(
        core: CoreDependency,
        workspace_id: str = Query(default=None),
    ) -> dict:
        return {"sessions": core.store.list_sessions(workspace_id)}

    @app.post("/v1/sessions", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def create_session(body: CreateSessionRequest, core: CoreDependency) -> dict:
        return core.store.create_session(body.title, body.workspace_id)

    @app.get("/v1/sessions/{session_id}/events", dependencies=protected)
    def list_events(
        session_id: str,
        core: CoreDependency,
        limit: int = Query(default=500, ge=1, le=1000),
        after: int = Query(default=0, ge=0),
    ) -> dict:
        events = core.store.list_events(session_id, limit=limit, after=after)
        return {"events": [event.to_dict() for event in events if event.sensitivity != "internal"]}

    @app.post("/v1/sessions/{session_id}/messages", dependencies=protected)
    def send_message(session_id: str, body: SendMessageRequest, core: CoreDependency) -> dict:
        if body.expected_context_version is None:
            return core.agent.send(session_id, body.content).to_dict()
        return core.agent.send(session_id, body.content, body.expected_context_version).to_dict()

    @app.post("/v1/sessions/{session_id}/messages/stream", dependencies=protected)
    def stream_message(session_id: str, body: SendMessageRequest, core: CoreDependency) -> StreamingResponse:
        def generate() -> AsyncIterator[str]:
            try:
                stream = (
                    core.agent.send_stream(session_id, body.content)
                    if body.expected_context_version is None
                    else core.agent.send_stream(session_id, body.content, body.expected_context_version)
                )
                for event in stream:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as exc:  # noqa: BLE001 - surface any failure as a streamed error
                payload = {"type": "error", "message": str(exc)}
                if isinstance(exc, AgentLimitError) and exc.checkpoint:
                    payload["checkpoint"] = exc.checkpoint
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/sessions/{session_id}/context", dependencies=protected)
    def context_status(session_id: str, core: CoreDependency) -> dict:
        return core.context.execute(session_id, "context_status", {})

    @app.post("/v1/sessions/{session_id}/context/tools", dependencies=protected)
    def execute_context_tool(session_id: str, body: ExecuteContextToolRequest, core: CoreDependency) -> dict:
        return core.context.execute(session_id, body.name, body.arguments)

    @app.get("/v1/sessions/{session_id}/memories", dependencies=protected)
    def list_memories(
        session_id: str,
        core: CoreDependency,
        query: str = Query(default="", max_length=2000),
        include_candidates: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict:
        if query.strip():
            return {"results": core.memory.search(session_id, query, min(limit, 20))}
        return {"memories": core.memory.list(session_id, include_candidates=include_candidates, limit=limit)}

    @app.get("/v1/sessions/{session_id}/memories/export", dependencies=protected)
    def export_memories(session_id: str, core: CoreDependency) -> dict:
        return core.memory.export(session_id)

    @app.post(
        "/v1/sessions/{session_id}/memories",
        status_code=status.HTTP_201_CREATED,
        dependencies=protected,
    )
    def create_memory(session_id: str, body: CreateMemoryRequest, core: CoreDependency) -> dict:
        return core.memory.remember(session_id, **body.model_dump(), user_action=True)

    @app.post("/v1/sessions/{session_id}/memories/{memory_id}/confirm", dependencies=protected)
    def confirm_memory(
        session_id: str,
        memory_id: str,
        body: ConfirmMemoryRequest,
        core: CoreDependency,
    ) -> dict:
        return core.memory.confirm(session_id, memory_id, body.supersedes_memory_id, user_action=True)

    @app.get("/v1/sessions/{session_id}/memories/{memory_id}/audit", dependencies=protected)
    def memory_audit(
        session_id: str,
        memory_id: str,
        core: CoreDependency,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        return {"audit": core.memory.audit(session_id, memory_id, limit)}

    @app.patch("/v1/sessions/{session_id}/memories/{memory_id}", dependencies=protected)
    def update_memory(
        session_id: str,
        memory_id: str,
        body: UpdateMemoryRequest,
        core: CoreDependency,
    ) -> dict:
        return {
            "memory": core.memory.set_pinned(
                session_id,
                memory_id,
                body.pinned,
                expected_pinned=body.expected_pinned,
            )
        }

    @app.post("/v1/sessions/{session_id}/memories/{memory_id}/correct", dependencies=protected)
    def correct_memory(
        session_id: str,
        memory_id: str,
        body: CorrectMemoryRequest,
        core: CoreDependency,
    ) -> dict:
        return core.memory.correct(session_id, memory_id, body.content, body.reason)

    @app.delete("/v1/sessions/{session_id}/memories/{memory_id}", dependencies=protected)
    def forget_memory(
        session_id: str,
        memory_id: str,
        body: ForgetMemoryRequest,
        core: CoreDependency,
    ) -> dict:
        return core.memory.forget(session_id, memory_id, body.reason, user_action=True)

    @app.get("/v1/extensions", dependencies=protected)
    def list_extensions(core: CoreDependency) -> dict:
        return {"extensions": [extension.to_dict() for extension in core.extensions.discover()]}

    @app.post("/v1/extensions", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def create_extension(body: CreateExtensionRequest, core: CoreDependency) -> dict:
        return {"extension": core.extensions.create_extension(body.spec()).to_dict()}

    @app.post("/v1/extensions/import", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def import_extension(body: ImportExtensionRequest, core: CoreDependency) -> dict:
        extension = core.extensions.import_extension(
            body.source_path, enabled=body.enabled, replace=body.replace
        )
        return {"extension": extension.to_dict()}

    @app.patch("/v1/extensions/{extension_id}", dependencies=protected)
    def update_extension(extension_id: str, body: UpdateExtensionRequest, core: CoreDependency) -> dict:
        return {"extension": core.extensions.set_enabled(extension_id, body.enabled).to_dict()}

    @app.delete("/v1/extensions/{extension_id}", dependencies=protected)
    def remove_extension(extension_id: str, core: CoreDependency) -> dict:
        if not core.extensions.remove_extension(extension_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="extension not found")
        return {"ok": True}

    @app.get("/v1/extensions/{extension_id}/host", dependencies=protected)
    def extension_host_status(extension_id: str, core: CoreDependency) -> dict:
        return core.extension_hosts.status(extension_id)

    @app.post("/v1/extensions/{extension_id}/host/connect", dependencies=protected)
    def connect_extension_host(extension_id: str, body: ExtensionHostRequest, core: CoreDependency) -> dict:
        return core.extension_hosts.connect(extension_id, body.session_id)

    @app.post("/v1/extensions/{extension_id}/host/disconnect", dependencies=protected)
    def disconnect_extension_host(extension_id: str, core: CoreDependency) -> dict:
        return {"disconnected": core.extension_hosts.disconnect(extension_id)}

    @app.get("/v1/extensions/{extension_id}/host/tools", dependencies=protected)
    def list_extension_tools(
        extension_id: str,
        core: CoreDependency,
        session_id: Optional[str] = Query(default=None),
    ) -> dict:
        return {"tools": core.extension_hosts.list_tools(extension_id, session_id)}

    @app.post(
        "/v1/extensions/{extension_id}/host/tools/{tool_name}/call",
        dependencies=protected,
    )
    def call_extension_tool(
        extension_id: str,
        tool_name: str,
        body: CallExtensionToolRequest,
        core: CoreDependency,
    ) -> dict:
        if body.confirmed:
            core.permissions.approve_inline_once(
                body.session_id,
                "extension",
                extension_id,
                f"tool:{tool_name}",
                body.arguments,
                {"extension": extension_id, "tool": tool_name, "source": "api-confirmation"},
            )
        return {
            "result": core.extension_hosts.call_tool(extension_id, tool_name, body.arguments, body.session_id)
        }

    @app.get("/v1/mcp/servers", dependencies=protected)
    def list_mcp_servers(core: CoreDependency) -> dict:
        return {"servers": core.mcp.list_servers()}

    @app.get("/v1/mcp/registry/servers", dependencies=protected)
    def search_mcp_registry(
        core: CoreDependency,
        search: str = Query(default="", max_length=500),
        limit: int = Query(default=20, ge=1, le=100),
        cursor: Optional[str] = Query(default=None),
    ) -> dict:
        return core.mcp_registry.search(search, limit=limit, cursor=cursor)

    @app.post("/v1/mcp/registry/import", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def import_mcp_registry(body: ImportMcpRegistryRequest, core: CoreDependency) -> dict:
        spec = core.mcp_registry.remote_config(body.server_name, body.version, body.local_name)
        return {"server": core.mcp.add_server(spec)}

    @app.post("/v1/mcp/servers", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def add_mcp_server(body: AddMcpServerRequest, core: CoreDependency) -> dict:
        return {"server": core.mcp.add_server(body.spec())}

    @app.post("/v1/mcp/import", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def import_mcp_server(body: ImportMcpServerRequest, core: CoreDependency) -> dict:
        return {"server": core.mcp.import_server(body.source_path, replace=body.replace)}

    @app.patch("/v1/mcp/servers/{name}", dependencies=protected)
    def update_mcp_server(name: str, body: UpdateMcpServerRequest, core: CoreDependency) -> dict:
        return {"server": core.mcp.set_state(name, enabled=body.enabled, approved=body.approved)}

    @app.post("/v1/mcp/servers/{name}/connect", dependencies=protected)
    def connect_mcp_server(name: str, core: CoreDependency) -> dict:
        return core.mcp.connect(name)

    @app.post("/v1/mcp/servers/{name}/oauth/begin", dependencies=protected)
    def begin_mcp_oauth(name: str, body: BeginMcpOAuthRequest, core: CoreDependency) -> dict:
        server = core.mcp.registry.get_server(name)
        if server["transport"] != "http" or not server.get("oauth"):
            raise ValidationError("MCP server is not configured for Streamable HTTP OAuth")
        redirect_uri = body.redirect_uri or server.get("oauth_redirect_uri")
        if not redirect_uri:
            raise ValidationError("OAuth redirect_uri is required")
        return core.oauth.begin(
            name,
            server["url"],
            server.get("oauth_client_id", ""),
            redirect_uri,
            server.get("oauth_scopes", []),
        )

    @app.post("/v1/mcp/oauth/callback", dependencies=protected)
    def complete_mcp_oauth(body: CompleteMcpOAuthRequest, core: CoreDependency) -> dict:
        return core.oauth.complete(body.state, body.code)

    @app.get("/v1/mcp/oauth/callback/browser", response_class=HTMLResponse)
    def complete_mcp_oauth_browser(state: str, code: str, core: CoreDependency) -> str:
        result = core.oauth.complete(state, code)
        server = str(result["server_name"]).replace("<", "&lt;").replace(">", "&gt;")
        return (
            "<!doctype html><meta charset='utf-8'><title>Z-Agent OAuth</title>"
            "<body style='font:16px system-ui;padding:40px;background:#111;color:#eee'>"
            f"<h1>授权完成</h1><p>MCP Server <strong>{server}</strong> 已连接到 Z-Agent。</p>"
            "<p>现在可以关闭此页面并返回应用。</p></body>"
        )

    @app.post("/v1/mcp/servers/{name}/disconnect", dependencies=protected)
    def disconnect_mcp_server(name: str, core: CoreDependency) -> dict:
        return {"disconnected": core.mcp.disconnect(name)}

    @app.get("/v1/mcp/servers/{name}/tools", dependencies=protected)
    def list_mcp_tools(name: str, core: CoreDependency) -> dict:
        return {"tools": core.mcp.list_tools(name)}

    @app.post("/v1/mcp/servers/{name}/tools/{tool_name}/call", dependencies=protected)
    def call_mcp_tool(name: str, tool_name: str, body: CallMcpToolRequest, core: CoreDependency) -> dict:
        if body.confirmed:
            core.permissions.approve_inline_once(
                None,
                "mcp",
                name,
                f"tool:{tool_name}",
                body.arguments,
                {"server": name, "tool": tool_name, "source": "api-confirmation"},
            )
        core.permissions.require(
            None,
            "mcp",
            name,
            f"tool:{tool_name}",
            body.arguments,
            {"server": name, "tool": tool_name, "source": "api"},
        )
        return {"result": core.mcp.call_tool(name, tool_name, body.arguments)}

    @app.delete("/v1/mcp/servers/{name}", dependencies=protected)
    def remove_mcp_server(name: str, core: CoreDependency) -> dict:
        if not core.mcp.remove_server(name):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found")
        return {"ok": True}

    @app.get("/v1/permissions/requests", dependencies=protected)
    def list_permission_requests(
        core: CoreDependency, request_status: str = Query(default="pending", alias="status")
    ) -> dict:
        selected = None if request_status == "all" else request_status
        return {"requests": core.store.list_permission_requests(selected)}

    @app.post("/v1/permissions/requests/{request_id}/decision", dependencies=protected)
    def decide_permission(request_id: str, body: DecidePermissionRequest, core: CoreDependency) -> dict:
        return {"request": core.store.decide_permission_request(request_id, body.decision, body.scope)}

    @app.get("/v1/permissions/grants", dependencies=protected)
    def list_permission_grants(core: CoreDependency) -> dict:
        return {"grants": core.store.list_permission_grants()}

    @app.get("/v1/permissions/audit", dependencies=protected)
    def list_permission_audit(core: CoreDependency, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
        return {"audit": core.store.list_permission_audit(limit)}

    @app.delete("/v1/permissions/grants/{grant_id}", dependencies=protected)
    def revoke_permission_grant(grant_id: str, core: CoreDependency) -> dict:
        if not core.store.revoke_permission_grant(grant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="grant not found")
        return {"ok": True}

    return app
