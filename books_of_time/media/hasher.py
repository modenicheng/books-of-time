from __future__ import annotations

import hashlib


class MediaHasher:
    def blob_sha256(self, data: bytes) -> bytes:
        return hashlib.sha256(data).digest()
