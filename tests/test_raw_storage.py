import hashlib
import json
from datetime import UTC, datetime

import zstandard

from books_of_time.storage.filesystem import RawPayloadFileStore


def test_raw_payload_store_writes_zstd_file_under_date_and_run_id(tmp_path) -> None:
    store = RawPayloadFileStore(raw_dir=tmp_path)
    captured_at = datetime(2026, 7, 8, 12, 34, 56, tzinfo=UTC)
    payload = {"code": 0, "data": {"view": 123}}
    body = json.dumps(payload, ensure_ascii=False).encode()

    saved = store.save(
        body=body,
        captured_at=captured_at,
        run_id="run-123",
        suffix=".json",
    )

    expected_hash = hashlib.sha256(body).hexdigest()
    assert saved.payload_hash_hex == expected_hash
    assert saved.uncompressed_size == len(body)
    assert saved.compressed_size > 0
    assert saved.storage_uri == (
        f"file://{tmp_path / '2026' / '07' / '08' / 'run-123' / f'{expected_hash}.json.zst'}"
    )

    compressed = (
        tmp_path / "2026" / "07" / "08" / "run-123" / f"{expected_hash}.json.zst"
    ).read_bytes()
    assert zstandard.ZstdDecompressor().decompress(compressed) == body
