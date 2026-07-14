#!/usr/bin/env python3
"""Regression tests for the documented HTML rendering path."""

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
RENDER = ROOT / "scripts" / "render.py"
INFOGRAPHIC = ROOT / "templates" / "infographic-clean.html"


def _png_header(width=1, height=1):
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def _png_chunk(chunk_type, data):
    crc = zlib.crc32(data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
    return (
        len(data).to_bytes(4, "big")
        + chunk_type
        + data
        + crc.to_bytes(4, "big")
    )


def _valid_png(width=1, height=1):
    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((8, 6, 0, 0, 0))
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )

_RENDER_SPEC = importlib.util.spec_from_file_location("heige_image_render", RENDER)
render_module = importlib.util.module_from_spec(_RENDER_SPEC)
_RENDER_SPEC.loader.exec_module(render_module)


class _FakeElement:
    def __init__(self, node_id="poster", screenshot=None):
        self.node_id = node_id
        self._screenshot = screenshot or self._write_screenshot
        self.screenshot_calls = 0

    @staticmethod
    def _write_screenshot(path, **_kwargs):
        Path(path).write_bytes(_valid_png())

    def get_attribute(self, name):
        return self.node_id if name == "id" else None

    def screenshot(self, path, **kwargs):
        self.screenshot_calls += 1
        return self._screenshot(path, **kwargs)


class _FakePage:
    def __init__(self, *, selected=None, posters=None, screenshot=None):
        self.selected = selected
        self.posters = list(posters or [])
        self._screenshot = screenshot or _FakeElement._write_screenshot

    def goto(self, *_args, **_kwargs):
        return None

    def evaluate(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, _milliseconds):
        return None

    def query_selector(self, _selector):
        return self.selected

    def query_selector_all(self, _selector):
        return self.posters

    def screenshot(self, path, **kwargs):
        return self._screenshot(path, **kwargs)


class _FakeBrowser:
    def __init__(self, page):
        self.page = page

    def new_page(self, **_kwargs):
        return self.page

    def close(self):
        return None


class _FakePlaywrightContext:
    def __init__(self, page):
        browser = _FakeBrowser(page)
        self.playwright = type("FakePlaywright", (), {})()
        self.playwright.chromium = type("FakeChromium", (), {
            "launch": staticmethod(lambda **_kwargs: browser),
        })()
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self.playwright

    def __exit__(self, *_args):
        return False


def _patch_playwright(page):
    context = _FakePlaywrightContext(page)
    return context, mock.patch.object(
        render_module,
        "sync_playwright",
        return_value=context,
    )


class RenderCliTests(unittest.TestCase):
    def test_trailing_slash_output_is_a_directory_for_single_poster(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "rendered"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RENDER),
                    str(INFOGRAPHIC),
                    "--settle-ms",
                    "0",
                    "-o",
                    str(output_dir) + os.sep,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            output = output_dir / "infographic.png"
            self.assertTrue(output.is_file(), proc.stdout + proc.stderr)
            self.assertGreater(output.stat().st_size, 0)

    def test_node_and_full_page_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.png"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RENDER),
                    str(INFOGRAPHIC),
                    "--node",
                    "#infographic",
                    "--full-page",
                    "--settle-ms",
                    "0",
                    "-o",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("--node", proc.stderr)
            self.assertIn("--full-page", proc.stderr)
            self.assertFalse(output.exists())

    def test_negative_settle_time_is_rejected_by_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.png"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RENDER),
                    str(INFOGRAPHIC),
                    "--settle-ms",
                    "-1",
                    "-o",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("非负", proc.stderr)
            self.assertFalse(output.exists())

    def test_output_below_regular_file_fails_cleanly_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent_file = Path(tmp) / "parent"
            parent_file.write_text("not a directory", encoding="utf-8")
            output = parent_file / "output.png"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RENDER),
                    str(INFOGRAPHIC),
                    "--settle-ms",
                    "0",
                    "-o",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("父路径不是目录", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertFalse(output.exists())

    def test_no_poster_fallback_honors_trailing_slash_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "plain.html"
            html.write_text("<html><body>plain page</body></html>", encoding="utf-8")
            output_dir = Path(tmp) / "rendered"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RENDER),
                    str(html),
                    "--scale",
                    "1",
                    "--settle-ms",
                    "0",
                    "-o",
                    str(output_dir) + os.sep,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            output = output_dir / "plain.png"
            self.assertTrue(output.is_file(), proc.stdout + proc.stderr)
            self.assertEqual(render_module._png_size(output), (1920, 1080))


