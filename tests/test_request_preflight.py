import subprocess
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class OutputPreflightTests(unittest.TestCase):
    def test_cli_rejects_non_object_batch_item(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "tasks.json"
            batch.write_text("[42]", encoding="utf-8")
            for command in ("gen", "edit"):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, str(root / "scripts" / f"{command}.py"),
                         "--batch", str(batch), "--api-key", "unused",
                         "--base-url", "http://127.0.0.1:1/v1"],
                        capture_output=True, text=True, encoding="utf-8", timeout=10,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("任务 #1 必须是 JSON 对象", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    @staticmethod
    def _load(name):
        fake_httpx = types.SimpleNamespace(
            Response=object,
            TimeoutException=type("TimeoutException", (Exception,), {}),
            ConnectError=type("ConnectError", (Exception,), {}),
            HTTPError=type("HTTPError", (Exception,), {}),
        )
        module_name = f"scripts.{name}"
        sys.modules.pop(module_name, None)
        with mock.patch.dict(sys.modules, {"httpx": fake_httpx}):
            return importlib.import_module(module_name)

    def tearDown(self):
        sys.modules.pop("scripts.gen", None)
        sys.modules.pop("scripts.edit", None)

    def test_generate_rejects_bad_output_before_request(self):
        gen = self._load("gen")
        with mock.patch.object(gen, "_request_once", side_effect=AssertionError("request called")):
            result = gen._generate_core(
                prompt="test",
                api_key="key",
                base_url="https://example.invalid/v1",
                model="model",
                output_path="bad.jpg",
                max_retries=0,
            )

        self.assertFalse(result["success"])
        self.assertIn(".png", result["error"])

    def test_edit_rejects_bad_output_before_request(self):
        edit = self._load("edit")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.png"
            source.write_bytes(b"input")
            with mock.patch.object(edit, "_request_once", side_effect=AssertionError("request called")):
                result = edit._edit_core(
                    input_image=str(source),
                    prompt="test",
                    api_key="key",
                    base_url="https://example.invalid/v1",
                    model="model",
                    output_path="bad.jpg",
                    max_retries=0,
                )

        self.assertFalse(result["success"])
        self.assertIn(".png", result["error"])


if __name__ == "__main__":
    unittest.main()
