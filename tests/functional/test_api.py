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

