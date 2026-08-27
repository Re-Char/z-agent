import hashlib

import pytest

from zagent.storage.blob_store import BlobStore


def test_blob_store_is_content_addressed(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put("中文内容")
    assert digest == hashlib.sha256("中文内容".encode()).hexdigest()
    assert store.get(digest) == "中文内容"


def test_blob_store_rejects_invalid_digest(tmp_path):
    with pytest.raises(ValueError):
        BlobStore(tmp_path).get("../secret")

