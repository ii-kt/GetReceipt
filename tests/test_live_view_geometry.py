"""A tap in the mirrored view has to land where the owner tapped.

The viewer maps a tap to screenshot pixels, and CDP input takes CSS pixels.
Those agree only while the capture is 1:1 with the viewport. If anything in
the browser setup ever changes that - a device-metrics override, a display
scale - every tap lands off by that ratio and a gate becomes impossible to
clear, with nothing on screen to say why.

Skipped where no browser is installed.
"""

from __future__ import annotations

import os
import sys
import unittest
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

os.environ.setdefault("GETRECEIPT_ALLOW_CHROMIUM", "1")

from src.automation.browser_session import (  # noqa: E402
    ManagedBrowser,
    find_browser_executable,
)
from src.ui.live_view import _page_point  # noqa: E402


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0}#target{position:absolute;left:400px;top:300px;
width:80px;height:40px}</style></head><body>
<button id="target" onclick="document.title='HIT'">check</button>
</body></html>"""


@unittest.skipUnless(find_browser_executable(), "no browser installed")
class LiveViewGeometryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        try:
            from PIL import Image
        except Exception as error:  # pragma: no cover - deployment dependency
            raise unittest.SkipTest("Pillow is unavailable") from error
        cls.Image = Image
        cls._temp = tempfile.TemporaryDirectory()
        root = Path(cls._temp.name)
        cls.browser = ManagedBrowser(
            profile_dir=root / "profile", download_dir=root / "downloads"
        )
        page = root / "page.html"
        page.write_text(PAGE, encoding="utf-8")
        cls.browser.navigate(page.as_uri(), wait_seconds=1.0)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close(clear_profile=True)
        finally:
            cls._temp.cleanup()

    def test_the_capture_matches_the_viewport(self) -> None:
        metrics = self.browser.evaluate(
            "(() => ({w: window.innerWidth, h: window.innerHeight}))()", timeout=10
        )
        image = self.Image.open(BytesIO(self.browser.screenshot_current_page()))
        image.load()

        self.assertEqual((metrics["w"], metrics["h"]), image.size)

    def test_a_tap_reaches_the_control_it_was_aimed_at(self) -> None:
        image = self.Image.open(BytesIO(self.browser.screenshot_current_page()))
        image.load()
        natural_width, natural_height = image.size
        rect = self.browser.evaluate(
            "(() => { const r = document.getElementById('target')"
            ".getBoundingClientRect(); return {x: Math.round(r.left + r.width / 2),"
            " y: Math.round(r.top + r.height / 2)}; })()",
            timeout=10,
        )
        # As the phone sees it: the image is drawn narrower than it was taken.
        display_width = min(720, natural_width)
        scale = display_width / natural_width
        point = _page_point(
            {
                "x": round(rect["x"] * scale),
                "y": round(rect["y"] * scale),
                "width": display_width,
                "height": round(natural_height * scale),
            },
            display_width=display_width,
            natural_width=natural_width,
            natural_height=natural_height,
        )

        # Two taps on the same spot are how the viewer sends a plain click,
        # which is all a bot-check box needs.
        self.browser.drag_current_page(point[0], point[1], point[0], point[1])

        self.assertEqual("HIT", self.browser.evaluate("document.title", timeout=10))


if __name__ == "__main__":
    unittest.main()
