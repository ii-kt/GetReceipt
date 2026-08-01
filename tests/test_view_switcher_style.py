from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.ui import styles as ui_styles  # noqa: E402


def injected_css() -> str:
    captured: list[str] = []
    with patch.object(ui_styles.st, "markdown", side_effect=lambda body, **_: captured.append(body)):
        ui_styles.inject_design()
    return "\n".join(captured)


class ViewSwitcherSelectionTest(unittest.TestCase):
    """The chosen view has to look chosen.

    Streamlit marks it with kind="segmented_controlActive", not with
    aria-checked. Styling only the aria state left both halves identical, so
    there was no way to tell which view was on screen.
    """

    def setUp(self) -> None:
        self.css = injected_css()

    def test_the_selected_segment_is_styled_by_the_attribute_streamlit_sets(self) -> None:
        self.assertIn('button[kind$="Active"]', self.css)
        self.assertIn('button[data-testid$="Active"]', self.css)

    def test_the_aria_states_are_kept_as_well(self) -> None:
        """So a Streamlit version that does use them keeps working."""

        self.assertIn('button[aria-checked="true"]', self.css)
        self.assertIn('button[aria-pressed="true"]', self.css)

    def test_the_selected_segment_is_filled_not_just_recoloured(self) -> None:
        block = re.search(
            r'button\[kind\$="Active"\][^{]*\{([^}]*)\}',
            self.css,
            re.S,
        )
        self.assertIsNotNone(block)
        rule = block.group(1)
        self.assertIn("color: #fff", rule)
        self.assertIn("background: var(--gr-ink)", rule)

    def test_the_unselected_segment_stays_plain(self) -> None:
        self.assertIn("background: transparent !important", self.css)


if __name__ == "__main__":
    unittest.main()
