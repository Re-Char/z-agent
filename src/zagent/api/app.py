from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from zagent.bootstrap import ApplicationContainer
from zagent.domain.errors import NotFoundError, ZAgentError

from .schemas import CreateSessionRequest, ExecuteContextToolRequest, SendMessageRequest, UpdateModelRequest


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

    @app.get("/v1/sessions", dependencies=protected)
    def list_sessions(core: CoreDependency) -> dict:
        return {"sessions": core.store.list_sessions()}

    @app.post("/v1/sessions", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def create_session(body: CreateSessionRequest, core: CoreDependency) -> dict:
        return core.store.create_session(body.title)

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

    @app.get("/v1/mcp/servers", dependencies=protected)
    def list_mcp_servers(core: CoreDependency) -> dict:
        return {"servers": core.mcp.list_servers()}

    return app
