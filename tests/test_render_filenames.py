import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from scripts import render


class RenderFilenameTests(unittest.TestCase):
    def test_colliding_repeated_and_fallback_ids_are_unique(self):
        ids = ["a b", "a-b", "a-b", "!!!", "???", "中文", "中文", "page-8"]
        names = [render.poster_filename(value, i) for i, value in enumerate(ids)]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(Path(name).name == name for name in names))
        self.assertEqual(names, [render.poster_filename(value, i) for i, value in enumerate(ids)])

    def test_render_writes_distinct_paths_without_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "page.html"
            html.write_text("<html></html>")
            posters = [MagicMock(), MagicMock(), MagicMock()]
            for el, node_id in zip(posters, ["a b", "a-b", None]):
                el.get_attribute.return_value = node_id
                el.screenshot.side_effect = lambda *, path: Path(path).write_bytes(b"fixture")
            with patch.object(render, "sync_playwright") as playwright, patch.object(render, "_report"):
                page = playwright.return_value.__enter__.return_value.chromium.launch.return_value.new_page.return_value
                page.query_selector_all.return_value = posters
                paths = render.render(str(html), output_path=str(Path(tmp) / "out"))
            self.assertEqual(len(paths), 3)
            self.assertEqual(len(set(paths)), 3)
            self.assertTrue(all(Path(path).is_file() for path in paths))
