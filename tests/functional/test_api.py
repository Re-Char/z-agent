from fastapi.testclient import TestClient

from zagent.api import create_api
from zagent.bootstrap import ApplicationContainer


def test_api_session_message_and_context_flow(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        with TestClient(create_api(container, auth_token="test-token")) as client:
            headers = {"Authorization": "Bearer test-token"}
            created = client.post("/v1/sessions", headers=headers, json={"title": "API 测试"})
            assert created.status_code == 201
            session_id = created.json()["session_id"]
            message = client.post(
                f"/v1/sessions/{session_id}/messages", headers=headers, json={"content": "你好"}
            )
            assert message.status_code == 200
            assert "本地演示模式" in message.json()["event"]["payload"]
            context = client.get(f"/v1/sessions/{session_id}/context", headers=headers)
            assert context.status_code == 200
            assert context.json()["stats"]["count"] >= 3
            assert context.json()["working_set"]["tokens"] > 0
            assert context.json()["archive_stats"] == {"count": 0, "tokens": 0}
    finally:
        container.close()


def test_api_rejects_missing_auth(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        with TestClient(create_api(container, auth_token="secret")) as client:
            assert client.get("/v1/sessions").status_code == 401
            assert client.get("/health").status_code == 200
    finally:
        container.close()


def test_api_exposes_reasoning_for_explicit_collapsed_ui_but_hides_internal_events(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        session = container.store.create_session("安全展示")
        container.store.append_event(
            session["session_id"],
            "assistant_tool_calls",
            "assistant",
            {
                "content": "",
                "reasoning_content": "可由用户手动展开的思考",
                "tool_calls": [{"call_id": "c1", "name": "context_status", "arguments": {}}],
            },
        )
        container.store.append_event(
            session["session_id"], "model_raw", "system", {"secret": "raw"},
            sensitivity="internal",
        )
        with TestClient(create_api(container, auth_token="test-token")) as client:
            response = client.get(
                f"/v1/sessions/{session['session_id']}/events",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 200
            events = response.json()["events"]
            assert len(events) == 1
            payload = events[0]["payload"]
            assert payload["reasoning_content"] == "可由用户手动展开的思考"
            assert payload["tool_calls"][0]["name"] == "context_status"
    finally:
        container.close()


def test_api_model_profiles_crud_and_activate(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        with TestClient(create_api(container, auth_token="test-token")) as client:
            headers = {"Authorization": "Bearer test-token"}
            config = client.get("/v1/config", headers=headers).json()
            assert len(config["models"]) == 1
            assert config["active_model_id"] == config["models"][0]["id"]

            created = client.post("/v1/models", headers=headers, json={
                "name": "DeepSeek", "provider": "deepseek", "model": "deepseek-v4-flash",
                "base_url": "https://api.deepseek.com", "context_window": 1000000,
            })
            assert created.status_code == 201
            body = created.json()
            assert len(body["models"]) == 2
            new_id = body["active_model_id"]
            assert body["models"][0]["id"] != new_id
            assert body["models"][1]["id"] == new_id
            assert body["model"]["name"] == "DeepSeek"

            patched = client.patch(f"/v1/models/{new_id}", headers=headers, json={"name": "DeepSeek 主力"})
            assert patched.status_code == 200
            assert patched.json()["model"]["name"] == "DeepSeek 主力"

            # switching back to the echo profile
            first_id = body["models"][0]["id"]
            activated = client.post(f"/v1/models/{first_id}/activate", headers=headers)
            assert activated.status_code == 200
            assert activated.json()["active_model_id"] == first_id
            assert activated.json()["model"]["provider"] == "echo"

            deleted = client.delete(f"/v1/models/{new_id}", headers=headers)
            assert deleted.status_code == 200
            assert len(deleted.json()["models"]) == 1
            assert client.patch("/v1/models/missing", headers=headers, json={"name": "x"}).status_code == 404
            assert client.delete("/v1/models/missing", headers=headers).status_code == 404
            assert client.post("/v1/models/missing/activate", headers=headers).status_code == 404
    finally:
        container.close()



def test_api_extension_and_mcp_management(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        with TestClient(create_api(container, auth_token="test-token")) as client:
            headers = {"Authorization": "Bearer test-token"}
            created = client.post("/v1/extensions", headers=headers, json={
                "id": "com.example.helper", "name": "助手扩展", "runtime": "declarative",
                "contributes": ["skills"],
            })
            assert created.status_code == 201
            assert created.json()["extension"]["id"] == "com.example.helper"
            assert client.get("/v1/extensions", headers=headers).json()["extensions"][0]["name"] == "助手扩展"
            assert client.delete("/v1/extensions/com.example.helper", headers=headers).status_code == 200
            assert client.delete("/v1/extensions/missing", headers=headers).status_code == 404

            mcp = client.post("/v1/mcp/servers", headers=headers, json={
                "name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "filesystem"],
            })
            assert mcp.status_code == 201
            assert mcp.json()["server"]["command"] == "npx"
            assert client.get("/v1/mcp/servers", headers=headers).json()["servers"][0]["name"] == "fs"
            assert client.delete("/v1/mcp/servers/fs", headers=headers).status_code == 200
            assert client.delete("/v1/mcp/servers/missing", headers=headers).status_code == 404
    finally:
        container.close()


def test_api_workspace_flow(tmp_path):
    container = ApplicationContainer(str(tmp_path / "data"), str(tmp_path))
    try:
        with TestClient(create_api(container, auth_token="test-token")) as client:
            headers = {"Authorization": "Bearer test-token"}
            project_path = tmp_path / "project"
            project_path.mkdir()
            workspaces = client.get("/v1/workspaces", headers=headers).json()["workspaces"]
            assert len(workspaces) == 1
            created = client.post("/v1/workspaces", headers=headers, json={
                "name": "项目", "path": str(project_path),
            })
            assert created.status_code == 201
            ws_id = created.json()["workspace"]["workspace_id"]
            session = client.post("/v1/sessions", headers=headers, json={
                "title": "项目会话", "workspace_id": ws_id,
            })
            assert session.status_code == 201
            assert session.json()["workspace_id"] == ws_id
            listed = client.get(f"/v1/sessions?workspace_id={ws_id}", headers=headers).json()["sessions"]
            assert [item["title"] for item in listed] == ["项目会话"]
            assert client.get("/v1/workspaces", headers=headers).json()["workspaces"][1]["name"] == "项目"
            rejected = client.patch(
                f"/v1/workspaces/{ws_id}", headers=headers,
                json={"path": str(tmp_path / "missing")},
            )
            assert rejected.status_code == 400
            assert "路径不存在" in rejected.json()["error"]
    finally:
        container.close()
