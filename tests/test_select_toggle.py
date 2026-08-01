from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.ui.select_toggle import _SCRIPT, render_select_arrow_toggle  # noqa: E402


class SelectArrowToggleTest(unittest.TestCase):
    """Verified against a real Chrome driving a real Streamlit selectbox.

    Without this, tapping the arrow a second time left the month list open:
    the widget is searchable, so the tap only refocuses it for typing.
    """

    def test_the_script_is_rendered_without_taking_up_space(self) -> None:
        with patch("streamlit.components.v1.html") as html:
            render_select_arrow_toggle(MagicMock())

        html.assert_called_once()
        self.assertEqual(0, html.call_args.kwargs["height"])
        self.assertIn("data-baseweb", html.call_args.args[0])

    def test_a_page_that_cannot_take_the_script_still_renders(self) -> None:
        """A picker that only opens is an annoyance, never a broken page."""

        with patch(
            "streamlit.components.v1.html", side_effect=RuntimeError("no host")
        ):
            render_select_arrow_toggle(MagicMock())

    def test_it_only_acts_on_an_open_list(self) -> None:
        """A tap on a closed picker must still open it, untouched."""

        self.assertIn("aria-expanded", _SCRIPT)
        self.assertIn("!== 'true'", _SCRIPT)

    def test_it_closes_the_same_way_the_widget_already_does(self) -> None:
        """Escape is the widget's own close, so the selection is left alone."""

        self.assertIn("Escape", _SCRIPT)
        self.assertNotIn("value =", _SCRIPT)
        self.assertNotIn(".click()", _SCRIPT)

    def test_the_mouse_events_after_the_pointer_event_are_swallowed(self) -> None:
        """They are separate events; unswallowed they reopen the list at once."""

        for event_type in ("mousedown", "mouseup", "click", "touchstart", "touchend"):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, _SCRIPT)

    def test_a_cross_origin_page_is_left_alone(self) -> None:
        self.assertIn("catch", _SCRIPT)
        self.assertIn("window.parent", _SCRIPT)


if __name__ == "__main__":
    unittest.main()
