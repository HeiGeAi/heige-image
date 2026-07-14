#!/usr/bin/env python3
"""Regression tests for API-engine input and response handling."""

import base64
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import httpx


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
GEN = SCRIPTS / "gen.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen  # noqa: E402


def _png_chunk(kind, payload):
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _valid_png(width=1024, height=1024):
    """Build a minimal valid 1-bit grayscale PNG at the requested API size."""
    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    row = b"\x00" + b"\x00" * ((width + 7) // 8)
    pixels = zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def _png_with_compressed_pixels(pixels):
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


class _FakeResponse:
    def __init__(self, payload=None, *, json_error=None, status_code=200, text=""):
        self.payload = payload
        self.json_error = json_error
        self.status_code = status_code
        self.text = text

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class _FakeDownloadResponse:
    def __init__(
        self,
        chunks,
        headers=None,
        *,
        status_code=200,
        url="https://example.invalid/image.png",
    ):
        self.chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status_code
        self.url = httpx.URL(url)
        self.iterated = False
        self.iteration_count = 0

    def raise_for_status(self):
        return None

    def iter_raw(self):
        self.iterated = True
        for chunk in self.chunks:
            self.iteration_count += 1
            yield chunk

    def iter_bytes(self):
        raise AssertionError("bounded readers must consume raw, undecoded bytes")


class _FakeApiStreamResponse:
    def __init__(self, chunks, headers=None, status_code=200):
        self.chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://example.invalid/v1/images/generations")
        self.iteration_count = 0

    def iter_raw(self):
        for chunk in self.chunks:
            self.iteration_count += 1
            yield chunk

    def iter_bytes(self):
        raise AssertionError("bounded readers must consume raw, undecoded bytes")


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, tb):
        return False


class ProviderContractDocumentationTests(unittest.TestCase):
    def test_contract_files_do_not_overpromise_unsupported_providers(self):
        contract_files = (
            ROOT / "README.md",
            ROOT / "SKILL.md",
            ROOT / "scripts" / "gen.py",
            ROOT / "config.example.json",
        )
        forbidden_phrases = (
            "走任意 OpenAI 兼容",
            "对接任意 OpenAI 兼容",
            "任意中转",
            "任意官方或中转",
            "接任意 OpenAI 兼容 API",
            "接口通用",
            "any OpenAI-compatible image API",
            "Works with OpenAI official, Azure",
            "官方 OpenAI、Azure、各家中转都能接",
            "可以用 gptx.cc 这个渠道",
            "gptx.cc is one recommended option",
            "扩散模型实际只出这三种",
            "gpt-image-2 实际只出方 / 横 / 竖三种",
            "精确画幅 API 做不到",
        )

        for path in contract_files:
            contents = path.read_text(encoding="utf-8")
            for phrase in forbidden_phrases:
                with self.subTest(path=path.name, phrase=phrase):
                    self.assertNotIn(phrase, contents)


