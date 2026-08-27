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
