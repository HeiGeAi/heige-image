import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from scripts.image_output import (
    ImageResponseError,
    OutputPathError,
    atomic_write_image,
    validate_image_response,
)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + _chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
    + _chunk(b"IEND", b"")
)


class FakeResponse:
    def __init__(self, content: bytes, content_type: str | None, *, chunk_size=None, content_length=None):
        self.content = content
        self.headers = {} if content_type is None else {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.chunk_size = chunk_size or max(1, len(content))
        self.chunks_read = 0

    def iter_bytes(self):
        for offset in range(0, len(self.content), self.chunk_size):
            self.chunks_read += 1
            yield self.content[offset:offset + self.chunk_size]


class ImageResponseTests(unittest.TestCase):
    def test_rejects_non_image_content_type(self):
        response = FakeResponse(PNG, "text/html; charset=utf-8")

        with self.assertRaisesRegex(ImageResponseError, "Content-Type"):
            validate_image_response(response)

    def test_rejects_missing_content_type(self):
        response = FakeResponse(PNG, None)

        with self.assertRaisesRegex(ImageResponseError, "Content-Type"):
            validate_image_response(response)

    def test_rejects_non_image_magic_bytes(self):
        response = FakeResponse(b"<html>not an image</html>", "image/png")

        with self.assertRaisesRegex(ImageResponseError, "PNG"):
            validate_image_response(response)

    def test_rejects_png_signature_without_complete_container(self):
        response = FakeResponse(b"\x89PNG\r\n\x1a\n" + b"payload", "image/png")

        with self.assertRaisesRegex(ImageResponseError, "PNG"):
            validate_image_response(response)

    def test_rejects_jpeg_for_png_output_contract(self):
        response = FakeResponse(b"\xff\xd8\xffpayload\xff\xd9", "image/jpeg")

        with self.assertRaisesRegex(ImageResponseError, "Content-Type"):
            validate_image_response(response)

    def test_rejects_oversized_response(self):
        response = FakeResponse(PNG + b"x" * 32, "image/png")

        with self.assertRaisesRegex(ImageResponseError, "过大"):
            validate_image_response(response, max_bytes=16)

    def test_stops_streaming_as_soon_as_limit_is_exceeded(self):
        response = FakeResponse(b"x" * 100, "image/png", chunk_size=10)

        with self.assertRaisesRegex(ImageResponseError, "过大"):
            validate_image_response(response, max_bytes=16)

        self.assertEqual(response.chunks_read, 2)

    def test_oversized_content_length_keeps_the_size_error(self):
        response = FakeResponse(PNG, "image/png", content_length=100)

        with self.assertRaisesRegex(ImageResponseError, "过大"):
            validate_image_response(response, max_bytes=16)

        self.assertEqual(response.chunks_read, 0)


class AtomicOutputTests(unittest.TestCase):
    def test_rejects_symlink_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            target = parent / "target.png"
            target.write_bytes(b"original")
            output = parent / "output.png"
            output.symlink_to(target)

            with self.assertRaisesRegex(OutputPathError, "符号链接"):
                atomic_write_image(output, PNG)

            self.assertEqual(target.read_bytes(), b"original")

    def test_replace_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp).resolve() / "output.png"
            output.write_bytes(b"original")

            with mock.patch.object(os, "replace", side_effect=OSError("boom")):
                with self.assertRaisesRegex(OSError, "boom"):
                    atomic_write_image(output, PNG)

            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(Path(tmp).glob(".heige-image-*")), [])

    def test_resolves_symlink_parent_directory(self):
        # 符号链接父目录（如 macOS /tmp）不再误拒：解析到真实目录落盘。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            outside = root / "outside"
            outside.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(outside, target_is_directory=True)

            out = atomic_write_image(linked_parent / "output.png", PNG)

            self.assertEqual(out, outside / "output.png")
            self.assertEqual((outside / "output.png").read_bytes(), PNG)

    def test_accepts_unresolved_tmp_style_output_path(self):
        # 回归：路径含符号链接层级（未 resolve 的临时目录）也必须可用。
        with tempfile.TemporaryDirectory() as tmp:
            out = atomic_write_image(Path(tmp) / "output.png", PNG)
            self.assertTrue(out.exists())
            self.assertEqual(out.read_bytes(), PNG)

    def test_rejects_non_png_output_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(OutputPathError, r"\.png"):
                atomic_write_image(Path(tmp).resolve() / "output.jpg", PNG)


if __name__ == "__main__":
    unittest.main()
