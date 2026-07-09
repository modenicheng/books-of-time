from __future__ import annotations

from pathlib import Path


class MediaStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)

    async def put(self, blob_sha256: bytes, data: bytes, ext: str) -> str:
        path = self.path_for(blob_sha256, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return f"file://{path}"

    async def exists(self, blob_sha256: bytes, ext: str) -> bool:
        return self.path_for(blob_sha256, ext).exists()

    def read_uri(self, storage_uri: str) -> bytes:
        if not storage_uri.startswith("file://"):
            raise ValueError(f"Unsupported media storage URI: {storage_uri}")
        return Path(storage_uri.removeprefix("file://")).read_bytes()

    def path_for(self, blob_sha256: bytes, ext: str) -> Path:
        hex_digest = blob_sha256.hex()
        normalized_ext = _normalize_ext(ext)
        return (
            self.root_dir
            / "sha256"
            / hex_digest[:2]
            / hex_digest[2:4]
            / f"{hex_digest}{normalized_ext}"
        )


def _normalize_ext(ext: str) -> str:
    stripped = ext.strip().lower()
    if not stripped:
        return ".bin"
    if not stripped.startswith("."):
        stripped = f".{stripped}"
    if not stripped[1:].replace("-", "").isalnum():
        return ".bin"
    return stripped
