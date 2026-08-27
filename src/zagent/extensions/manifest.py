from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from zagent.domain.errors import ValidationError

EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
ALLOWED_CONTRIBUTIONS = {"tools", "views", "skills", "model_providers", "context_sources"}
ALLOWED_RUNTIMES = {"node", "python", "declarative"}


@dataclass(frozen=True)
class ExtensionManifest:
    extension_id: str
    version: str
    name: str
    root: str
    runtime: str
    entry: Optional[str]
    contributes: List[str]
    permissions: List[str]
    integrity: Optional[str]
    enabled: bool = False
    status: str = "discovered"

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["id"] = value.pop("extension_id")
        return value


class ExtensionRegistry:
    def __init__(self, data_dir: str, project_dir: Optional[str] = None) -> None:
        self._roots = [Path(data_dir) / "extensions"]
        if project_dir:
            self._roots.append(Path(project_dir) / ".zagent" / "extensions")

    def discover(self) -> List[ExtensionManifest]:
        manifests: List[ExtensionManifest] = []
        seen = set()
        for root in self._roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*/zagent.extension.json")):
                try:
                    manifest = self.load_manifest(path)
                except (ValidationError, OSError, json.JSONDecodeError):
                    continue
                if manifest.extension_id not in seen:
                    manifests.append(manifest)
                    seen.add(manifest.extension_id)
        return manifests

    def create_extension(self, spec: Dict[str, Any]) -> ExtensionManifest:
        """Create a declarative extension under the user data dir and return its manifest."""
        extension_id = str(spec.get("id", "")).strip().lower()
        if not EXTENSION_ID_RE.fullmatch(extension_id):
            raise ValidationError(
                "invalid extension id: use 3-128 chars of [a-z0-9._-], starting with a letter/digit"
            )
        runtime = str(spec.get("runtime", "declarative"))
        allowed = ", ".join(sorted(ALLOWED_RUNTIMES))
        if runtime not in ALLOWED_RUNTIMES:
            raise ValidationError(f"unsupported runtime '{runtime}' (allowed: {allowed})")
        contributes = [str(item) for item in spec.get("contributes", [])]
        unknown = [item for item in contributes if item not in ALLOWED_CONTRIBUTIONS]
        if unknown:
            raise ValidationError(f"unknown contribution(s): {', '.join(unknown)}")
        manifest_value = {
            "id": extension_id,
            "name": str(spec.get("name") or extension_id),
            "version": str(spec.get("version") or "0.0.0"),
            "runtime": runtime,
            "entry": str(spec["entry"]) if spec.get("entry") else None,
            "contributes": contributes,
            "permissions": [str(item) for item in spec.get("permissions", [])],
            "enabled": bool(spec.get("enabled", True)),
        }
        root = self._roots[0] / extension_id
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "zagent.extension.json"
        manifest_path.write_text(json.dumps(manifest_value, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.load_manifest(manifest_path)

    def remove_extension(self, extension_id: str) -> bool:
        """Delete a user extension directory. Returns False when it does not exist."""
        root = self._roots[0] / extension_id
        if not root.exists() or not (root / "zagent.extension.json").is_file():
            return False
        for child in sorted(root.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        root.rmdir()
        return True

    def load_manifest(self, path: Path) -> ExtensionManifest:
        value = json.loads(path.read_text(encoding="utf-8"))
        extension_id = str(value.get("id", ""))
        if not EXTENSION_ID_RE.fullmatch(extension_id):
            raise ValidationError("invalid extension id")
        runtime = value.get("runtime", "declarative")
        if runtime not in ALLOWED_RUNTIMES:
            raise ValidationError("unsupported extension runtime")
        contributes = list(value.get("contributes", []))
        if any(item not in ALLOWED_CONTRIBUTIONS for item in contributes):
            raise ValidationError("unknown extension contribution")
        entry = value.get("entry")
        resolved_entry = None
        if entry:
            resolved_entry = (path.parent / entry).resolve()
            if path.parent.resolve() not in resolved_entry.parents or not resolved_entry.is_file():
                raise ValidationError("invalid extension entry")
        integrity = value.get("integrity")
        status = self._verify_integrity(resolved_entry, integrity)
        return ExtensionManifest(
            extension_id=extension_id,
            version=str(value.get("version", "0.0.0")),
            name=str(value.get("name", extension_id)),
            root=str(path.parent),
            runtime=runtime,
            entry=entry,
            contributes=contributes,
            permissions=list(value.get("permissions", [])),
            integrity=integrity,
            enabled=bool(value.get("enabled", False)),
            status=status,
        )

    @staticmethod
    def _verify_integrity(entry: Optional[Path], integrity: Optional[str]) -> str:
        if not integrity or entry is None:
            return "discovered"
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        return "discovered" if integrity in {digest, "sha256-" + digest} else "integrity_failed"