class RenderValidationTests(unittest.TestCase):
    def test_html_input_must_be_a_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = _FakePage()
            context, patch = _patch_playwright(page)
            with (
                patch,
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    tmp,
                    output_path=str(Path(tmp) / "output.png"),
                    full_page=True,
                    settle_ms=0,
                )
            self.assertFalse(context.entered)

    def test_negative_settle_time_is_rejected_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            page = _FakePage()
            context, patch = _patch_playwright(page)
            with (
                patch,
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    str(html),
                    output_path=str(Path(tmp) / "output.png"),
                    full_page=True,
                    settle_ms=-1,
                )
            self.assertFalse(context.entered)

    def test_direct_render_rejects_invalid_scale_before_browser_launch(self):
        invalid_scales = (0, 4.1, True, "2", None)
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            for scale in invalid_scales:
                with self.subTest(scale=scale):
                    page = _FakePage()
                    context, patch = _patch_playwright(page)
                    with (
                        patch,
                        redirect_stderr(io.StringIO()),
                        redirect_stdout(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        render_module.render(
                            str(html),
                            output_path=str(Path(tmp) / "output.png"),
                            full_page=True,
                            scale=scale,
                            settle_ms=0,
                        )
                    self.assertFalse(context.entered)

    def test_direct_render_accepts_fractional_scale_in_range(self):
        def write_png(path, **_kwargs):
            Path(path).write_bytes(_valid_png())

        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            output = Path(tmp) / "output.png"
            page = _FakePage(screenshot=write_png)
            _context, patch = _patch_playwright(page)
            with patch, redirect_stdout(io.StringIO()):
                paths = render_module.render(
                    str(html),
                    output_path=str(output),
                    full_page=True,
                    scale=1.5,
                    settle_ms=0,
                )
            self.assertEqual(paths, [str(output)])
            self.assertEqual(render_module._png_size(output), (1, 1))

    def test_direct_render_accepts_pathlike_input_and_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            output_dir = Path(tmp) / "rendered"
            output_dir.mkdir()
            element = _FakeElement("poster")
            page = _FakePage(posters=[element])
            context, patch = _patch_playwright(page)

            with patch, redirect_stdout(io.StringIO()):
                paths = render_module.render(
                    html,
                    output_path=output_dir,
                    settle_ms=0,
                )

            output = output_dir / "poster.png"
            self.assertTrue(context.entered)
            self.assertEqual(paths, [str(output)])
            self.assertEqual(render_module._png_size(output), (1, 1))

    def test_direct_render_rejects_parent_file_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            parent_file = Path(tmp) / "parent"
            parent_file.write_text("not a directory", encoding="utf-8")
            page = _FakePage(posters=[_FakeElement()])
            context, patch = _patch_playwright(page)
            stderr = io.StringIO()

            with (
                patch,
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    html,
                    output_path=parent_file / "output.png",
                    settle_ms=0,
                )

            self.assertFalse(context.entered)
            self.assertIn("父路径不是目录", stderr.getvalue())

    def test_direct_render_rejects_bytes_paths_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            page = _FakePage()
            context, patch = _patch_playwright(page)
            stderr = io.StringIO()

            with (
                patch,
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    os.fsencode(html),
                    output_path=Path(tmp) / "output.png",
                    full_page=True,
                    settle_ms=0,
                )

            self.assertFalse(context.entered)
            self.assertIn("文本 PathLike", stderr.getvalue())

    def test_direct_render_rejects_node_and_full_page_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            page = _FakePage()
            context, patch = _patch_playwright(page)
            with (
                patch,
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    str(html),
                    output_path=str(Path(tmp) / "output.png"),
                    node_selector="#poster",
                    full_page=True,
                    settle_ms=0,
                )
            self.assertFalse(context.entered)

    def test_file_outputs_must_use_png_extension(self):
        cases = (
            {"node_selector": "#poster", "page": _FakePage(selected=_FakeElement())},
            {"full_page": True, "page": _FakePage()},
            {"page": _FakePage(posters=[_FakeElement()])},
            {"page": _FakePage(posters=[])},
        )
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            for index, case in enumerate(cases):
                with self.subTest(index=index):
                    context, patch = _patch_playwright(case["page"])
                    kwargs = {key: value for key, value in case.items() if key != "page"}
                    with (
                        patch,
                        redirect_stderr(io.StringIO()),
                        redirect_stdout(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        render_module.render(
                            str(html),
                            output_path=str(Path(tmp) / f"output-{index}.jpg"),
                            settle_ms=0,
                            **kwargs,
                        )
                    self.assertEqual(context.entered, index >= 2)


class RenderOutputSafetyTests(unittest.TestCase):
    def test_multi_poster_output_cannot_be_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            output = Path(tmp) / "existing.png"
            output.write_bytes(b"old-output")
            first = _FakeElement("first")
            second = _FakeElement("second")
            page = _FakePage(posters=[first, second])
            context, patch = _patch_playwright(page)
            stderr = io.StringIO()

            with (
                patch,
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    str(html),
                    output_path=str(output),
                    settle_ms=0,
                )

            self.assertTrue(context.entered)
            self.assertEqual(first.screenshot_calls, 0)
            self.assertEqual(second.screenshot_calls, 0)
            self.assertEqual(output.read_bytes(), b"old-output")
            self.assertIn("多节点", stderr.getvalue())
            self.assertIn("普通文件", stderr.getvalue())

    def test_existing_output_symlink_is_rejected_without_changing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            target = Path(tmp) / "target.png"
            target.write_bytes(b"old-target")
            output = Path(tmp) / "output.png"
            try:
                output.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlink unsupported: {error}")

            element = _FakeElement()
            context, patch = _patch_playwright(_FakePage(selected=element))
            with (
                patch,
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    str(html),
                    output_path=str(output),
                    node_selector="#poster",
                    settle_ms=0,
                )
            self.assertEqual(target.read_bytes(), b"old-target")
            self.assertEqual(element.screenshot_calls, 0)
            self.assertFalse(context.entered)

    def test_generated_slug_symlink_is_rejected_without_changing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            output_dir = Path(tmp) / "output"
            output_dir.mkdir()
            target = Path(tmp) / "target.png"
            target.write_bytes(b"old-target")
            output = output_dir / "poster.png"
            try:
                output.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlink unsupported: {error}")

            element = _FakeElement("poster")
            context, patch = _patch_playwright(_FakePage(posters=[element]))
            with (
                patch,
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    str(html),
                    output_path=str(output_dir) + os.sep,
                    settle_ms=0,
                )
            self.assertEqual(target.read_bytes(), b"old-target")
            self.assertEqual(element.screenshot_calls, 0)
            self.assertTrue(context.entered)

    def test_colliding_slugs_fail_before_any_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            first = _FakeElement("same id")
            second = _FakeElement("same-id")
            page = _FakePage(posters=[first, second])
            _context, patch = _patch_playwright(page)
            stderr = io.StringIO()

            with (
                patch,
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                render_module.render(
                    str(html),
                    output_path=str(Path(tmp) / "output"),
                    settle_ms=0,
                )

            self.assertEqual(first.screenshot_calls, 0)
            self.assertEqual(second.screenshot_calls, 0)
            self.assertIn("same id", stderr.getvalue())
            self.assertIn("same-id", stderr.getvalue())

    def test_failed_screenshot_preserves_old_output_and_removes_temp_file(self):
        def fail_after_partial_write(path, **_kwargs):
            Path(path).write_bytes(b"partial")
            raise RuntimeError("simulated screenshot failure")

        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            output = Path(tmp) / "output.png"
            output.write_bytes(b"old-output")
            element = _FakeElement(screenshot=fail_after_partial_write)
            _context, patch = _patch_playwright(_FakePage(selected=element))

            with (
                patch,
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "simulated screenshot failure"),
            ):
                render_module.render(
                    str(html),
                    output_path=str(output),
                    node_selector="#poster",
                    settle_ms=0,
                )

            self.assertEqual(output.read_bytes(), b"old-output")
            self.assertEqual(list(Path(tmp).glob(".heige-render-*.png")), [])

    def test_successful_callback_with_invalid_png_preserves_old_output(self):
        corrupt_crc = bytearray(_valid_png())
        corrupt_crc[-1] ^= 1
        invalid_callbacks = (
            lambda _path, **_kwargs: None,
            lambda path, **_kwargs: Path(path).write_bytes(b"not-a-png"),
            lambda path, **_kwargs: Path(path).write_bytes(_png_header()),
            lambda path, **_kwargs: Path(path).write_bytes(_valid_png()[:-12]),
            lambda path, **_kwargs: Path(path).write_bytes(bytes(corrupt_crc)),
            lambda path, **_kwargs: Path(path).write_bytes(_valid_png(width=0)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "input.html"
            html.write_text("<html></html>", encoding="utf-8")
            for index, callback in enumerate(invalid_callbacks):
                with self.subTest(index=index):
                    output = Path(tmp) / f"output-{index}.png"
                    output.write_bytes(b"old-output")
                    element = _FakeElement(screenshot=callback)
                    _context, patch = _patch_playwright(_FakePage(selected=element))

                    with (
                        patch,
                        redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(OSError, "PNG"),
                    ):
                        render_module.render(
                            str(html),
                            output_path=str(output),
                            node_selector="#poster",
                            settle_ms=0,
                        )

                    self.assertEqual(output.read_bytes(), b"old-output")
                    self.assertEqual(list(Path(tmp).glob(".heige-render-*.png")), [])


class RenderDependencyDocumentationTests(unittest.TestCase):
    def test_playwright_install_commands_use_the_active_python_interpreter(self):
        expected = (
            "python3 -m pip install playwright && "
            "python3 -m playwright install chromium"
        )
        for path in (RENDER, ROOT / "README.md", ROOT / "SKILL.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(expected, text)
                self.assertNotIn(
                    "pip install playwright && playwright install chromium",
                    text,
                )

    def test_httpx_install_command_uses_the_active_python_interpreter(self):
        expected = "python3 -m pip install httpx"
        for path in (ROOT / "README.md", ROOT / "SKILL.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(expected, text)
                self.assertNotIn("\npip install httpx", text)

    def test_api_contract_documents_static_png_and_rejects_apng(self):
        for path in (ROOT / "README.md", ROOT / "SKILL.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("静态 PNG", text)
                self.assertIn("不支持 APNG", text)

    def test_template_comments_describe_manual_replacement_not_runtime_injection(self):
        for name in ("cover-clean.html", "infographic-clean.html"):
            path = ROOT / "templates" / name
            with self.subTest(path=name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("render.py 注入", text)
                self.assertIn("手工替换", text)

    def test_generation_retry_cost_semantics_are_explicit(self):
        required = (
            "默认自动重试为 0",
            "生图 POST 无幂等承诺",
            "`--max-n` 只限制目标张数",
            "显式重试会增加实际请求次数",
        )
        for path in (ROOT / "README.md", ROOT / "SKILL.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                for phrase in required:
                    self.assertIn(phrase, text)

    def test_provider_compatibility_does_not_overpromise_azure(self):
        compatible_contract = (
            "OpenAI 官方，或遵循同一套 Bearer 认证与 "
            "`/images/generations` 路径契约的兼容渠道"
        )
        for path in (ROOT / "README.md", ROOT / "SKILL.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(compatible_contract, text)
                self.assertIn("Azure 当前未原生适配", text)


class RenderPngInspectionTests(unittest.TestCase):
    def test_png_size_reads_only_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.png"
            image.write_bytes(_valid_png())
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("must not read the whole file"),
            ):
                self.assertEqual(render_module._png_size(image), (1, 1))


if __name__ == "__main__":
    unittest.main()
