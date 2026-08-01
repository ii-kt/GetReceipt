from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.epos import (  # noqa: E402
    AcquisitionError,
    EposAutoFetcher,
    build_epos_puzzle_state_expression,
    build_epos_puzzle_submit_expression,
)


class EposPuzzleResumeTest(unittest.TestCase):
    """Epos's image check is answered by the owner, never by the app."""

    def _fetcher(self, evaluations: list) -> tuple[EposAutoFetcher, MagicMock]:
        browser = MagicMock()
        browser.evaluate.side_effect = evaluations
        return EposAutoFetcher(browser, credentials={}), browser

    def test_an_untouched_puzzle_is_told_apart_by_its_fingerprint(self) -> None:
        """The field arrives already holding the piece's starting position.

        Treating a non-empty field as "solved" sent that starting value, Epos
        answered that the puzzle was wrong, and the sign-in was spent.
        """

        fetcher, _ = self._fetcher([{"present": True, "fingerprint": "a1b2c3"}])

        state = fetcher.interactive_challenge_state()

        self.assertEqual("a1b2c3", state["fingerprint"])
        self.assertTrue(state["present"])

    def test_the_fingerprint_is_a_digest_not_the_answer(self) -> None:
        from src.automation.epos import build_epos_puzzle_state_expression

        script = build_epos_puzzle_state_expression()

        self.assertIn("digest(value)", script)
        self.assertNotIn("answer: value", script)

    def test_the_answer_the_owner_produced_is_submitted_unchanged(self) -> None:
        fetcher, browser = self._fetcher(
            [{"present": True, "fingerprint": "moved"}, "form.submit()"]
        )
        with (
            patch.object(fetcher, "_wait_for_login_after_security_code"),
            patch.object(fetcher, "fetch_pdf", return_value="statement") as fetch,
            patch("src.automation.epos.time.sleep"),
        ):
            result = fetcher.resume_after_interactive_challenge("2026-07")

        self.assertEqual("statement", result)
        fetch.assert_called_once_with("2026-07")
        submit_script = browser.evaluate.call_args_list[1].args[0]
        self.assertIn("puzzleVerifyForm", submit_script)
        # The app must never write the answer field, only send what is there.
        self.assertNotIn("capy_answer\"].value =", submit_script)
        self.assertNotIn("capy_answer'].value =", submit_script)

    def test_a_page_without_the_puzzle_resumes_directly(self) -> None:
        fetcher, browser = self._fetcher([{"present": False}])
        with (
            patch.object(fetcher, "_wait_for_login_after_security_code") as wait,
            patch.object(fetcher, "fetch_pdf", return_value="statement"),
        ):
            fetcher.resume_after_interactive_challenge("2026-07")

        wait.assert_called_once()
        self.assertEqual(1, browser.evaluate.call_count)


class PuzzleScriptTest(unittest.TestCase):
    def test_the_state_script_only_reads_the_answer_field(self) -> None:
        script = build_epos_puzzle_state_expression()

        self.assertIn("capy_answer", script)
        self.assertNotIn(".value =", script)

    def test_the_submit_script_only_submits(self) -> None:
        script = build_epos_puzzle_submit_expression()

        self.assertIn("puzzleVerifyForm", script)
        self.assertNotIn(".value =", script)


if __name__ == "__main__":
    unittest.main()
