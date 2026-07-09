import hashlib

import pytest

from books_of_time.media.storage import MediaStore


@pytest.mark.asyncio
async def test_media_store_writes_blob_under_sha256_fanout_path(tmp_path) -> None:
    store = MediaStore(tmp_path)
    blob = b"image-bytes"
    digest = hashlib.sha256(blob).digest()

    storage_uri = await store.put(digest, blob, ".jpg")

    hex_digest = hashlib.sha256(blob).hexdigest()
    expected_path = tmp_path / "sha256" / hex_digest[:2] / hex_digest[2:4]
    expected_file = expected_path / f"{hex_digest}.jpg"
    assert storage_uri == f"file://{expected_file}"
    assert expected_file.read_bytes() == blob
    assert await store.exists(digest, ".jpg") is True


def test_media_store_rejects_non_file_uri(tmp_path) -> None:
    store = MediaStore(tmp_path)

    with pytest.raises(ValueError, match="Unsupported media storage URI"):
        store.read_uri("s3://bucket/key")
