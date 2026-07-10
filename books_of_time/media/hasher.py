from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from io import BytesIO
from statistics import median

from PIL import Image, UnidentifiedImageError


@dataclass(frozen=True)
class MediaImageMetadata:
    width: int | None
    height: int | None
    mime_type: str | None
    file_ext: str | None
    size_bytes: int
    pixel_sha256: bytes | None
    phash: int | None
    dhash: int | None
    ahash: int | None


class MediaHasher:
    def blob_sha256(self, data: bytes) -> bytes:
        return hashlib.sha256(data).digest()

    def pixel_sha256(self, data: bytes) -> bytes | None:
        return self.inspect_image(data).pixel_sha256

    def inspect_image(self, data: bytes) -> MediaImageMetadata:
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                rgba = image.convert("RGBA")
                pixel_sha256 = _pixel_sha256(rgba)
                return MediaImageMetadata(
                    width=image.width,
                    height=image.height,
                    mime_type=Image.MIME.get(image.format or ""),
                    file_ext=_file_ext_for_format(image.format),
                    size_bytes=len(data),
                    pixel_sha256=pixel_sha256,
                    phash=_phash(image),
                    dhash=None,
                    ahash=None,
                )
        except (OSError, UnidentifiedImageError):
            return MediaImageMetadata(
                width=None,
                height=None,
                mime_type=None,
                file_ext=None,
                size_bytes=len(data),
                pixel_sha256=None,
                phash=None,
                dhash=None,
                ahash=None,
            )


def _pixel_sha256(image: Image.Image) -> bytes:
    header = f"{image.mode}:{image.width}x{image.height}:".encode()
    return hashlib.sha256(header + image.tobytes()).digest()


def _file_ext_for_format(image_format: str | None) -> str | None:
    if image_format == "JPEG":
        return ".jpg"
    if image_format is None:
        return None
    for ext, registered_format in Image.registered_extensions().items():
        if registered_format == image_format:
            return ext
    return None


def _ahash(image: Image.Image) -> int:
    grayscale = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels = _pixel_values(grayscale)
    threshold = sum(pixels) / len(pixels)
    return _bits_to_int(pixel >= threshold for pixel in pixels)


def _dhash(image: Image.Image) -> int:
    grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = _pixel_values(grayscale)
    bits: list[bool] = []
    for y in range(8):
        row_offset = y * 9
        for x in range(8):
            bits.append(pixels[row_offset + x] > pixels[row_offset + x + 1])
    return _bits_to_int(bits)


def _phash(image: Image.Image) -> int:
    size = 32
    grayscale = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = [float(value) for value in _pixel_values(grayscale)]
    coefficients = [
        _dct_coefficient(pixels, size=size, u=u, v=v)
        for v in range(8)
        for u in range(8)
    ]
    threshold = median(coefficients[1:])
    return _bits_to_int(coefficient >= threshold for coefficient in coefficients)


def _dct_coefficient(pixels: list[float], *, size: int, u: int, v: int) -> float:
    cu = 1 / math.sqrt(2) if u == 0 else 1.0
    cv = 1 / math.sqrt(2) if v == 0 else 1.0
    total = 0.0
    for y in range(size):
        for x in range(size):
            total += (
                pixels[y * size + x]
                * math.cos(((2 * x + 1) * u * math.pi) / (2 * size))
                * math.cos(((2 * y + 1) * v * math.pi) / (2 * size))
            )
    return 0.25 * cu * cv * total


def _pixel_values(image: Image.Image) -> list[int]:
    get_flattened_data = getattr(image, "get_flattened_data", None)
    if get_flattened_data is not None:
        return list(get_flattened_data())
    return list(image.getdata())


def _bits_to_int(bits) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    if value >= 2**63:
        return value - 2**64
    return value