class GenerateCoreTests(unittest.TestCase):
    def test_generation_response_is_bounded_while_streaming(self):
        streamed = _FakeApiStreamResponse([b"a" * 10, b"b" * 10, b"c" * 10])
        buffered = httpx.Response(200, content=b"x" * 30)
        with mock.patch.object(gen, "MAX_API_RESPONSE_BYTES", 16):
            with mock.patch.object(gen.httpx.Client, "post", return_value=buffered):
                with mock.patch.object(
                    gen.httpx.Client,
                    "stream",
                    return_value=_FakeStreamContext(streamed),
                ):
                    with self.assertRaisesRegex(gen.ImagePayloadError, "API 响应"):
                        gen._request_once({}, 1, "fake", "https://example.invalid/v1")
        self.assertEqual(streamed.iteration_count, 2)

    def test_compressed_generation_response_is_rejected_before_body_decode(self):
        streamed = _FakeApiStreamResponse(
            [zlib.compress(b"x" * 1024 * 1024)],
            headers={"content-encoding": "deflate"},
        )
        with mock.patch.object(
            gen.httpx.Client,
            "stream",
            return_value=_FakeStreamContext(streamed),
        ) as stream:
            with self.assertRaisesRegex(gen.ImagePayloadError, "Content-Encoding"):
                gen._request_once({}, 1, "fake", "https://example.invalid/v1")
        self.assertEqual(streamed.iteration_count, 0)
        self.assertEqual(stream.call_args.kwargs["headers"]["Accept-Encoding"], "identity")

    def test_compressed_download_is_rejected_before_body_decode(self):
        streamed = _FakeDownloadResponse(
            [zlib.compress(b"x" * 1024 * 1024)],
            headers={"content-encoding": "gzip"},
        )
        with mock.patch.object(
            gen.httpx,
            "stream",
            return_value=_FakeStreamContext(streamed),
        ) as stream:
            with self.assertRaisesRegex(gen.ImagePayloadError, "Content-Encoding"):
                gen._download_image_bytes("https://example.invalid/image.png")
        self.assertEqual(streamed.iteration_count, 0)
        self.assertEqual(stream.call_args.kwargs["headers"]["Accept-Encoding"], "identity")

    def test_compressed_redirect_is_rejected_before_following_or_decoding(self):
        redirect = _FakeDownloadResponse(
            [zlib.compress(b"x" * 16 * 1024 * 1024)],
            headers={
                "content-encoding": "gzip",
                "location": "/final.png",
            },
            status_code=302,
            url="https://example.invalid/start",
        )
        target = _FakeDownloadResponse([_valid_png()])
        with mock.patch.object(
            gen.httpx,
            "stream",
            side_effect=[_FakeStreamContext(redirect), _FakeStreamContext(target)],
        ) as stream:
            with self.assertRaisesRegex(gen.ImagePayloadError, "Content-Encoding"):
                gen._download_image_bytes("https://example.invalid/start")
        self.assertEqual(redirect.iteration_count, 0)
        self.assertEqual(stream.call_count, 1)
        self.assertFalse(stream.call_args.kwargs["follow_redirects"])

    def test_identity_redirect_is_followed_manually_with_a_bounded_hop_count(self):
        redirect = _FakeDownloadResponse(
            [],
            headers={"location": "/final.png"},
            status_code=302,
            url="https://example.invalid/start",
        )
        target = _FakeDownloadResponse(
            [_valid_png()],
            headers={"content-type": "image/png"},
            url="https://example.invalid/final.png",
        )
        with mock.patch.object(
            gen.httpx,
            "stream",
            side_effect=[_FakeStreamContext(redirect), _FakeStreamContext(target)],
        ) as stream:
            payload = gen._download_image_bytes("https://example.invalid/start")
        self.assertEqual(payload, _valid_png())
        self.assertEqual(stream.call_count, 2)
        self.assertEqual(stream.call_args_list[1].args[1], "https://example.invalid/final.png")
        self.assertTrue(all(not call.kwargs["follow_redirects"] for call in stream.call_args_list))

    def test_existing_png_directory_is_rejected_before_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "directory.png"
            output.mkdir()
            with mock.patch.object(gen, "_request_once") as request:
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=str(output),
                    max_retries=0,
                )
        self.assertFalse(result["success"])
        self.assertIn("目录", result["error"])
        request.assert_not_called()

    def test_parent_file_is_rejected_before_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "not-a-directory"
            parent.write_text("x", encoding="utf-8")
            with mock.patch.object(gen, "_request_once") as request:
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=str(parent / "image.png"),
                    max_retries=0,
                )
        self.assertFalse(result["success"])
        self.assertIn("父路径", result["error"])
        request.assert_not_called()

    def test_unwritable_parent_is_rejected_before_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "read-only"
            parent.mkdir()
            parent.chmod(0o500)
            try:
                if os.access(parent, os.W_OK):
                    self.skipTest("current user can still write to chmod 0500 directory")
                with mock.patch.object(gen, "_request_once") as request:
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        output_path=str(parent / "image.png"),
                        max_retries=0,
                    )
                self.assertFalse(result["success"])
                self.assertIn("不可写", result["error"])
                request.assert_not_called()
            finally:
                parent.chmod(0o700)

    def test_existing_output_in_unwritable_parent_is_rejected_before_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "read-only"
            parent.mkdir()
            output = parent / "image.png"
            output.write_bytes(_valid_png())
            parent.chmod(0o500)
            try:
                if os.access(parent, os.W_OK | os.X_OK):
                    self.skipTest("current user can still write to chmod 0500 directory")
                with mock.patch.object(gen, "_request_once") as request:
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        output_path=str(output),
                        max_retries=0,
                    )
                self.assertFalse(result["success"])
                self.assertIn("父目录不可写", result["error"])
                request.assert_not_called()
            finally:
                parent.chmod(0o700)

    def test_symbolic_link_output_is_rejected_before_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.png"
            target.write_bytes(_valid_png())
            output = Path(tmp) / "alias.png"
            try:
                output.symlink_to(target)
            except OSError as error:
                self.skipTest(f"当前文件系统不支持符号链接: {error}")
            with mock.patch.object(gen, "_request_once") as request:
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=str(output),
                    max_retries=0,
                )
            self.assertFalse(result["success"])
            self.assertIn("符号链接", result["error"])
            request.assert_not_called()

    def test_invalid_generation_base_url_returns_task_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="http://example.com:abc",
                model="fake-model",
                output_path=str(Path(tmp) / "image.png"),
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("URL", result["error"])

    def test_direct_core_call_rejects_blank_prompt_and_invalid_aspect_ratio(self):
        invalid_requests = (
            {"prompt": "   ", "aspect_ratio": "1:1", "error": "prompt"},
            {"prompt": "test", "aspect_ratio": "not-a-ratio", "error": "aspect_ratio"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            for request_data in invalid_requests:
                with self.subTest(request_data=request_data):
                    with mock.patch.object(gen, "_request_once") as request:
                        result = gen._generate_core(
                            prompt=request_data["prompt"],
                            api_key="fake",
                            base_url="https://example.invalid/v1",
                            model="fake-model",
                            aspect_ratio=request_data["aspect_ratio"],
                            output_path=str(Path(tmp) / "image.png"),
                            max_retries=0,
                        )
                    self.assertFalse(result["success"])
                    self.assertIn(request_data["error"], result["error"])
                    request.assert_not_called()

    def test_direct_core_call_accepts_pathlike_and_rejects_other_output_types(self):
        image = _valid_png()
        response = _FakeResponse({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            with mock.patch.object(gen, "_request_once", return_value=response):
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=output,
                    max_retries=0,
                )
            self.assertTrue(result["success"], result)
            self.assertEqual(output.read_bytes(), image)

            for invalid in (42, b"image.png"):
                with self.subTest(output_path=invalid):
                    with mock.patch.object(gen, "_request_once") as request:
                        result = gen._generate_core(
                            prompt="test",
                            api_key="fake",
                            base_url="https://example.invalid/v1",
                            model="fake-model",
                            output_path=invalid,
                            max_retries=0,
                        )
                    self.assertFalse(result["success"])
                    self.assertIn("output", result["error"])
                    request.assert_not_called()

    def test_invalid_download_url_returns_task_failure(self):
        response = _FakeResponse({"data": [{"url": "http://example.com:abc/image.png"}]})
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gen, "_request_once", return_value=response):
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=str(Path(tmp) / "image.png"),
                    max_retries=0,
                )
        self.assertFalse(result["success"])
        self.assertIn("URL", result["error"])

    def test_invalid_download_url_does_not_abort_batch(self):
        valid = _valid_png()
        responses = [
            _FakeResponse({"data": [{"url": "http://example.com:abc/image.png"}]}),
            _FakeResponse({"data": [{"b64_json": base64.b64encode(valid).decode("ascii")}]}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [
                {"prompt": "bad", "output": str(Path(tmp) / "bad.png")},
                {"prompt": "good", "output": str(Path(tmp) / "good.png")},
            ]
            with mock.patch.object(gen, "_request_once", side_effect=responses):
                results = gen.generate_batch(
                    tasks,
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    workers=1,
                    max_retries=0,
                )
        self.assertFalse(results[0]["success"])
        self.assertTrue(results[1]["success"])

    def test_batch_expands_home_in_output_before_preflight_and_write(self):
        image = _valid_png()
        response = _FakeResponse({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            cwd = root / "cwd"
            home.mkdir()
            cwd.mkdir()
            old_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                with mock.patch.dict(os.environ, {"HOME": str(home)}):
                    with mock.patch.object(gen, "_request_once", return_value=response):
                        results = gen.generate_batch(
                            [{"prompt": "test", "output": "~/image.png"}],
                            api_key="fake",
                            base_url="https://example.invalid/v1",
                            model="fake-model",
                            workers=1,
                            max_retries=0,
                        )
            finally:
                os.chdir(old_cwd)
            self.assertTrue(results[0]["success"], results)
            self.assertEqual(Path(results[0]["path"]), home / "image.png")
            self.assertEqual((home / "image.png").read_bytes(), image)
            self.assertFalse((cwd / "~" / "image.png").exists())

    def test_valid_base64_response_writes_the_requested_file(self):
        image = _valid_png(1536, 1024)
        response = _FakeResponse({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "image.png"
            reference = Path(tmp) / "reference.png"
            reference.write_bytes(b"reference")
            expected_mode = stat.S_IMODE(reference.stat().st_mode)
            with mock.patch.object(gen, "_request_once", return_value=response):
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    aspect_ratio="16:9",
                    output_path=str(output),
                    max_retries=0,
                )
            self.assertTrue(result["success"], result)
            self.assertEqual(output.read_bytes(), image)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), expected_mode)

    def test_mismatched_png_dimensions_do_not_overwrite_existing_file(self):
        image = _valid_png(1024, 1024)
        response = _FakeResponse({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            old_contents = b"existing-image-must-survive"
            output.write_bytes(old_contents)
            with mock.patch.object(gen, "_request_once", return_value=response):
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    aspect_ratio="16:9",
                    output_path=str(output),
                    max_retries=0,
                )

            self.assertFalse(result["success"])
            self.assertIn("PNG 尺寸", result["error"])
            self.assertIn("1536x1024", result["error"])
            self.assertIn("1024x1024", result["error"])
            self.assertEqual(output.read_bytes(), old_contents)
            self.assertEqual(list(Path(tmp).glob(".heige-image-*.tmp")), [])

    def test_atomic_overwrite_preserves_existing_file_mode(self):
        image = _valid_png()
        response = _FakeResponse({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            output.write_bytes(b"old")
            output.chmod(0o640)
            with mock.patch.object(gen, "_request_once", return_value=response):
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=str(output),
                    max_retries=0,
                )
            self.assertTrue(result["success"], result)
            self.assertEqual(output.read_bytes(), image)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)

    def test_atomic_write_supports_a_near_name_max_output(self):
        image = _valid_png()
        response = _FakeResponse({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / ("a" * 246 + ".png")
            with mock.patch.object(gen, "_request_once", return_value=response):
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=str(output),
                    max_retries=0,
                )
            self.assertTrue(result["success"], result)
            self.assertEqual(output.read_bytes(), image)

    def test_name_too_long_is_rejected_before_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                name_max = os.pathconf(tmp, "PC_NAME_MAX")
            except (AttributeError, OSError, ValueError):
                self.skipTest("current platform does not expose PC_NAME_MAX")
            if name_max <= 0:
                self.skipTest("current platform reports no finite PC_NAME_MAX")
            output = Path(tmp) / ("a" * (name_max - 3) + ".png")
            with mock.patch.object(gen, "_request_once") as request:
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    output_path=str(output),
                    max_retries=0,
                )
            self.assertFalse(result["success"])
            self.assertIn("文件名过长", result["error"])
            request.assert_not_called()

    def test_failed_atomic_write_preserves_existing_file_and_removes_temp_file(self):
        image = _valid_png()
        response = _FakeResponse({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        })
        original_fdopen = os.fdopen

        class _PartialWriter:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.handle.close()
                return False

            def write(self, payload):
                self.handle.write(payload[:10])
                self.handle.flush()
                raise OSError("simulated disk full")

            def flush(self):
                self.handle.flush()

            def fileno(self):
                return self.handle.fileno()

        def _failing_fdopen(fd, mode):
            return _PartialWriter(original_fdopen(fd, mode))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            old_contents = b"VALID-OLD-CONTENT"
            output.write_bytes(old_contents)
            with mock.patch.object(gen, "_request_once", return_value=response):
                with mock.patch.object(gen.os, "fdopen", side_effect=_failing_fdopen):
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        output_path=str(output),
                        max_retries=0,
                    )
            self.assertFalse(result["success"])
            self.assertIn("写入图片失败", result["error"])
            self.assertEqual(output.read_bytes(), old_contents)
            self.assertEqual(list(Path(tmp).glob(".heige-image-*.tmp")), [])

    def test_base64_html_is_rejected_instead_of_being_saved_as_png(self):
        payload = base64.b64encode(b"<html>upstream error</html>").decode("ascii")
        response = _FakeResponse({"data": [{"b64_json": payload}]})
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("PNG", result["error"])

    def test_truncated_png_is_rejected(self):
        payload = base64.b64encode(_valid_png()[:-5]).decode("ascii")
        response = _FakeResponse({"data": [{"b64_json": payload}]})
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("PNG", result["error"])

    def test_png_with_truncated_zlib_stream_is_rejected(self):
        compressed = zlib.compress(b"\x00\xff\x00\x00\xff")[:-2]
        payload = base64.b64encode(
            _png_with_compressed_pixels(compressed)
        ).decode("ascii")
        response = _FakeResponse({"data": [{"b64_json": payload}]})
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("PNG", result["error"])

    def test_png_allows_unused_trailing_bytes_in_final_idat(self):
        compressed = zlib.compress(b"\x00\xff\x00\x00\xff") + b"\x00unused"
        gen._validate_png(_png_with_compressed_pixels(compressed))

    def test_png_with_bad_chunk_crc_is_rejected(self):
        image = bytearray(_valid_png())
        idat_offset = image.index(b"IDAT")
        image[idat_offset + 4] ^= 0x01
        payload = base64.b64encode(bytes(image)).decode("ascii")
        response = _FakeResponse({"data": [{"b64_json": payload}]})
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("CRC", result["error"])

    def test_base64_size_is_bounded_before_decode(self):
        payload = base64.b64encode(_valid_png()).decode("ascii")
        response = _FakeResponse({"data": [{"b64_json": payload}]})
        with mock.patch.object(gen, "MAX_IMAGE_BYTES", 16, create=True):
            with mock.patch.object(gen, "_request_once", return_value=response):
                result = gen._generate_core(
                    prompt="test",
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    max_retries=0,
                )
        self.assertFalse(result["success"])
        self.assertIn("上限", result["error"])

    def test_malformed_success_json_returns_failure_instead_of_traceback(self):
        response = _FakeResponse(json_error=ValueError("not json"))
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("JSON", result["error"])

    def test_deeply_nested_success_json_returns_failure_instead_of_traceback(self):
        response = httpx.Response(
            200,
            content=b"[" * 2000 + b"0" + b"]" * 2000,
            request=httpx.Request(
                "POST",
                "https://example.invalid/v1/images/generations",
            ),
        )
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("JSON", result["error"])

    def test_invalid_success_payload_error_is_bounded_and_omits_bulk_base64(self):
        bulk = "A" * (1024 * 1024)
        response = _FakeResponse({
            "data": {"b64_json": bulk, "junk": bulk},
        })
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertLess(len(result["error"]), 1000)
        self.assertNotIn("A" * 100, result["error"])

    def test_retryable_non_object_json_does_not_abort_the_task(self):
        valid = _valid_png()
        responses = [
            _FakeResponse([], status_code=500, text="temporary"),
            _FakeResponse({
                "data": [{"b64_json": base64.b64encode(valid).decode("ascii")}],
            }),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gen, "_request_once", side_effect=responses):
                with mock.patch.object(gen.time, "sleep"):
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        output_path=str(Path(tmp) / "image.png"),
                        max_retries=1,
                    )
        self.assertTrue(result["success"], result)

    def test_default_does_not_retry_non_idempotent_generation_requests(self):
        request = httpx.Request(
            "POST",
            "https://example.invalid/v1/images/generations",
        )
        timeout = httpx.ReadTimeout("response timed out", request=request)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gen, "_request_once", side_effect=timeout) as call:
                with mock.patch.object(gen.time, "sleep") as sleep:
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        output_path=str(Path(tmp) / "image.png"),
                    )
        self.assertFalse(result["success"])
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    def test_download_transport_error_returns_failure_instead_of_escaping(self):
        response = _FakeResponse({"data": [{"url": "https://example.invalid/image.png"}]})
        request = httpx.Request("GET", "https://example.invalid/image.png")
        error = httpx.ConnectError("offline", request=request)
        with mock.patch.object(gen, "_request_once", return_value=response):
            with mock.patch.object(gen.httpx, "get", side_effect=error):
                with mock.patch.object(gen.httpx, "stream", side_effect=error):
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        max_retries=0,
                    )
        self.assertFalse(result["success"])
        self.assertIn("下载", result["error"])

    def test_signed_download_url_is_redacted_from_logs_and_errors(self):
        secret = "VERY-SECRET-SIGNATURE"
        signed_url = f"https://example.invalid/image.png?sig={secret}#fragment"
        response = _FakeResponse({"data": [{"url": signed_url}]})
        request = httpx.Request("GET", signed_url)
        error = httpx.ConnectError(f"failed for {signed_url}", request=request)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gen, "_request_once", return_value=response):
                with mock.patch.object(gen, "_download_image_bytes", side_effect=error):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        result = gen._generate_core(
                            prompt="test",
                            api_key="fake",
                            base_url="https://example.invalid/v1",
                            model="fake-model",
                            output_path=str(Path(tmp) / "image.png"),
                            max_retries=0,
                        )
        combined = stdout.getvalue() + stderr.getvalue() + result["error"]
        self.assertFalse(result["success"])
        self.assertNotIn(secret, combined)
        self.assertNotIn("?sig=", combined)
        self.assertIn("https://example.invalid/image.png", combined)
        summary = gen._bounded_json_summary({"data": {"url": signed_url}})
        self.assertNotIn(secret, summary)
        self.assertNotIn("?sig=", summary)

    def test_valid_streamed_png_response_writes_the_requested_file(self):
        image = _valid_png()
        api_response = _FakeResponse({
            "data": [{"url": "https://example.invalid/image.png"}],
        })
        download_response = _FakeDownloadResponse(
            [image[:17], image[17:]],
            headers={"content-type": "image/png", "content-length": str(len(image))},
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            with mock.patch.object(gen, "_request_once", return_value=api_response):
                with mock.patch.object(
                    gen.httpx,
                    "stream",
                    return_value=_FakeStreamContext(download_response),
                ):
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        output_path=str(output),
                        max_retries=0,
                    )
            self.assertTrue(result["success"], result)
            self.assertEqual(output.read_bytes(), image)

    def test_downloaded_html_is_rejected_instead_of_being_saved_as_png(self):
        api_response = _FakeResponse({
            "data": [{"url": "https://example.invalid/image.png"}],
        })
        download_response = _FakeDownloadResponse(
            [b"<html>upstream error</html>"],
            headers={"content-type": "text/html"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            with mock.patch.object(gen, "_request_once", return_value=api_response):
                with mock.patch.object(
                    gen.httpx,
                    "stream",
                    return_value=_FakeStreamContext(download_response),
                ):
                    result = gen._generate_core(
                        prompt="test",
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                        output_path=str(output),
                        max_retries=0,
                    )
            self.assertFalse(result["success"])
            self.assertIn("PNG", result["error"])
            self.assertFalse(output.exists())

    def test_png_with_malformed_fixed_metadata_is_rejected(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        for kind, payload in ((b"gAMA", b"\x00"), (b"pHYs", b"\x00" * 8)):
            image = (
                b"\x89PNG\r\n\x1a\n"
                + _png_chunk(b"IHDR", header)
                + _png_chunk(kind, payload)
                + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
                + _png_chunk(b"IEND", b"")
            )
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(gen.ImagePayloadError, kind.decode("ascii")):
                    gen._validate_png(image)

    def test_png_with_truncated_exif_is_rejected(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        image = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"eXIf", b"MM\x00*")
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(gen.ImagePayloadError, "eXIf"):
            gen._validate_png(image)

    def test_png_with_compressed_text_metadata_is_rejected(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        compressed_text = b"comment\x00\x00" + zlib.compress(b"x" * (5 * 1024 * 1024))
        image = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"zTXt", compressed_text)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(gen.ImagePayloadError, "zTXt"):
            gen._validate_png(image)

    def test_png_with_common_profile_and_text_metadata_remains_valid(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        icc = bytearray(128)
        struct.pack_into(">I", icc, 0, len(icc))
        icc[16:20] = b"RGB "
        icc[36:40] = b"acsp"
        itxt = b"Comment\x00\x01\x00en\x00Title\x00" + zlib.compress("你好".encode("utf-8"))
        ztxt = b"Source\x00\x00" + zlib.compress(b"generated")
        exif = b"MM\x00*\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00"
        cabx = struct.pack(">I4s", 8, b"jumb")
        image = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"iCCP", b"sRGB\x00\x00" + zlib.compress(bytes(icc)))
            + _png_chunk(b"sRGB", b"\x00")
            + _png_chunk(b"iTXt", itxt)
            + _png_chunk(b"zTXt", ztxt)
            + _png_chunk(b"eXIf", exif)
            + _png_chunk(b"caBX", cabx)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + _png_chunk(b"IEND", b"")
        )
        gen._validate_png(image)

    def test_png_with_unknown_private_ancillary_chunks_remains_valid_and_bounded(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        prefix = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
        )
        suffix = (
            _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + _png_chunk(b"IEND", b"")
        )
        image = prefix + _png_chunk(b"aaAa", b"vendor") * 2 + suffix
        gen._validate_png(image)

        with mock.patch.object(gen, "MAX_DECOMPRESSED_METADATA_BYTES", 8):
            with self.assertRaisesRegex(gen.ImagePayloadError, "aaAa"):
                gen._validate_png(image)

    def test_indexed_png_rejects_palette_indices_outside_plte_for_all_bit_depths(self):
        def _indexed_png(bit_depth, palette_index, *, interlace=0):
            header = struct.pack(
                ">IIBBBBB",
                1,
                1,
                bit_depth,
                3,
                0,
                0,
                interlace,
            )
            packed_index = palette_index << (8 - bit_depth)
            return (
                b"\x89PNG\r\n\x1a\n"
                + _png_chunk(b"IHDR", header)
                + _png_chunk(b"PLTE", b"\x00\x00\x00")
                + _png_chunk(b"IDAT", zlib.compress(bytes((0, packed_index))))
                + _png_chunk(b"IEND", b"")
            )

        for bit_depth in (1, 2, 4, 8):
            with self.subTest(bit_depth=bit_depth):
                gen._validate_png(_indexed_png(bit_depth, 0))
                with self.assertRaisesRegex(gen.ImagePayloadError, "PLTE"):
                    gen._validate_png(_indexed_png(bit_depth, 1))

        with self.assertRaisesRegex(gen.ImagePayloadError, "PLTE"):
            gen._validate_png(_indexed_png(8, 1, interlace=1))

    def test_indexed_png_checks_palette_indices_after_unfiltering(self):
        header = struct.pack(">IIBBBBB", 1, 2, 8, 3, 0, 0, 0)
        # 第一行是索引 1；第二行用 Up filter，1 + above(1) = 2。
        # 压缩字节本身看似不越界，反滤后才能发现非法索引 2。
        filtered_rows = b"\x00\x01\x02\x01"
        image = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"PLTE", b"\x00\x00\x00\xff\xff\xff")
            + _png_chunk(b"IDAT", zlib.compress(filtered_rows))
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(gen.ImagePayloadError, "PLTE"):
            gen._validate_png(image)

    def test_iccp_color_space_must_match_png_color_type(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
        icc = bytearray(128)
        struct.pack_into(">I", icc, 0, len(icc))
        icc[16:20] = b"RGB "
        icc[36:40] = b"acsp"
        image = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"iCCP", b"sRGB\x00\x00" + zlib.compress(bytes(icc)))
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff"))
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(gen.ImagePayloadError, "色彩空间"):
            gen._validate_png(image)

    def test_png_with_common_fixed_metadata_remains_valid(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        image = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"gAMA", struct.pack(">I", 45455))
            + _png_chunk(b"sRGB", b"\x00")
            + _png_chunk(b"pHYs", struct.pack(">IIB", 3780, 3780, 1))
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + _png_chunk(b"IEND", b"")
        )
        gen._validate_png(image)

    def test_png3_hdr_and_suggested_palette_chunks_remain_valid(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        splt = b"Suggested\x00\x08" + b"\xff\x00\x00\xff\x00\x01"
        image = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"cICP", b"\x01\x0d\x00\x01")
            + _png_chunk(b"mDCV", b"\x00" * 24)
            + _png_chunk(b"cLLI", b"\x00" * 8)
            + _png_chunk(b"sPLT", splt)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + _png_chunk(b"IEND", b"")
        )
        gen._validate_png(image)

    def test_png3_metadata_rejects_bad_lengths_flags_and_duplicate_palette_names(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        idat = _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
        invalid_chunks = (
            (b"cICP", b"\x01\x0d\x00\x02"),
            (b"mDCV", b"\x00" * 23),
            (b"cLLI", b"\x00" * 7),
            (b"sPLT", b"Palette\x00\x08\x00"),
        )
        for kind, payload in invalid_chunks:
            image = (
                b"\x89PNG\r\n\x1a\n"
                + _png_chunk(b"IHDR", header)
                + _png_chunk(kind, payload)
                + idat
                + _png_chunk(b"IEND", b"")
            )
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(gen.ImagePayloadError, kind.decode("ascii")):
                    gen._validate_png(image)

        splt = b"Palette\x00\x08" + b"\xff\x00\x00\xff\x00\x01"
        duplicated = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"sPLT", splt)
            + _png_chunk(b"sPLT", splt)
            + idat
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(gen.ImagePayloadError, "sPLT"):
            gen._validate_png(duplicated)

    def test_cabx_allows_post_idat_and_standard_jumbf_box_sizes(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        pixels = _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
        for cabx in (
            struct.pack(">I4s", 0, b"jumb"),
            struct.pack(">I4sQ", 1, b"jumb", 16),
        ):
            image = (
                b"\x89PNG\r\n\x1a\n"
                + _png_chunk(b"IHDR", header)
                + pixels
                + _png_chunk(b"caBX", cabx)
                + _png_chunk(b"IEND", b"")
            )
            with self.subTest(lbox=struct.unpack(">I", cabx[:4])[0]):
                gen._validate_png(image)

    def test_invalid_base64_returns_failure_instead_of_empty_file(self):
        response = _FakeResponse({"data": [{"b64_json": "not-valid-base64%%%"}]})
        with mock.patch.object(gen, "_request_once", return_value=response):
            result = gen._generate_core(
                prompt="test",
                api_key="fake",
                base_url="https://example.invalid/v1",
                model="fake-model",
                max_retries=0,
            )
        self.assertFalse(result["success"])
        self.assertIn("base64", result["error"])

    def test_download_rejects_declared_oversize_before_reading_body(self):
        response = _FakeDownloadResponse(
            [_valid_png()],
            headers={"content-length": "999"},
        )
        with mock.patch.object(gen, "MAX_IMAGE_BYTES", 16, create=True):
            with mock.patch.object(
                gen.httpx,
                "stream",
                return_value=_FakeStreamContext(response),
            ):
                with self.assertRaisesRegex(ValueError, "上限"):
                    gen._download_image_bytes("https://example.invalid/image.png")
        self.assertFalse(response.iterated)

    def test_download_rejects_actual_oversize_without_content_length(self):
        response = _FakeDownloadResponse([b"12345678", b"901234567"])
        with mock.patch.object(gen, "MAX_IMAGE_BYTES", 16, create=True):
            with mock.patch.object(
                gen.httpx,
                "stream",
                return_value=_FakeStreamContext(response),
            ):
                with self.assertRaisesRegex(ValueError, "上限"):
                    gen._download_image_bytes("https://example.invalid/image.png")


class SpecValidationTests(unittest.TestCase):
    def test_final_prompt_does_not_borrow_a_fence_from_the_next_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.md"
            spec.write_text(
                "## 最终 Prompt\n\n这里漏了代码块\n\n"
                "## Invariant\n\n```text\nWRONG INVARIANT BLOCK\n```\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                gen.parse_spec(str(spec))

    def test_markdown_heading_inside_prompt_fence_remains_part_of_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.md"
            spec.write_text(
                "## 最终 Prompt\n\n```text\n"
                "## Composition\n\nA structured image prompt.\n```\n"
                "\n## Invariant\n",
                encoding="utf-8",
            )
            parsed = gen.parse_spec(str(spec))
            self.assertIn("## Composition", parsed["prompt"])

    def test_unsupported_spec_aspect_ratio_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.md"
            spec.write_text(
                "比例: 7:5\n\n## 最终 Prompt\n\n```text\nvalid prompt\n```\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                gen.parse_spec(str(spec))

    def test_spec_directory_and_invalid_utf8_fail_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                gen.parse_spec(tmp)
            spec = Path(tmp) / "spec.md"
            spec.write_bytes(b"\xff\xfe")
            with self.assertRaises(SystemExit):
                gen.parse_spec(str(spec))


class ConfigValidationTests(unittest.TestCase):
    def test_non_object_config_is_ignored_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text("[]", encoding="utf-8")
            with mock.patch.object(gen, "CONFIG_FILE", config_file):
                with mock.patch.object(gen, "_safe_print") as warning:
                    self.assertEqual(gen._load_config(), {})
            warning.assert_called_once()
            self.assertIn("必须是 JSON 对象", warning.call_args.args[0])

    def test_invalid_utf8_config_is_ignored_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_bytes(b"{\xff}")
            with mock.patch.object(gen, "CONFIG_FILE", config_file):
                self.assertEqual(gen._load_config(), {})

    def test_non_string_config_fields_are_ignored_without_traceback(self):
        config = {"base_url": 42, "api_key": [], "model": {}}
        self.assertEqual(gen.resolve_base_url(config=config), gen.DEFAULT_BASE_URL)
        self.assertEqual(gen.resolve_model(config=config), gen.DEFAULT_MODEL)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                gen.resolve_api_key(config=config)


class BatchValidationTests(unittest.TestCase):
    def test_direct_batch_call_rejects_invalid_schema_before_requests(self):
        invalid_batches = (
            ([], "非空"),
            ([42], "对象"),
            ([{"prompt": "   ", "output": "out.png"}], "prompt"),
            ([{"prompt": "test", "output": "out.jpg"}], "PNG"),
            ([{
                "prompt": "test",
                "output": "out.png",
                "aspect_ratio": "not-a-ratio",
            }], "aspect_ratio"),
        )
        for tasks, message in invalid_batches:
            with self.subTest(tasks=tasks):
                with mock.patch.object(gen, "_request_once") as request:
                    with self.assertRaisesRegex(ValueError, message):
                        gen.generate_batch(
                            tasks,
                            api_key="fake",
                            base_url="https://example.invalid/v1",
                            model="fake-model",
                        )
                request.assert_not_called()

    def test_direct_batch_call_rejects_duplicate_outputs_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = [
                {"prompt": "one", "output": str(root / "x.png")},
                {"prompt": "two", "output": str(root / "sub" / ".." / "x.png")},
            ]
            with mock.patch.object(gen, "_generate_core") as generate_core:
                with self.assertRaisesRegex(ValueError, "重复 output"):
                    gen.generate_batch(
                        tasks,
                        api_key="fake",
                        base_url="https://example.invalid/v1",
                        model="fake-model",
                    )
        generate_core.assert_not_called()

    def test_direct_batch_call_enforces_cost_limit_before_requests(self):
        tasks = [
            {"prompt": f"task {index}", "output": f"{index}.png"}
            for index in range(3)
        ]
        with mock.patch.object(gen, "_request_once") as request:
            with self.assertRaisesRegex(ValueError, "max_n"):
                gen.generate_batch(
                    tasks,
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    max_n=2,
                )
        request.assert_not_called()

    def test_unexpected_task_exception_is_isolated_in_batch_results(self):
        valid = _valid_png()
        responses = [
            RuntimeError("simulated provider bug"),
            _FakeResponse({
                "data": [{"b64_json": base64.b64encode(valid).decode("ascii")}],
            }),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [
                {"prompt": "bad", "output": str(Path(tmp) / "bad.png")},
                {"prompt": "good", "output": str(Path(tmp) / "good.png")},
            ]
            with mock.patch.object(gen, "_request_once", side_effect=responses):
                results = gen.generate_batch(
                    tasks,
                    api_key="fake",
                    base_url="https://example.invalid/v1",
                    model="fake-model",
                    workers=1,
                    max_retries=0,
                )
        self.assertFalse(results[0]["success"])
        self.assertIn("RuntimeError", results[0]["error"])
        self.assertTrue(results[1]["success"], results)

    def test_non_object_batch_item_is_rejected_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "tasks.json"
            batch.write_text(json.dumps([42]), encoding="utf-8")
            env = os.environ.copy()
            env["HEIGE_IMAGE_API_KEY"] = "fake"
            proc = subprocess.run(
                [sys.executable, str(GEN), "--batch", str(batch), "--retry", "0"],
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("对象", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_batch_directory_and_invalid_output_destination_fail_without_traceback(self):
        env = os.environ.copy()
        env["HEIGE_IMAGE_API_KEY"] = "fake"
        with tempfile.TemporaryDirectory() as tmp:
            directory_proc = subprocess.run(
                [sys.executable, str(GEN), "--batch", tmp, "--retry", "0"],
                capture_output=True,
                text=True,
                env=env,
            )
            parent = Path(tmp) / "not-a-directory"
            parent.write_text("x", encoding="utf-8")
            batch = Path(tmp) / "tasks.json"
            batch.write_text(
                json.dumps([{
                    "prompt": "test",
                    "output": str(parent / "image.png"),
                }]),
                encoding="utf-8",
            )
            output_proc = subprocess.run(
                [sys.executable, str(GEN), "--batch", str(batch), "--retry", "0"],
                capture_output=True,
                text=True,
                env=env,
            )
        for proc in (directory_proc, output_proc):
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("Traceback", proc.stderr)

    def test_empty_prompt_is_rejected_before_any_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "tasks.json"
            batch.write_text(
                json.dumps([{"prompt": "   ", "output": "out.png"}]),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HEIGE_IMAGE_API_KEY"] = "fake"
            proc = subprocess.run(
                [sys.executable, str(GEN), "--batch", str(batch), "--retry", "0"],
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("prompt", proc.stderr)
        self.assertNotIn("正在生成图片", proc.stdout)

    def test_non_png_output_is_rejected_before_any_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "tasks.json"
            batch.write_text(
                json.dumps([{"prompt": "test", "output": "out.jpg"}]),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(GEN),
                    "--batch",
                    str(batch),
                    "--api-key",
                    "fake",
                    "--base-url",
                    "http://127.0.0.1:1",
                    "--retry",
                    "0",
                ],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PNG", proc.stderr)
        self.assertNotIn("正在生成图片", proc.stdout)

    def test_normalized_duplicate_outputs_are_rejected_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "x.png"
            alias = root / "sub" / ".." / "x.png"
            batch = root / "tasks.json"
            batch.write_text(
                json.dumps([
                    {"prompt": "one", "output": str(first)},
                    {"prompt": "two", "output": str(alias)},
                ]),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(GEN),
                    "--batch",
                    str(batch),
                    "--api-key",
                    "fake",
                    "--base-url",
                    "http://127.0.0.1:1",
                    "--retry",
                    "0",
                ],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("重复 output", proc.stderr)
        self.assertNotIn("正在生成图片", proc.stdout)

    def test_symlink_parent_alias_outputs_are_rejected_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            alias = root / "alias"
            real.mkdir()
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"当前平台不能创建目录符号链接: {error}")
            batch = root / "tasks.json"
            batch.write_text(
                json.dumps([
                    {"prompt": "one", "output": str(real / "x.png")},
                    {"prompt": "two", "output": str(alias / "x.png")},
                ]),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(GEN),
                    "--batch",
                    str(batch),
                    "--api-key",
                    "fake",
                    "--base-url",
                    "http://127.0.0.1:1",
                    "--retry",
                    "0",
                ],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("重复 output", proc.stderr)
        self.assertNotIn("正在生成图片", proc.stdout)

    def test_darwin_case_alias_outputs_are_rejected_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [
                {"prompt": "one", "output": str(Path(tmp) / "Case.png")},
                {"prompt": "two", "output": str(Path(tmp) / "case.png")},
            ]
            with mock.patch.object(gen.sys, "platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "重复 output"):
                    gen._validate_unique_batch_outputs(tasks)

    def test_darwin_unicode_equivalent_outputs_are_rejected_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [
                {"prompt": "one", "output": str(Path(tmp) / "café.png")},
                {"prompt": "two", "output": str(Path(tmp) / "cafe\u0301.png")},
            ]
            with mock.patch.object(gen.sys, "platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "重复 output"):
                    gen._validate_unique_batch_outputs(tasks)

    def test_existing_hardlink_outputs_are_rejected_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.png"
            alias = Path(tmp) / "alias.png"
            first.write_bytes(_valid_png())
            try:
                os.link(first, alias)
            except OSError as error:
                self.skipTest(f"当前文件系统不支持硬链接: {error}")
            self.assertTrue(first.samefile(alias))
            with self.assertRaisesRegex(ValueError, "重复 output"):
                gen._validate_unique_batch_outputs([
                    {"prompt": "one", "output": str(first)},
                    {"prompt": "two", "output": str(alias)},
                ])

    def test_home_alias_hardlink_outputs_are_rejected_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            first = home / "first.png"
            alias = home / "alias.png"
            first.write_bytes(_valid_png())
            try:
                os.link(first, alias)
            except OSError as error:
                self.skipTest(f"当前文件系统不支持硬链接: {error}")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(ValueError, "重复 output"):
                    gen._validate_unique_batch_outputs([
                        {"prompt": "one", "output": "~/first.png"},
                        {"prompt": "two", "output": str(alias)},
                    ])


if __name__ == "__main__":
    unittest.main()
