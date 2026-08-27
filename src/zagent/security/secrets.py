from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict


class SecretStore:
    """File-backed bootstrap store; desktop keychain adapter is planned for v1.1."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "secrets.json"

    def get(self, name: str) -> str:
        return os.environ.get(name, "") or self._read().get(name, "")

    def set(self, name: str, value: str) -> None:
        current = self._read()
        current[name] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(current), encoding="utf-8")
        os.chmod(self._path, 0o600)

    def _read(self) -> Dict[str, str]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

