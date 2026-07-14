#!/usr/bin/env python3
"""Repository-facing documentation hygiene checks."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class RepositoryHygieneTests(unittest.TestCase):
    def test_readme_has_no_prompt_boundary_artifacts(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("</content>", readme)
        self.assertNotIn("</invoke>", readme)


if __name__ == "__main__":
    unittest.main()
