import json
import zipfile
from pathlib import Path

import pytest

from zagent.domain.errors import ValidationError
from zagent.extensions.manifest import ExtensionRegistry


def test_discover_declarative_extension(tmp_path):
    root = tmp_path / "data" / "extensions" / "sample"
    root.mkdir(parents=True)
    (root / "zagent.extension.json").write_text(json.dumps({
        "id": "com.example.sample", "version": "1.0.0", "runtime": "declarative",
        "contributes": ["skills"], "permissions": []
    }), encoding="utf-8")
    extensions = ExtensionRegistry(str(tmp_path / "data")).discover()
    assert extensions[0].extension_id == "com.example.sample"


def test_integrity_mismatch_is_reported(tmp_path):
    root = tmp_path / "data" / "extensions" / "sample"
    root.mkdir(parents=True)
    (root / "index.js").write_text("ok", encoding="utf-8")
    (root / "zagent.extension.json").write_text(json.dumps({
        "id": "com.example.sample", "version": "1.0.0", "runtime": "node", "entry": "index.js",
        "contributes": ["tools"], "permissions": [], "integrity": "sha256-wrong"
    }), encoding="utf-8")
    assert ExtensionRegistry(str(tmp_path / "data")).discover()[0].status == "integrity_failed"


def test_create_and_remove_extension(tmp_path):

    registry = ExtensionRegistry(str(tmp_path / "data"))
    manifest = registry.create_extension({
        "id": "com.example.my-tool", "name": "我的工具", "runtime": "declarative",
        "contributes": ["tools", "skills"], "permissions": ["read"],
    })
    assert manifest.extension_id == "com.example.my-tool"
    assert manifest.name == "我的工具"
    assert manifest.contributes == ["tools", "skills"]
    discovered = registry.discover()
    assert [item.extension_id for item in discovered] == ["com.example.my-tool"]
    assert registry.remove_extension("com.example.my-tool")
    assert registry.discover() == []
    assert not registry.remove_extension("com.example.my-tool")


def test_create_extension_rejects_invalid_specs(tmp_path):
    registry = ExtensionRegistry(str(tmp_path / "data"))
    with pytest.raises(ValidationError):
        registry.create_extension({"id": "UPPER_CASE!"})
    with pytest.raises(ValidationError):
        registry.create_extension({"id": "com.example.bad", "runtime": "wasm"})
    with pytest.raises(ValidationError):
        registry.create_extension({"id": "com.example.bad", "contributes": ["shell"]})


def test_import_real_directory_persists_metadata_and_enable_state(tmp_path):
    source = tmp_path / "source-extension"
    source.mkdir()
    entry = source / "extension.json"
    entry.write_text('{"commands": ["hello"]}', encoding="utf-8")
    (source / "zagent.extension.json").write_text(
        json.dumps(
            {
                "id": "com.example.imported",
                "name": "Imported",
                "version": "2.0.0",
                "runtime": "declarative",
                "entry": "extension.json",
                "contributes": ["tools"],
                "permissions": [],
            }
        ),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    registry = ExtensionRegistry(str(data_dir))
    imported = registry.import_extension(str(source))
    assert imported.status == "installed"
    assert imported.enabled is False
    assert len(imported.package_sha256 or "") == 64
    assert imported.installed_at
    assert Path(imported.root).parent == data_dir / "extensions"

    enabled = registry.set_enabled("com.example.imported", True)
    assert enabled.enabled is True
    restarted = ExtensionRegistry(str(data_dir)).discover()[0]
    assert restarted.enabled is True
    assert restarted.package_sha256 == imported.package_sha256
    (Path(restarted.root) / "extension.json").write_text('{"commands": []}', encoding="utf-8")
    assert ExtensionRegistry(str(data_dir)).discover()[0].status == "package_modified"


def test_import_real_zip_with_top_level_directory(tmp_path):
    archive_path = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "sample/zagent.extension.json",
            json.dumps(
                {
                    "id": "com.example.zipped",
                    "version": "1.0.0",
                    "runtime": "declarative",
                    "contributes": ["skills"],
                }
            ),
        )
        archive.writestr("sample/README.md", "真实 ZIP 扩展")
    imported = ExtensionRegistry(str(tmp_path / "data")).import_extension(
        str(archive_path), enabled=True
    )
    assert imported.extension_id == "com.example.zipped"
    assert imported.enabled is True
    assert (Path(imported.root) / "README.md").read_text(encoding="utf-8") == "真实 ZIP 扩展"


def test_import_rejects_zip_traversal_symlinks_and_duplicates(tmp_path):
    registry = ExtensionRegistry(str(tmp_path / "data"))
    unsafe_zip = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe_zip, "w") as archive:
        archive.writestr("../zagent.extension.json", "{}")
    with pytest.raises(ValidationError, match="unsafe ZIP path"):
        registry.import_extension(str(unsafe_zip))

    source = tmp_path / "symlink-package"
    source.mkdir()
    (source / "zagent.extension.json").write_text(
        json.dumps(
            {
                "id": "com.example.symlink",
                "runtime": "declarative",
                "contributes": [],
            }
        ),
        encoding="utf-8",
    )
    (source / "outside").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ValidationError, match="symlinks"):
        registry.import_extension(str(source))

    clean = tmp_path / "clean-package"
    clean.mkdir()
    (clean / "zagent.extension.json").write_text(
        json.dumps(
            {
                "id": "com.example.duplicate",
                "runtime": "declarative",
                "contributes": [],
            }
        ),
        encoding="utf-8",
    )
    registry.import_extension(str(clean))
    with pytest.raises(ValidationError, match="already exists"):
        registry.import_extension(str(clean))
    manifest_value = json.loads((clean / "zagent.extension.json").read_text(encoding="utf-8"))
    manifest_value["version"] = "2.0.0"
    (clean / "zagent.extension.json").write_text(json.dumps(manifest_value), encoding="utf-8")
    assert registry.import_extension(str(clean), replace=True).version == "2.0.0"
