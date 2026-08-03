"""The gesture the viewer asks for has to match what is on the screen.

A slide puzzle needs a drag, so the owner marks the piece and then where it
goes. A bot check needs a button pressed, and there one tap is the whole
gesture - asking for it in the language of pieces and destinations described
nothing that was on the screen, and pressing a button meant tapping it twice
for no reason anybody could see.
"""

from __future__ import annotations

import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.ui.live_view import render_live_view  # noqa: E402


class _Rerun(RuntimeError):
    """Stands in for st.rerun, which ends the script run."""


class _FakeStreamlit:
    def __init__(self) -> None:
        self.session_state: dict = {}
        self.captions: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.checkboxes: list[dict] = []

    def checkbox(self, label, *, value=False, key=None, help=None):
        self.checkboxes.append({"label": label, "value": value, "key": key})
        if key in self.session_state:
            return bool(self.session_state[key])
        return bool(value)

    def caption(self, text, **_kwargs):
        self.captions.append(str(text))

    def info(self, text, **_kwargs):
        self.infos.append(str(text))

    def error(self, text, **_kwargs):
        self.errors.append(str(text))

    def columns(self, count):
        return [SimpleNamespace(button=lambda *a, **k: False) for _ in range(count)]

    def rerun(self):
        raise _Rerun()

    @property
    def text(self) -> str:
        return "\n".join(self.captions + self.infos)


def _png(width: int = 400, height: int = 300) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _browser() -> MagicMock:
    browser = MagicMock()
    browser.current_page_target.return_value = {
        "url": "https://cfg.smt.docomo.ne.jp/auth/cgi/anidlogin"
    }
    browser.screenshot_current_page.return_value = _png()
    return browser


HOSTS = ("cfg.smt.docomo.ne.jp",)


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import PIL  # noqa: F401
        except Exception as error:  # pragma: no cover - deployment dependency
            raise unittest.SkipTest("Pillow is unavailable") from error
        self.tap: dict | None = None
        module = ModuleType("streamlit_image_coordinates")
        module.streamlit_image_coordinates = lambda *a, **k: self.tap
        self._patch = patch.dict(sys.modules, {"streamlit_image_coordinates": module})
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _render(self, st, browser, *, key: str = "view") -> None:
        try:
            render_live_view(st, browser, key=key, allowed_hosts=HOSTS)
        except _Rerun:
            pass


class TapGestureTest(_Harness):
    def test_one_tap_clicks_where_the_owner_tapped(self) -> None:
        st = _FakeStreamlit()
        st.session_state["view__gesture"] = "tap"
        browser = _browser()
        self.tap = {"x": 100, "y": 75, "width": 400, "height": 300, "unix_time": "1"}

        self._render(st, browser)

        browser.drag_current_page.assert_called_once()
        args = browser.drag_current_page.call_args.args
        # Press and release at the same point: a plain click.
        self.assertEqual((100, 75, 100, 75), args)

    def test_it_does_not_talk_about_pieces(self) -> None:
        st = _FakeStreamlit()
        st.session_state["view__gesture"] = "tap"

        self._render(st, _browser())

        self.assertNotIn("ピース", st.text)
        self.assertIn("タップ", st.text)

    def test_no_half_finished_selection_is_left_behind(self) -> None:
        """Switching out of drag mode must not strand a marked start point."""

        st = _FakeStreamlit()
        st.session_state["view__gesture"] = "tap"
        st.session_state["view__drag_start"] = (10, 20)

        self._render(st, _browser())

        self.assertNotIn("view__drag_start", st.session_state)


class DragGestureStillWorksTest(_Harness):
    def test_a_puzzle_still_takes_two_taps(self) -> None:
        st = _FakeStreamlit()
        st.session_state["view__gesture"] = "drag"
        browser = _browser()
        self.tap = {"x": 100, "y": 75, "width": 400, "height": 300, "unix_time": "1"}

        self._render(st, browser)

        # The first tap only marks the piece.
        browser.drag_current_page.assert_not_called()
        self.assertEqual((100, 75), st.session_state["view__drag_start"])

        self.tap = {"x": 260, "y": 75, "width": 400, "height": 300, "unix_time": "2"}
        self._render(st, browser)

        browser.drag_current_page.assert_called_once_with(100, 75, 260, 75)

    def test_an_older_page_that_says_nothing_still_gets_the_drag(self) -> None:
        """A deploy can serve this module beside the previous page."""

        st = _FakeStreamlit()
        browser = _browser()
        self.tap = {"x": 100, "y": 75, "width": 400, "height": 300, "unix_time": "1"}

        self._render(st, browser)

        browser.drag_current_page.assert_not_called()
        self.assertEqual((100, 75), st.session_state["view__drag_start"])

    def test_the_owner_can_switch_gesture_whatever_was_offered(self) -> None:
        st = _FakeStreamlit()
        st.session_state["view__gesture"] = "drag"
        # The checkbox the owner unticked.
        st.session_state["view__drag_mode"] = False
        browser = _browser()
        self.tap = {"x": 100, "y": 75, "width": 400, "height": 300, "unix_time": "1"}

        self._render(st, browser)

        browser.drag_current_page.assert_called_once_with(100, 75, 100, 75)


if __name__ == "__main__":
    unittest.main()
