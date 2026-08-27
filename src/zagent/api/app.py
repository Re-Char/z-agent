from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from zagent.bootstrap import ApplicationContainer
from zagent.domain.errors import NotFoundError, ZAgentError

from .schemas import (
    AddMcpServerRequest,
    CreateExtensionRequest,
    CreateSessionRequest,
    CreateWorkspaceRequest,
    ExecuteContextToolRequest,
    SendMessageRequest,
    UpdateModelRequest,
    UpdateWorkspaceRequest,
)


def _container_from_request(request: Request) -> ApplicationContainer:
    return request.app.state.container


CoreDependency = Annotated[ApplicationContainer, Depends(_container_from_request)]


def create_api(container: ApplicationContainer, auth_token: Optional[str] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(title="Z-Agent Core API", version="0.1.0", lifespan=lifespan)
    app.state.container = container

    def authorize(authorization: Optional[str] = Header(default=None)) -> None:
        if auth_token and authorization != f"Bearer {auth_token}":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.exception_handler(ZAgentError)
    async def handle_domain_error(_: Request, exc: ZAgentError) -> JSONResponse:
        code = status.HTTP_404_NOT_FOUND if isinstance(exc, NotFoundError) else status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=code, content={"error": str(exc)})

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.1.0"}

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
        return {"workspace": core.store.create_workspace(body.name, body.path)}

    @app.patch("/v1/workspaces/{workspace_id}", dependencies=protected)
    def update_workspace(workspace_id: str, body: UpdateWorkspaceRequest, core: CoreDependency) -> dict:
        return {"workspace": core.store.update_workspace(workspace_id, body.name, body.path)}

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
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

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

    @app.delete("/v1/mcp/servers/{name}", dependencies=protected)
    def remove_mcp_server(name: str, core: CoreDependency) -> dict:
        if not core.mcp.remove_server(name):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found")
        return {"ok": True}

    return app
