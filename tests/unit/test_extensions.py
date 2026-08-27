import json

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
    import pytest

    from zagent.domain.errors import ValidationError

    registry = ExtensionRegistry(str(tmp_path / "data"))
    with pytest.raises(ValidationError):
        registry.create_extension({"id": "UPPER_CASE!"})
    with pytest.raises(ValidationError):
        registry.create_extension({"id": "com.example.bad", "runtime": "wasm"})
    with pytest.raises(ValidationError):
        registry.create_extension({"id": "com.example.bad", "contributes": ["shell"]})
