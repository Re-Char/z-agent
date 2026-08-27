
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


def test_fs_tools_are_read_only_and_exposed_in_schema(project):
    executor = make_executor(project)
    names = [schema["function"]["name"] for schema in executor.schemas]
    assert names == ["fs_list", "fs_read", "fs_search", "fs_project_overview"]
    # no write tools exist
    assert not any("write" in name or "delete" in name or "edit" in name for name in names)


def test_fs_search_reports_partial_field(project):
    result = make_executor(project).execute("s1", "fs_search", {"query": "main"})
    assert all("partial" in hit for hit in result["hits"])
