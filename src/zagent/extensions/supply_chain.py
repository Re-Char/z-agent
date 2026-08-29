from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from zagent.domain.errors import ValidationError

SBOM_NAME = ".zagent-sbom.cdx.json"
SIGNATURE_NAME = ".zagent-signature.json"
INSTALL_METADATA_NAME = ".zagent-install.json"
SUPPLY_CHAIN_FILES = {SBOM_NAME, SIGNATURE_NAME, INSTALL_METADATA_NAME}


class ExtensionSupplyChain:
    """CycloneDX SBOM generation and local Ed25519 installation attestation."""

    def __init__(self, data_dir: str) -> None:
        trust_root = Path(data_dir) / "trust"
        self._private_path = trust_root / "extension-host-ed25519.pem"
        self._public_path = trust_root / "extension-host-ed25519.pub.pem"

    def seal(
        self,
        root: Path,
        *,
        extension_id: str,
        version: str,
        permissions: list[str],
        package_sha256: str,
    ) -> Dict[str, Any]:
        if any((root / name).exists() for name in SUPPLY_CHAIN_FILES):
            raise ValidationError("extension source contains reserved Z-Agent supply-chain files")
        sbom = self._sbom(root, extension_id, version)
        self._write_json(root / SBOM_NAME, sbom)
        sbom_sha256 = hashlib.sha256(self._canonical(sbom)).hexdigest()
        payload = {
            "schema_version": 1,
            "extension_id": extension_id,
            "version": version,
            "permissions": sorted(permissions),
            "package_sha256": package_sha256,
            "sbom_sha256": sbom_sha256,
        }
        private_key = self._private_key()
        signature = private_key.sign(self._canonical(payload))
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        envelope = {
            **payload,
            "algorithm": "Ed25519",
            "key_id": hashlib.sha256(public_raw).hexdigest(),
            "signature": base64.b64encode(signature).decode("ascii"),
            "signed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_json(root / SIGNATURE_NAME, envelope)
        return envelope

    def verify(
        self,
        root: Path,
        *,
        extension_id: str,
        version: str,
        permissions: list[str],
        package_sha256: str,
    ) -> str:
        sbom_path = root / SBOM_NAME
        signature_path = root / SIGNATURE_NAME
        if not sbom_path.is_file() or not signature_path.is_file():
            return "unsigned"
        try:
            sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
            envelope = json.loads(signature_path.read_text(encoding="utf-8"))
            if not isinstance(sbom, dict) or not isinstance(envelope, dict):
                return "signature_failed"
            payload = {
                key: envelope.get(key)
                for key in (
                    "schema_version",
                    "extension_id",
                    "version",
                    "permissions",
                    "package_sha256",
                    "sbom_sha256",
                )
            }
            expected = {
                "schema_version": 1,
                "extension_id": extension_id,
                "version": version,
                "permissions": sorted(permissions),
                "package_sha256": package_sha256,
                "sbom_sha256": hashlib.sha256(self._canonical(sbom)).hexdigest(),
            }
            if payload != expected or envelope.get("algorithm") != "Ed25519":
                return "signature_failed"
            public_key = self._public_key()
            public_raw = public_key.public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            if envelope.get("key_id") != hashlib.sha256(public_raw).hexdigest():
                return "signature_failed"
            public_key.verify(
                base64.b64decode(str(envelope.get("signature", "")), validate=True),
                self._canonical(payload),
            )
        except (OSError, ValueError, json.JSONDecodeError, InvalidSignature):
            return "signature_failed"
        return "verified"

    @staticmethod
    def content_digest(root: Path, files: Iterable[Path]) -> str:
        digest = hashlib.sha256()
        for path in files:
            if path.name in SUPPLY_CHAIN_FILES:
                continue
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _private_key(self) -> Ed25519PrivateKey:
        if self._private_path.is_file():
            os.chmod(self._private_path, 0o600)
            value = serialization.load_pem_private_key(self._private_path.read_bytes(), password=None)
            if not isinstance(value, Ed25519PrivateKey):
                raise ValidationError("extension signing key is not Ed25519")
            return value
        self._private_path.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        self._private_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        os.chmod(self._private_path, 0o600)
        self._public_path.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        os.chmod(self._public_path, 0o644)
        return key

    def _public_key(self) -> Ed25519PublicKey:
        if not self._public_path.is_file():
            self._private_key()
        value = serialization.load_pem_public_key(self._public_path.read_bytes())
        if not isinstance(value, Ed25519PublicKey):
            raise ValidationError("extension verification key is not Ed25519")
        return value

    @staticmethod
    def _sbom(root: Path, extension_id: str, version: str) -> Dict[str, Any]:
        components = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if path.name in SUPPLY_CHAIN_FILES:
                continue
            components.append(
                {
                    "type": "file",
                    "name": path.relative_to(root).as_posix(),
                    "hashes": [{"alg": "SHA-256", "content": hashlib.sha256(path.read_bytes()).hexdigest()}],
                }
            )
        components.extend(ExtensionSupplyChain._dependency_components(root))
        return {
            "bomFormat": "CycloneDX",
            "specVersion": "1.7",
            "serialNumber": f"urn:uuid:{uuid.uuid4()}",
            "version": 1,
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "component": {
                    "type": "application",
                    "name": extension_id,
                    "version": version,
                },
            },
            "components": components,
        }

    @staticmethod
    def _dependency_components(root: Path) -> list[Dict[str, Any]]:
        dependencies: Dict[tuple[str, str], Dict[str, Any]] = {}
        package_lock = root / "package-lock.json"
        if package_lock.is_file():
            try:
                value = json.loads(package_lock.read_text(encoding="utf-8"))
                packages = value.get("packages", {}) if isinstance(value, dict) else {}
                if isinstance(packages, dict):
                    for location, item in packages.items():
                        if not location or not isinstance(item, dict):
                            continue
                        name = item.get("name") or str(location).rsplit("node_modules/", 1)[-1]
                        version = item.get("version")
                        if isinstance(name, str) and isinstance(version, str):
                            dependencies[(name, version)] = {
                                "type": "library",
                                "name": name,
                                "version": version,
                                "properties": [{"name": "zagent:source", "value": "npm-lock"}],
                            }
            except (OSError, json.JSONDecodeError):
                pass
        requirements = root / "requirements.txt"
        if requirements.is_file():
            for raw in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                name, separator, version = line.partition("==")
                if separator and name.strip() and version.strip():
                    dependencies[(name.strip(), version.strip())] = {
                        "type": "library",
                        "name": name.strip(),
                        "version": version.strip(),
                        "properties": [{"name": "zagent:source", "value": "requirements"}],
                    }
        return [dependencies[key] for key in sorted(dependencies)]

    @staticmethod
    def _canonical(value: Dict[str, Any]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )

    @staticmethod
    def _write_json(path: Path, value: Dict[str, Any]) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
