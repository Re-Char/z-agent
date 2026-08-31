import json

from zagent import server


def test_server_shutdown_on_keyboard_interrupt_is_quiet(tmp_path, monkeypatch, capsys):
    def interrupt(_server):
        raise KeyboardInterrupt

    monkeypatch.setattr(server.uvicorn.Server, "run", interrupt)

    server.main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "18766",
            "--data-dir",
            str(tmp_path / "data"),
            "--project-dir",
            str(tmp_path / "project"),
            "--auth-token",
            "test-token",
        ]
    )

    ready = json.loads(capsys.readouterr().out)
    assert ready["ready"] is True
    assert ready["token"] == "test-token"
    assert ready["host"] == "127.0.0.1"
