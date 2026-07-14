#!/usr/bin/env python3
"""Regression tests for the documented HTML rendering path."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RENDER = ROOT / "scripts" / "render.py"
INFOGRAPHIC = ROOT / "templates" / "infographic-clean.html"


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


if __name__ == "__main__":
    unittest.main()
