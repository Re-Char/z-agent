from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from zagent.domain.errors import ValidationError

EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
ALLOWED_CONTRIBUTIONS = {"tools", "views", "skills", "model_providers", "context_sources"}
ALLOWED_RUNTIMES = {"node", "python", "declarative"}
MANIFEST_NAME = "zagent.extension.json"
INSTALL_METADATA_NAME = ".zagent-install.json"
MAX_PACKAGE_FILES = 2_048
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024


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
    package_sha256: Optional[str] = None
    installed_at: Optional[str] = None

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
            for path in sorted(root.glob(f"*/{MANIFEST_NAME}")):
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
        manifest_value = self._manifest_value(spec)
        root = self._extension_root(manifest_value["id"])
        if root.exists():
            raise ValidationError(f"extension already exists: {manifest_value['id']}")
        root.mkdir(parents=True, exist_ok=False)
        manifest_path = root / MANIFEST_NAME
        self._write_json(manifest_path, manifest_value)
        return self.load_manifest(manifest_path)

    def import_extension(
        self, source_path: str, *, enabled: bool = False, replace: bool = False
    ) -> ExtensionManifest:
        """Safely install an extension directory or ZIP after validating it in staging."""
        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            raise ValidationError(f"extension package does not exist: {source_path}")
        if not source.is_dir() and not (source.is_file() and zipfile.is_zipfile(source)):
            raise ValidationError("extension package must be a directory or ZIP archive")

        install_root = self._roots[0]
        install_root.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix=".install-", dir=install_root))
        package_root = staging_root / "package"
        try:
            if source.is_dir():
                self._copy_directory(source, package_root)
            else:
                self._extract_zip(source, package_root)
            package_root = self._locate_package_root(package_root)
            manifest_path = package_root / MANIFEST_NAME
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest_value, dict):
                raise ValidationError("extension manifest must be a JSON object")
            manifest = self.load_manifest(manifest_path)
            if manifest.status == "integrity_failed":
                raise ValidationError("extension entry integrity check failed")
            digest = self._package_digest(package_root)
            self._write_json(
                package_root / INSTALL_METADATA_NAME,
                {
                    "schema_version": 1,
                    "source_name": source.name,
                    "source_type": "directory" if source.is_dir() else "zip",
                    "package_sha256": digest,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "enabled": bool(enabled),
                },
            )
            target = self._extension_root(manifest.extension_id)
            if target.is_symlink():
                raise ValidationError("refusing to replace a symlinked extension root")
            if target.exists() and not replace:
                raise ValidationError(f"extension already exists: {manifest.extension_id}")
            backup = staging_root / "previous"
            if target.exists():
                target.replace(backup)
            try:
                package_root.replace(target)
            except OSError:
                if backup.exists() and not target.exists():
                    backup.replace(target)
                raise
            return self.load_manifest(target / MANIFEST_NAME)
        except (OSError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise ValidationError(f"invalid extension package: {exc}") from exc
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)

    def set_enabled(self, extension_id: str, enabled: bool) -> ExtensionManifest:
        root = self._extension_root(extension_id)
        manifest_path = root / MANIFEST_NAME
        if not manifest_path.is_file():
            raise ValidationError(f"extension not found: {extension_id}")
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValidationError("extension manifest must be a JSON object")
        metadata = self._read_install_metadata(root)
        if metadata:
            metadata["enabled"] = bool(enabled)
            self._write_json(root / INSTALL_METADATA_NAME, metadata)
        else:
            value["enabled"] = bool(enabled)
            self._write_json(manifest_path, value)
        return self.load_manifest(manifest_path)

    def remove_extension(self, extension_id: str) -> bool:
        """Delete one validated user extension directory only."""
        root = self._extension_root(extension_id)
        if not root.exists() or not (root / MANIFEST_NAME).is_file():
            return False
        self._remove_tree(root)
        return True

    def load_manifest(self, path: Path) -> ExtensionManifest:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValidationError("extension manifest must be a JSON object")
        extension_id = str(value.get("id", ""))
        if not EXTENSION_ID_RE.fullmatch(extension_id):
            raise ValidationError("invalid extension id")
        runtime = value.get("runtime", "declarative")
        if runtime not in ALLOWED_RUNTIMES:
            raise ValidationError("unsupported extension runtime")
        contributes_value = value.get("contributes", [])
        if not isinstance(contributes_value, list):
            raise ValidationError("extension contributes must be an array")
        contributes = [str(item) for item in contributes_value]
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
        metadata = self._read_install_metadata(path.parent)
        if metadata and status != "integrity_failed":
            recorded_digest = metadata.get("package_sha256")
            status = (
                "installed"
                if recorded_digest and recorded_digest == self._package_digest(path.parent)
                else "package_modified"
            )
        permissions_value = value.get("permissions", [])
        if not isinstance(permissions_value, list):
            raise ValidationError("extension permissions must be an array")
        return ExtensionManifest(
            extension_id=extension_id,
            version=str(value.get("version", "0.0.0")),
            name=str(value.get("name", extension_id)),
            root=str(path.parent),
            runtime=runtime,
            entry=entry,
            contributes=contributes,
            permissions=[str(item) for item in permissions_value],
            integrity=integrity,
            enabled=bool(metadata.get("enabled", False)) if metadata else bool(value.get("enabled", False)),
            status=status,
            package_sha256=metadata.get("package_sha256") if metadata else None,
            installed_at=metadata.get("installed_at") if metadata else None,
        )

    def _extension_root(self, extension_id: str) -> Path:
        normalized = str(extension_id).strip().lower()
        if not EXTENSION_ID_RE.fullmatch(normalized):
            raise ValidationError("invalid extension id")
        root = self._roots[0].resolve() / normalized
        if root.parent != self._roots[0].resolve():
            raise ValidationError("extension path escapes install root")
        return root

    @staticmethod
    def _manifest_value(spec: Dict[str, Any]) -> Dict[str, Any]:
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
        return {
            "id": extension_id,
            "name": str(spec.get("name") or extension_id),
            "version": str(spec.get("version") or "0.0.0"),
            "runtime": runtime,
            "entry": str(spec["entry"]) if spec.get("entry") else None,
            "contributes": contributes,
            "permissions": [str(item) for item in spec.get("permissions", [])],
            "enabled": bool(spec.get("enabled", True)),
        }

    @staticmethod
    def _copy_directory(source: Path, destination: Path) -> None:
        files = ExtensionRegistry._validated_directory_files(source)
        destination.mkdir(parents=True, exist_ok=False)
        for path in files:
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)

    @staticmethod
    def _validated_directory_files(source: Path) -> List[Path]:
        files: List[Path] = []
        total = 0
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValidationError(f"extension packages cannot contain symlinks: {path.name}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValidationError(f"extension packages may contain regular files only: {path.name}")
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValidationError(f"extension file exceeds {MAX_FILE_BYTES} bytes: {path.name}")
            total += size
            files.append(path)
            if len(files) > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                raise ValidationError("extension package exceeds safety limits")
        return files

    @staticmethod
    def _extract_zip(source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        count = 0
        total = 0
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                raw_name = info.filename.replace("\\", "/")
                relative = PurePosixPath(raw_name)
                if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                    raise ValidationError(f"unsafe ZIP path: {info.filename}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValidationError(f"ZIP symlinks are not allowed: {info.filename}")
                if info.is_dir():
                    continue
                count += 1
                total += info.file_size
                if info.flag_bits & 0x1:
                    raise ValidationError("encrypted ZIP files are not supported")
                if info.file_size > MAX_FILE_BYTES:
                    raise ValidationError(f"extension file exceeds {MAX_FILE_BYTES} bytes: {info.filename}")
                if count > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                    raise ValidationError("extension package exceeds safety limits")
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source_file, target.open("wb") as target_file:
                    shutil.copyfileobj(source_file, target_file, length=1024 * 1024)

    @staticmethod
    def _locate_package_root(extracted_root: Path) -> Path:
        if (extracted_root / MANIFEST_NAME).is_file():
            return extracted_root
        manifests = list(extracted_root.glob(f"*/{MANIFEST_NAME}"))
        top_level = [path for path in extracted_root.iterdir() if path.name != "__MACOSX"]
        if len(manifests) == 1 and len(top_level) == 1 and top_level[0].is_dir():
            return manifests[0].parent
        raise ValidationError(f"extension package must contain one root {MANIFEST_NAME}")

    @staticmethod
    def _package_digest(root: Path) -> str:
        digest = hashlib.sha256()
        for path in ExtensionRegistry._validated_directory_files(root):
            if path.name == INSTALL_METADATA_NAME:
                continue
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _read_install_metadata(root: Path) -> Dict[str, Any]:
        path = root / INSTALL_METADATA_NAME
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_json(path: Path, value: Dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _remove_tree(root: Path) -> None:
        if root.is_symlink():
            raise ValidationError("refusing to remove a symlinked extension root")
        shutil.rmtree(root)

    @staticmethod
    def _verify_integrity(entry: Optional[Path], integrity: Optional[str]) -> str:
        if not integrity or entry is None:
            return "discovered"
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        return "discovered" if integrity in {digest, "sha256-" + digest} else "integrity_failed"
