import base64
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import httpx
from scripts import gen, edit


def png(value=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, value, 0, 0]))) + chunk(b"IEND", b""))


class BatchRegressionTests(unittest.TestCase):
    def test_duplicate_and_aliased_outputs_make_no_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alias").symlink_to(root, target_is_directory=True)
            for second in (root / "out.png", root / "alias/out.png", root / "sub/../out.png"):
                for module, batch in ((gen, gen.generate_batch), (edit, edit.edit_batch)):
                    with self.subTest(second=second, module=module.__name__):
                        tasks = [{"prompt": "p", "input": "unused.png", "output": str(path)}
                                 for path in (root / "out.png", second)]
                        with patch.object(module, "_request_once") as request:
                            results = batch(tasks, "fake", "https://example.test/v1", "fake", max_retries=0)
                        request.assert_not_called()
                        self.assertEqual(len(results), 2)
                        self.assertTrue(all(not r["success"] for r in results))
                        self.assertFalse((root / "out.png").exists())

    def test_invalid_later_output_prevents_entire_batch(self):
        for module, batch in ((gen, gen.generate_batch), (edit, edit.edit_batch)):
            with tempfile.TemporaryDirectory() as tmp, patch.object(module, "_request_once") as request:
                tasks = [{"prompt": "p", "input": "unused.png", "output": str(Path(tmp) / name)}
                         for name in ("ok.png", "bad.jpg")]
                results = batch(tasks, "fake", "https://example.test", "fake")
                request.assert_not_called()
                self.assertTrue(all(not r["success"] for r in results))

    def test_distinct_outputs_preserve_both_images(self):
        for module, batch in ((gen, gen.generate_batch), (edit, edit.edit_batch)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source.png"
                source.write_bytes(png())
                tasks = [{"prompt": "p", "input": str(source), "output": str(root / f"{i}.png")} for i in range(2)]
                responses = [httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png(i)).decode()}]})
                             for i in range(2)]
                with patch.object(module, "_request_once", side_effect=responses) as request:
                    results = batch(tasks, "fake", "https://example.test/v1", "fake", workers=1, max_retries=0)
                self.assertEqual(request.call_count, 2)
                self.assertTrue(all(r["success"] for r in results), results)
                self.assertEqual([Path(r["path"]).read_bytes() for r in results], [png(0), png(1)])
