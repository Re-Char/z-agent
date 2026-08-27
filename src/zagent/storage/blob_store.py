from __future__ import annotations

import hashlib
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        target = self.root / digest
        if not target.exists():
            target.write_text(content, encoding="utf-8")
        return digest

    def get(self, digest: str) -> str:
        if not digest or any(char not in "0123456789abcdef" for char in digest) or len(digest) != 64:
            raise ValueError("invalid blob digest")
        return (self.root / digest).read_text(encoding="utf-8")

