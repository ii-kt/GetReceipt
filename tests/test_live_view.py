from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.browser_session import ManagedBrowser  # noqa: E402
from src.ui.live_view import (  # noqa: E402
    LiveViewUnavailableError,
    _page_point,
    assert_official_page,
)


EPOS_HOSTS = ("www.eposcard.co.jp",)


class OfficialPageGuardTest(unittest.TestCase):
    """The owner drives this page with real input, so its origin is checked."""

    def test_the_provider_https_page_is_allowed(self) -> None:
        assert_official_page(
            "https://www.eposcard.co.jp/memberservice/pc/nocardusedetail/login_dispatch.do",
            EPOS_HOSTS,
        )

    def test_another_host_is_rejected(self) -> None:
        with self.assertRaises(LiveViewUnavailableError):
            assert_official_page("https://eposcard.co.jp.evil.example/login", EPOS_HOSTS)

    def test_plain_http_is_rejected(self) -> None:
        with self.assertRaises(LiveViewUnavailableError):
            assert_official_page("http://www.eposcard.co.jp/login", EPOS_HOSTS)

    def test_embedded_credentials_and_odd_ports_are_rejected(self) -> None:
        for url in (
            "https://user:pass@www.eposcard.co.jp/login",
            "https://www.eposcard.co.jp:8443/login",
        ):
            with self.subTest(url=url), self.assertRaises(LiveViewUnavailableError):
                assert_official_page(url, EPOS_HOSTS)

    def test_a_blank_page_is_rejected(self) -> None:
        with self.assertRaises(LiveViewUnavailableError):
            assert_official_page("about:blank", EPOS_HOSTS)


class TapMappingTest(unittest.TestCase):
    def test_a_tap_on_the_shrunken_image_maps_back_to_the_page(self) -> None:
        """A phone draws the page smaller, so taps must be scaled back up."""

        point = _page_point(
            {"x": 180, "y": 100, "width": 360, "height": 225},
            display_width=360,
            natural_width=1280,
            natural_height=800,
        )

        self.assertEqual((640, 356), point)

    def test_a_tap_can_never_land_outside_the_page(self) -> None:
        point = _page_point(
            {"x": 9999, "y": 9999, "width": 360, "height": 225},
            display_width=360,
            natural_width=1280,
            natural_height=800,
        )

        self.assertEqual((1279, 799), point)


class DragGestureTest(unittest.TestCase):
    """A slide puzzle needs a press, a movement, and a release."""

    def _browser(self) -> tuple[ManagedBrowser, MagicMock]:
        browser = ManagedBrowser.__new__(ManagedBrowser)
        connection = MagicMock()
        browser.connection = connection
        return browser, connection

    def test_the_drag_presses_moves_and_releases_in_order(self) -> None:
        browser, connection = self._browser()

        browser._drag_in_session("session-1", 100, 200, 300, 210, steps=4)

        events = [
            (call.args[1]["type"], call.args[1]["x"], call.args[1]["y"])
            for call in connection.send.call_args_list
        ]
        self.assertEqual("mouseMoved", events[0][0])
        self.assertEqual(("mousePressed", 100, 200), events[1])
        self.assertEqual(("mouseReleased", 300, 210), events[-1])
        # The pointer travels the whole way rather than jumping.
        moves = [event for event in events[2:-1] if event[0] == "mouseMoved"]
        self.assertEqual(4, len(moves))
        self.assertEqual([150, 200, 250, 300], [event[1] for event in moves])

    def test_the_button_is_held_down_for_the_whole_movement(self) -> None:
        browser, connection = self._browser()

        browser._drag_in_session("session-1", 0, 0, 40, 0, steps=2)

        held = [
            call.args[1]["buttons"]
            for call in connection.send.call_args_list
            if call.args[1]["type"] == "mouseMoved" and call.args[1]["button"] == "left"
        ]
        self.assertTrue(held)
        self.assertTrue(all(value == 1 for value in held))

    def test_every_session_call_targets_the_given_session(self) -> None:
        browser, connection = self._browser()

        browser._drag_in_session("session-9", 0, 0, 10, 10, steps=2)

        for call in connection.send.call_args_list:
            self.assertEqual("session-9", call.kwargs["session_id"])


if __name__ == "__main__":
    unittest.main()
