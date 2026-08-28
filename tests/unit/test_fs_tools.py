
import pytest

from zagent.agent.fs_tools import FileSystemToolExecutor
from zagent.domain.errors import ToolExecutionError


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("def main():\n    print('hello world')\n", encoding="utf-8")
    (root / "README.md").write_text("# 项目\n关键说明：缓存命中率优化\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "big.js").write_text("x" * 5000, encoding="utf-8")
    (root / "src" / "secret_outside.txt").write_text("内部文件", encoding="utf-8")
    (root / "long_log.txt").write_text("行内容\n" * 300, encoding="utf-8")
    (root / ".env").write_text("DEEPSEEK_API_KEY=should-never-leak", encoding="utf-8")
    (root / "deploy.key").write_text("private-key", encoding="utf-8")
    (root / "image.bin").write_bytes(b"abc\x00def")
    return root


def make_executor(root):
    return FileSystemToolExecutor(lambda _session_id: str(root))


def test_fs_list_ignores_vcs_and_dependencies(project):
    result = make_executor(project).execute("s1", "fs_list", {"path": "."})
    names = [entry["name"] for entry in result["entries"]]
    assert "src" in names and "README.md" in names
    assert ".git" not in names and "node_modules" not in names
    assert result["count"] == len(result["entries"])


def test_fs_read_returns_content_and_truncates(project):
    executor = make_executor(project)
    full = executor.execute("s1", "fs_read", {"path": "src/main.py"})
    assert "hello world" in full["content"]
    assert full["truncated"] is False
    assert len(full["sha256"]) == 64
    assert full["modified_ns"] > 0
    small = executor.execute("s1", "fs_read", {"path": "long_log.txt", "max_chars": 256})
    assert small["truncated"] is True
    assert len(small["content"]) == 256


def test_fs_search_finds_filename_and_content_hits(project):
    executor = make_executor(project)
    result = executor.execute("s1", "fs_search", {"query": "缓存命中率"})
    assert any(hit["path"] == "README.md" and hit["kind"] == "content" for hit in result["hits"])
    result = executor.execute("s1", "fs_search", {"query": "main"})
    assert any(hit["path"] == "src/main.py" for hit in result["hits"])


def test_fs_overview_builds_top_level_tree(project):
    result = make_executor(project).execute("s1", "fs_project_overview", {})
    assert result["root"].endswith("project")
    dirs = [item for item in result["top_level"] if item["type"] == "dir"]
    assert dirs[0]["name"] == "src"
    assert set(dirs[0]["children"]) == {"main.py", "secret_outside.txt"}


def test_fs_rejects_path_escape(project):
    executor = make_executor(project)
    with pytest.raises(ToolExecutionError, match="超出工作区边界"):
        executor.execute("s1", "fs_read", {"path": "../outside.txt"})
    with pytest.raises(ToolExecutionError, match="超出工作区边界"):
        executor.execute("s1", "fs_list", {"path": "../../etc"})


def test_fs_requires_workspace_path():
    executor = FileSystemToolExecutor(lambda _session_id: "")
    with pytest.raises(ToolExecutionError, match="未设置路径"):
        executor.execute("s1", "fs_list", {"path": "."})


def test_fs_blocks_symlink_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("不该被读到", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    executor = make_executor(root)
    with pytest.raises(ToolExecutionError, match="超出工作区边界"):
        executor.execute("s1", "fs_read", {"path": "link.txt"})


def test_fs_tools_expose_version_checked_write_operations(project):
    executor = make_executor(project)
    names = [schema["function"]["name"] for schema in executor.schemas]
    assert names == [
        "fs_list", "fs_read", "fs_search", "fs_project_overview", "fs_write", "fs_replace",
    ]
    assert not any("delete" in name or "execute" in name for name in names)


def test_sensitive_and_binary_files_are_never_exposed(project):
    executor = make_executor(project)
    listed = executor.execute("s1", "fs_list", {"path": "."})
    names = {entry["name"] for entry in listed["entries"]}
    assert ".env" not in names and "deploy.key" not in names
    with pytest.raises(ToolExecutionError, match="敏感文件"):
        executor.execute("s1", "fs_read", {"path": ".env"})
    with pytest.raises(ToolExecutionError, match="敏感文件"):
        executor.execute("s1", "fs_write", {"path": "deploy.key", "content": "new"})
    with pytest.raises(ToolExecutionError, match="二进制"):
        executor.execute("s1", "fs_read", {"path": "image.bin"})
    searched = executor.execute("s1", "fs_search", {"query": "should-never-leak"})
    assert searched["hits"] == []


def test_fs_write_requires_latest_sha_for_existing_file(project):
    executor = make_executor(project)
    read = executor.execute("s1", "fs_read", {"path": "src/main.py"})
    with pytest.raises(ToolExecutionError, match="必须先 fs_read"):
        executor.execute("s1", "fs_write", {"path": "src/main.py", "content": "changed"})

    written = executor.execute("s1", "fs_write", {
        "path": "src/main.py", "content": "print('updated')\n", "expected_sha256": read["sha256"],
    })
    assert written["created"] is False
    assert (project / "src" / "main.py").read_text(encoding="utf-8") == "print('updated')\n"

    with pytest.raises(ToolExecutionError, match="发生变化"):
        executor.execute("s1", "fs_write", {
            "path": "src/main.py", "content": "stale", "expected_sha256": read["sha256"],
        })


def test_fs_write_creates_file_and_replace_is_exact(project):
    executor = make_executor(project)
    created = executor.execute("s1", "fs_write", {"path": "src/new.py", "content": "value = 1\n"})
    assert created["created"] is True
    read = executor.execute("s1", "fs_read", {"path": "src/new.py"})
    replaced = executor.execute("s1", "fs_replace", {
        "path": "src/new.py", "old_text": "value = 1", "new_text": "value = 2",
        "expected_sha256": read["sha256"],
    })
    assert replaced["replacements"] == 1
    assert (project / "src" / "new.py").read_text(encoding="utf-8") == "value = 2\n"

    (project / "repeated.txt").write_text("same same", encoding="utf-8")
    repeated = executor.execute("s1", "fs_read", {"path": "repeated.txt"})
    with pytest.raises(ToolExecutionError, match="出现 2 次"):
        executor.execute("s1", "fs_replace", {
            "path": "repeated.txt", "old_text": "same", "new_text": "next",
            "expected_sha256": repeated["sha256"],
        })


def test_fs_search_reports_partial_scan_for_large_file(project):
    marker = "large-file-head-marker"
    (project / "large.log").write_text(marker + "\n" + "x" * 600_000, encoding="utf-8")
    executor = make_executor(project)

    result = executor.execute("s1", "fs_search", {"query": marker})

    hit = next(item for item in result["hits"] if item["path"] == "large.log")
    assert hit == {"path": "large.log", "kind": "content", "partial": True}


def test_fs_search_does_not_claim_unscanned_large_file_tail(project):
    marker = "large-file-tail-marker"
    (project / "large.log").write_text("x" * 600_000 + marker, encoding="utf-8")

    result = make_executor(project).execute("s1", "fs_search", {"query": marker})

    assert not any(item["path"] == "large.log" for item in result["hits"])
