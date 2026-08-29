from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from zagent import __version__
from zagent.bootstrap import ApplicationContainer
from zagent.domain.errors import AgentLimitError, NotFoundError, ValidationError, ZAgentError

from .schemas import (
    AddMcpServerRequest,
    CallMcpToolRequest,
    CreateExtensionRequest,
    CreateSessionRequest,
    CreateWorkspaceRequest,
    ExecuteContextToolRequest,
    ImportExtensionRequest,
    SendMessageRequest,
    UpdateExtensionRequest,
    UpdateMcpServerRequest,
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
        code = status.HTTP_404_NOT_FOUND if isinstance(exc, NotFoundError) else status.HTTP_400_BAD_REQUEST
        content = {"error": str(exc)}
        if isinstance(exc, AgentLimitError) and exc.checkpoint:
            content["checkpoint"] = exc.checkpoint
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
    def send_message(
        session_id: str, body: SendMessageRequest, core: CoreDependency
    ) -> dict:
        return core.agent.send(session_id, body.content).to_dict()

    @app.post("/v1/sessions/{session_id}/messages/stream", dependencies=protected)
    def stream_message(session_id: str, body: SendMessageRequest, core: CoreDependency) -> StreamingResponse:
        def generate() -> AsyncIterator[str]:
            try:
                for event in core.agent.send_stream(session_id, body.content):
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
    def execute_context_tool(
        session_id: str, body: ExecuteContextToolRequest, core: CoreDependency
    ) -> dict:
        return core.context.execute(session_id, body.name, body.arguments)

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
    def update_extension(
        extension_id: str, body: UpdateExtensionRequest, core: CoreDependency
    ) -> dict:
        return {"extension": core.extensions.set_enabled(extension_id, body.enabled).to_dict()}

    @app.delete("/v1/extensions/{extension_id}", dependencies=protected)
    def remove_extension(extension_id: str, core: CoreDependency) -> dict:
        if not core.extensions.remove_extension(extension_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="extension not found")
        return {"ok": True}

    @app.get("/v1/mcp/servers", dependencies=protected)
    def list_mcp_servers(core: CoreDependency) -> dict:
        return {"servers": core.mcp.list_servers()}

    @app.post("/v1/mcp/servers", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def add_mcp_server(body: AddMcpServerRequest, core: CoreDependency) -> dict:
        return {"server": core.mcp.add_server(body.spec())}

    @app.patch("/v1/mcp/servers/{name}", dependencies=protected)
    def update_mcp_server(name: str, body: UpdateMcpServerRequest, core: CoreDependency) -> dict:
        return {
            "server": core.mcp.set_state(name, enabled=body.enabled, approved=body.approved)
        }

    @app.post("/v1/mcp/servers/{name}/connect", dependencies=protected)
    def connect_mcp_server(name: str, core: CoreDependency) -> dict:
        return core.mcp.connect(name)

    @app.post("/v1/mcp/servers/{name}/disconnect", dependencies=protected)
    def disconnect_mcp_server(name: str, core: CoreDependency) -> dict:
        return {"disconnected": core.mcp.disconnect(name)}

    @app.get("/v1/mcp/servers/{name}/tools", dependencies=protected)
    def list_mcp_tools(name: str, core: CoreDependency) -> dict:
        return {"tools": core.mcp.list_tools(name)}

    @app.post("/v1/mcp/servers/{name}/tools/{tool_name}/call", dependencies=protected)
    def call_mcp_tool(
        name: str, tool_name: str, body: CallMcpToolRequest, core: CoreDependency
    ) -> dict:
        return {"result": core.mcp.call_tool(name, tool_name, body.arguments)}

    @app.delete("/v1/mcp/servers/{name}", dependencies=protected)
    def remove_mcp_server(name: str, core: CoreDependency) -> dict:
        if not core.mcp.remove_server(name):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found")
        return {"ok": True}

    return app
