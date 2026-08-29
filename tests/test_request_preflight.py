import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class FakeApiResponse:
    status_code = 200
    text = ""

    def __init__(self, payload=None, *, json_error=None, text=""):
        self.payload = payload
        self.json_error = json_error
        self.text = text

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class OutputPreflightTests(unittest.TestCase):
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

    def test_generate_returns_diagnostic_error_for_malformed_200_responses(self):
        gen = self._load("gen")
        cases = {
            "html": FakeApiResponse(json_error=ValueError("not json"), text="<html>ok</html>"),
            "empty object": FakeApiResponse({}),
            "empty data": FakeApiResponse({"data": []}),
            "null item": FakeApiResponse({"data": [None]}),
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for label, response in cases.items():
                with self.subTest(label=label):
                    with mock.patch.object(gen, "_request_once", return_value=response):
                        result = gen._generate_core(
                            prompt="test",
                            api_key="key",
                            base_url="https://example.invalid/v1",
                            model="model",
                            output_path=str(root / f"{label.replace(' ', '-')}.png"),
                            max_retries=0,
                        )

                    self.assertFalse(result["success"])
                    self.assertIn("API 响应格式错误", result["error"])

    def test_edit_returns_diagnostic_error_for_malformed_200_responses(self):
        edit = self._load("edit")
        cases = {
            "html": FakeApiResponse(json_error=ValueError("not json"), text="<html>ok</html>"),
            "empty object": FakeApiResponse({}),
            "empty data": FakeApiResponse({"data": []}),
            "null item": FakeApiResponse({"data": [None]}),
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "input.png"
            source.write_bytes(b"input")
            for label, response in cases.items():
                with self.subTest(label=label):
                    with mock.patch.object(edit, "_request_once", return_value=response):
                        result = edit._edit_core(
                            input_image=str(source),
                            prompt="test",
                            api_key="key",
                            base_url="https://example.invalid/v1",
                            model="model",
                            output_path=str(root / f"edit-{label.replace(' ', '-')}.png"),
                            max_retries=0,
                        )

                    self.assertFalse(result["success"])
                    self.assertIn("API 响应格式错误", result["error"])


if __name__ == "__main__":
    unittest.main()
