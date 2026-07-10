from io import BytesIO

from PIL import Image

from books_of_time.media.hasher import MediaHasher


def make_image_bytes(
    *,
    fmt: str = "PNG",
    size: tuple[int, int] = (3, 2),
    color: tuple[int, int, int] = (255, 0, 0),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


def test_media_hasher_extracts_image_metadata_and_pixel_hash() -> None:
    data = make_image_bytes(fmt="PNG", size=(3, 2))
    hasher = MediaHasher()

    metadata = hasher.inspect_image(data)

    assert metadata.width == 3
    assert metadata.height == 2
    assert metadata.mime_type == "image/png"
    assert metadata.file_ext == ".png"
    assert metadata.size_bytes == len(data)
    assert hasher.pixel_sha256(data) == metadata.pixel_sha256
    assert metadata.pixel_sha256 is not None


def test_media_hasher_computes_perceptual_hashes_for_decodable_images() -> None:
    data = make_image_bytes(fmt="PNG", size=(16, 16), color=(0, 255, 0))
    hasher = MediaHasher()

    metadata = hasher.inspect_image(data)

    assert metadata.ahash is None
    assert metadata.dhash is None
    assert isinstance(metadata.phash, int)
