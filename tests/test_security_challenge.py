from __future__ import annotations

import base64
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.browser_session import BrowserAutomationError, ManagedBrowser  # noqa: E402
from src.automation.epos import AcquisitionError, FetchedStatement  # noqa: E402
from src.automation.official_sites import (  # noqa: E402
    CommufaAutoFetcher,
    _apply_auto_login_result,
    build_configured_auto_login_expression,
)
from src.automation.security_challenge import (  # noqa: E402
    BrowserAttemptUnavailableError,
    BrowserLeaseRegistry,
    BrowserLeaseUnavailableError,
    ChallengeKind,
    SecurityChallengeObservation,
    SecurityChallengeSubmissionError,
    SecurityCodeValidationError,
    inspect_commufa_security_challenge,
    new_attempt_run_dir,
    submit_commufa_security_code,
)


class FakeProcess:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code

    def poll(self) -> int | None:
        return self.return_code


class FakeBrowser:
    def __init__(self, *, url: str = "https://mypage.commufa.jp/join/s/") -> None:
        self.url = url
        self.process = FakeProcess()
        self.closed = False
        self.clear_calls = 0
        self.expressions: list[str] = []
        self.evaluation_result: dict[str, object] = {"ok": True}
        self.profile_dir = Path("profile")
        self.download_dir = Path("downloads")

    def current_page_target(self) -> dict[str, str]:
        if self.process.poll() is not None:
            raise BrowserAutomationError("browser closed")
        return {"targetId": "target-1", "type": "page", "url": self.url}

    def evaluate_current_page(self, expression: str, *, timeout: float = 30) -> dict[str, object]:
        self.expressions.append(expression)
        return dict(self.evaluation_result)

    def current_page_summary(self) -> dict[str, object]:
        return {
            "url": self.url,
            "title": "Myコミュファ",
            "text": "ログアウト ご契約内容",
            "passwordFields": 0,
            "visibleInputs": [],
        }

    def clear_downloads(self) -> None:
        self.clear_calls += 1

    def close(self, *, clear_profile: bool = False) -> None:
        self.closed = True
        self.process.return_code = 0


class FakeTimer:
    def __init__(self, interval: float, function, args=()) -> None:
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.function(*self.args)


class SecurityChallengeTest(unittest.TestCase):
    def test_acquisition_error_exposes_only_normalized_challenge_kind(self) -> None:
        error = AcquisitionError(
            "追加認証が必要です。",
            code="SECURITY_CHALLENGE",
            challenge_kind=ChallengeKind.VERIFICATION_CODE,
        )

        self.assertEqual("verification_code", error.challenge_kind)
        self.assertNotIn("verification_code", repr(error))

    def test_commufa_login_script_classifies_only_exact_code_input_as_resumable(self) -> None:
        expression = build_configured_auto_login_expression(
            {"login_id": "member@example.invalid", "password": "password-placeholder"}
        )

        self.assertIn('location.hostname === "mypage.commufa.jp"', expression)
        self.assertIn("SECURITY_ORIGIN_MISMATCH", expression)
        self.assertIn('challengeKind: "verification_code"', expression)
        self.assertIn('challengeKind: "captcha"', expression)
        self.assertIn('challengeKind: "other"', expression)
        self.assertIn('["text", "tel", "number", "password"]', expression)

    def test_auto_login_result_preserves_verification_and_captcha_kinds(self) -> None:
        browser = FakeBrowser()
        for raw_kind, expected in (
            ("verification_code", "verification_code"),
            ("captcha", "captcha"),
            ("unknown-provider-challenge", "other"),
        ):
            with self.subTest(raw_kind=raw_kind):
                with self.assertRaises(AcquisitionError) as caught:
                    _apply_auto_login_result(
                        browser,
                        {"code": "SECURITY_CHALLENGE", "challengeKind": raw_kind},
                        "Wi-Fi",
                    )
                self.assertEqual(expected, caught.exception.challenge_kind)

    def test_security_code_requires_exactly_six_ascii_digits_without_echo(self) -> None:
        browser = FakeBrowser()
        for rejected in ("12x45", "１２３４５６", "١٢٣٤٥٦"):
            with self.subTest(rejected=rejected):
                with self.assertRaises(SecurityCodeValidationError) as caught:
                    submit_commufa_security_code(browser, rejected)

                self.assertNotIn(rejected, str(caught.exception))
                self.assertNotIn(rejected, repr(caught.exception))
        self.assertEqual([], browser.expressions)

    def test_security_code_is_submitted_only_to_exact_https_commufa_host(self) -> None:
        for url in (
            "http://mypage.commufa.jp/join/s/",
            "https://evil.example/mypage.commufa.jp",
            "https://sub.mypage.commufa.jp/join/s/",
        ):
            browser = FakeBrowser(url=url)
            with self.assertRaises(SecurityChallengeSubmissionError) as caught:
                submit_commufa_security_code(browser, "123456")
            self.assertEqual("other", caught.exception.challenge_kind)
            self.assertEqual([], browser.expressions)

        browser = FakeBrowser()
        submit_commufa_security_code(browser, "123456")
        self.assertEqual(1, len(browser.expressions))
        self.assertIn('location.hostname !== "mypage.commufa.jp"', browser.expressions[0])

    def test_browser_submission_errors_never_echo_the_security_code(self) -> None:
        browser = FakeBrowser()
        code = "123456"
        browser.evaluate_current_page = Mock(
            side_effect=BrowserAutomationError(f"evaluation failed: {code}")
        )

        with self.assertRaises(SecurityChallengeSubmissionError) as caught:
            submit_commufa_security_code(browser, code)

        self.assertNotIn(code, str(caught.exception))
        self.assertNotIn(code, repr(caught.exception))

    def test_captcha_submission_is_terminal_and_not_reclassified_as_code_input(self) -> None:
        browser = FakeBrowser()
        browser.evaluation_result = {"ok": False, "error": "CAPTCHA_PRESENT"}
        with self.assertRaises(SecurityChallengeSubmissionError) as caught:
            submit_commufa_security_code(browser, "123456")
        self.assertEqual("captcha", caught.exception.challenge_kind)

        fetcher = CommufaAutoFetcher(browser, credentials={})
        with self.assertRaises(AcquisitionError) as wrapped:
            fetcher.resume_after_security_code("2026-07", "123456")
        self.assertEqual("captcha", wrapped.exception.challenge_kind)

    def test_probe_keeps_captcha_separate_from_verification_code(self) -> None:
        browser = FakeBrowser()
        browser.evaluation_result = {"kind": "captcha", "codeRejected": False}
        captcha = inspect_commufa_security_challenge(browser)
        browser.evaluation_result = {"kind": "verification_code", "codeRejected": True}
        verification = inspect_commufa_security_challenge(browser)

        self.assertEqual(SecurityChallengeObservation(ChallengeKind.CAPTCHA), captcha)
        self.assertEqual(
            SecurityChallengeObservation(ChallengeKind.VERIFICATION_CODE, code_rejected=True),
            verification,
        )

    def test_commufa_resume_does_not_navigate_before_fetching_current_page(self) -> None:
        browser = FakeBrowser()
        browser.navigate = Mock(side_effect=AssertionError("resume must not navigate"))
        statement = FetchedStatement(
            content=b"%PDF-1.4\n",
            source_url=browser.url,
            original_file_name="commufa.pdf",
            metadata_text="",
            logs=(),
        )
        fetcher = CommufaAutoFetcher(browser, credentials={})

        with (
            patch("src.automation.official_sites.submit_commufa_security_code") as submit,
            patch.object(fetcher, "_wait_for_login_after_security_code") as wait,
            patch.object(fetcher, "_fetch_statement_from_current_page", return_value=statement) as fetch,
        ):
            result = fetcher.resume_after_security_code("2026-07", "123456")

        submit.assert_called_once_with(browser, "123456")
        wait.assert_called_once_with()
        fetch.assert_called_once_with("2026-07")
        self.assertIs(result, statement)
        self.assertEqual(1, browser.clear_calls)
        browser.navigate.assert_not_called()

    def test_commufa_prints_temporary_pdf_inside_browser_download_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            download_dir = Path(temp_dir) / "downloads"
            browser = FakeBrowser()
            browser.download_dir = download_dir
            browser.evaluate = Mock(return_value={"ok": True, "metadataText": "利用明細"})
            browser.switch_to_page = Mock(return_value=None)
            browser.page_summary = Mock(return_value={"url": browser.url, "text": "利用明細"})
            printed_paths: list[Path] = []

            def print_to_pdf(path: Path) -> Path:
                printed_paths.append(path)
                return path

            browser.print_to_pdf = print_to_pdf
            fetcher = CommufaAutoFetcher(browser, credentials={})
            with (
                patch("src.automation.official_sites._downloaded_pdf_content", return_value=b"%PDF-1.4\n"),
                patch("src.automation.official_sites.assert_commufa_usage_month"),
                patch("src.automation.official_sites.time.sleep"),
            ):
                fetcher._fetch_statement_from_current_page("2026-07")

            self.assertEqual(1, len(printed_paths))
            self.assertEqual(download_dir, printed_paths[0].parent)

    def test_page_summary_never_reads_input_values(self) -> None:
        expression = ManagedBrowser._page_summary_expression()

        self.assertNotIn("el.value", expression)
        self.assertIn("el.placeholder", expression)

    def test_password_style_otp_fields_remain_in_strict_otp_detection(self) -> None:
        from src.automation import security_challenge as challenge_module

        for expression in (
            challenge_module._COMMUFA_CHALLENGE_PROBE,
            challenge_module._COMMUFA_CODE_FILL_TEMPLATE,
        ):
            self.assertIn('["text", "tel", "number", "password"]', expression)
            self.assertIn('autocomplete === "one-time-code"', expression)
            self.assertIn("maxLength === 0 || maxLength === 6", expression)


class BrowserLeaseRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / "runtime"
        self.runtime_root.mkdir()
        self.clock = [100.0]
        self.timers: list[FakeTimer] = []

        def timer_factory(interval, function, args=()):
            timer = FakeTimer(interval, function, args)
            self.timers.append(timer)
            return timer

        self.registry = BrowserLeaseRegistry(
            runtime_root=self.runtime_root,
            timer_factory=timer_factory,
            monotonic=lambda: self.clock[0],
            utcnow=lambda: datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.registry.close_all()
        self.temp_dir.cleanup()

    def create_lease(self):
        browser = FakeBrowser()
        run_dir = new_attempt_run_dir(
            "commufa",
            "2026-07",
            runtime_root=self.runtime_root,
        )
        run_dir.mkdir(parents=True)
        browser.profile_dir = run_dir / "profile"
        browser.download_dir = run_dir / "downloads"
        (run_dir / "sensitive.tmp").write_text("temporary", encoding="utf-8")
        ticket = self.registry.create(
            service_id="commufa",
            target_month="2026-07",
            browser=browser,
            run_dir=run_dir,
        )
        return browser, run_dir, ticket

    def test_ticket_is_256_bit_opaque_and_exposes_read_only_expiry(self) -> None:
        _browser, _run_dir, ticket = self.create_lease()
        decoded = base64.urlsafe_b64decode(ticket.token + "=" * (-len(ticket.token) % 4))

        self.assertEqual(32, len(decoded))
        self.assertNotIn(ticket.token, repr(ticket))
        self.assertEqual(datetime(2026, 7, 15, 0, 10, tzinfo=timezone.utc), ticket.expires_at)
        self.assertEqual(ticket.expires_at, self.registry.metadata(ticket.token).expires_at)
        self.assertTrue(self.timers[0].daemon)
        self.assertTrue(self.timers[0].started)

    def test_working_the_page_gives_the_owner_the_hold_back(self) -> None:
        """An image check runs several rounds, each a tap and a redraw.

        The hold counted down the whole time, so the browser could be taken
        away part-way through and every round already answered thrown out.
        """

        _browser, _run_dir, ticket = self.create_lease()
        self.clock[0] += 9 * 60

        metadata = self.registry.extend(ticket.token)

        self.clock[0] += 5 * 60
        # Past the original ten minutes, and still there.
        self.assertEqual(
            "commufa", self.registry.metadata(ticket.token).service_id
        )
        self.assertEqual(ticket.expires_at, metadata.expires_at)
        # The old countdown was called off rather than left to fire.
        self.assertTrue(self.timers[0].cancelled)
        self.assertTrue(self.timers[-1].started)

    def test_a_hold_that_already_ran_out_is_not_revived(self) -> None:
        _browser, _run_dir, ticket = self.create_lease()
        self.clock[0] += 11 * 60

        with self.assertRaises(BrowserLeaseUnavailableError):
            self.registry.extend(ticket.token)

    def test_atomic_claim_allows_only_one_process_wide_attempt(self) -> None:
        barrier = threading.Barrier(3)
        tickets = []
        errors = []

        def claim(service_id: str) -> None:
            barrier.wait(timeout=2)
            try:
                tickets.append(
                    self.registry.claim_attempt(
                        service_id=service_id,
                        target_month="2026-07",
                    )
                )
            except BrowserAttemptUnavailableError as error:
                errors.append(error)

        first = threading.Thread(target=claim, args=("commufa",))
        second = threading.Thread(target=claim, args=("epos",))
        first.start()
        second.start()
        barrier.wait(timeout=2)
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(1, len(tickets))
        self.assertEqual(1, len(errors))
        self.assertEqual(1, len(self.registry))
        self.assertTrue(self.registry.release_attempt(tickets[0].token))
        self.assertEqual(0, len(self.registry))

        replacement = self.registry.claim_attempt(service_id="mobile", target_month="2026-07")
        self.assertEqual("mobile", replacement.service_id)

    def test_claim_promotes_same_token_and_late_claim_timer_cannot_close_lease(self) -> None:
        claim = self.registry.claim_attempt(service_id="commufa", target_month="2026-07")
        claim_timer = self.timers[0]
        browser = FakeBrowser()
        run_dir = new_attempt_run_dir(
            "commufa",
            "2026-07",
            runtime_root=self.runtime_root,
        )
        run_dir.mkdir(parents=True)
        browser.profile_dir = run_dir / "profile"
        browser.download_dir = run_dir / "downloads"

        lease = self.registry.promote_claim_to_lease(
            claim.token,
            browser=browser,
            run_dir=run_dir,
        )

        self.assertEqual(claim.token, lease.token)
        self.assertTrue(claim_timer.cancelled)
        self.assertEqual(2, len(self.timers))
        self.assertEqual(lease.expires_at, self.registry.metadata(lease.token).expires_at)

        # A Timer.cancel() racing with its callback must not discard the newer
        # lease generation that intentionally reuses the opaque claim token.
        claim_timer.fire()
        self.assertFalse(browser.closed)
        self.assertEqual(1, len(self.registry))
        self.registry.metadata(lease.token)

        self.timers[1].fire()
        self.assertTrue(browser.closed)
        self.assertFalse(run_dir.exists())
        self.assertEqual(0, len(self.registry))

    def test_legacy_create_respects_an_active_pre_login_claim(self) -> None:
        claim = self.registry.claim_attempt(service_id="commufa", target_month="2026-07")
        browser = FakeBrowser()
        run_dir = new_attempt_run_dir(
            "epos",
            "2026-07",
            runtime_root=self.runtime_root,
        )
        run_dir.mkdir(parents=True)
        browser.profile_dir = run_dir / "profile"
        browser.download_dir = run_dir / "downloads"

        with self.assertRaises(BrowserAttemptUnavailableError):
            self.registry.create(
                service_id="epos",
                target_month="2026-07",
                browser=browser,
                run_dir=run_dir,
            )

        self.assertFalse(browser.closed)
        self.assertTrue(self.registry.release_attempt(claim.token))

    def test_checkout_validates_service_and_month_and_serializes_access(self) -> None:
        _browser, _run_dir, ticket = self.create_lease()
        entered_first = threading.Event()
        release_first = threading.Event()
        entered_second = threading.Event()

        def first() -> None:
            with self.registry.checkout(
                ticket.token,
                expected_service_id="commufa",
                expected_target_month="2026-07",
            ):
                entered_first.set()
                release_first.wait(timeout=2)

        def second() -> None:
            entered_first.wait(timeout=2)
            with self.registry.checkout(ticket.token):
                entered_second.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        second_thread.start()
        self.assertTrue(entered_first.wait(timeout=2))
        self.assertFalse(entered_second.wait(timeout=0.05))
        release_first.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)
        self.assertTrue(entered_second.is_set())

        with self.assertRaises(BrowserLeaseUnavailableError):
            with self.registry.checkout(ticket.token, expected_service_id="epos"):
                pass

    def test_expiry_closes_browser_and_deletes_owned_run_dir(self) -> None:
        browser, run_dir, ticket = self.create_lease()
        self.clock[0] += 601

        with self.assertRaises(BrowserLeaseUnavailableError):
            self.registry.metadata(ticket.token)

        self.assertTrue(browser.closed)
        self.assertFalse(run_dir.exists())
        self.assertEqual(0, len(self.registry))

    def test_timer_discards_lease_and_duplicate_run_dir_is_rejected(self) -> None:
        browser, run_dir, ticket = self.create_lease()
        another_browser = FakeBrowser()
        another_browser.profile_dir = run_dir / "profile"
        another_browser.download_dir = run_dir / "downloads"
        with self.assertRaises(ValueError):
            self.registry.create(
                service_id="commufa",
                target_month="2026-07",
                browser=another_browser,
                run_dir=run_dir,
            )

        self.timers[0].fire()

        self.assertTrue(browser.closed)
        self.assertFalse(run_dir.exists())
        self.assertEqual(0, len(self.registry))
        with self.assertRaises(BrowserLeaseUnavailableError):
            self.registry.metadata(ticket.token)

    def test_registry_rejects_browser_files_outside_owned_attempt_directory(self) -> None:
        browser = FakeBrowser()
        run_dir = new_attempt_run_dir(
            "commufa",
            "2026-07",
            runtime_root=self.runtime_root,
        )
        run_dir.mkdir(parents=True)

        with self.assertRaises(ValueError):
            self.registry.create(
                service_id="commufa",
                target_month="2026-07",
                browser=browser,
                run_dir=run_dir,
            )


if __name__ == "__main__":
    unittest.main()


class CommufaCodeCommitTest(unittest.TestCase):
    """Filling and submitting the code must be separate page evaluations.

    Submitting in the same tick sends an empty code: the provider then
    redisplays the form without mailing a new one, which looks to the owner
    like the code was rejected.
    """

    class _Browser:
        def __init__(self) -> None:
            self.expressions: list[str] = []

        def current_page_target(self):
            return {"url": "https://mypage.commufa.jp/join/s/login/"}

        def evaluate_current_page(self, expression, timeout=10):
            self.expressions.append(expression)
            if "securityCode" in expression:
                return {"ok": True, "filled": True}
            return {"ok": True}

    def test_code_is_committed_before_submission(self) -> None:
        from src.automation import security_challenge as challenge_module

        browser = self._Browser()
        with patch.object(challenge_module.time, "sleep") as sleep:
            challenge_module.submit_commufa_security_code(browser, "123456")

        self.assertEqual(2, len(browser.expressions))
        # The submit pass must never carry the one-time code.
        self.assertNotIn("123456", browser.expressions[1])
        sleep.assert_called_once()



class AbandonedSlotReclaimTest(unittest.TestCase):
    """A new acquisition must not be blocked by an abandoned attempt.

    The registry allows one attempt at a time. When a challenge is abandoned
    without its token (a lost session, an errored run), the slot stayed held
    for its whole TTL, so the next acquisition logged in and made the provider
    mail a code before failing to take the slot.
    """

    def test_close_all_frees_the_slot_for_a_new_attempt(self) -> None:
        from src.automation import security_challenge as challenge_module

        registry = challenge_module.BrowserLeaseRegistry(
            timer_factory=FakeTimer,
        )
        registry.claim_attempt(service_id="commufa", target_month="2026-06")

        with self.assertRaises(challenge_module.BrowserAttemptUnavailableError):
            registry.claim_attempt(service_id="commufa", target_month="2026-06")

        registry.close_all()

        # The slot is free again, so the owner's retry can proceed.
        ticket = registry.claim_attempt(
            service_id="commufa", target_month="2026-06"
        )
        self.assertEqual("commufa", ticket.service_id)


class CommufaSubmitControlTest(unittest.TestCase):
    """The verification form's submit control is 「検証」.

    「コードを再送信」contains 「送信」, so a naive match reissues the code and
    redisplays the same form, which is indistinguishable from a rejected code.
    """

    def _template(self) -> str:
        from src.automation import security_challenge as challenge_module

        return challenge_module._COMMUFA_CODE_SUBMIT_TEMPLATE

    def test_verify_label_is_a_submit_word(self) -> None:
        self.assertIn('"検証"', self._template())

    def test_resend_is_excluded(self) -> None:
        template = self._template()
        self.assertIn('"再送信"', template)
        self.assertIn("submitExcludes", template)

    def test_fallback_uses_the_filled_field(self) -> None:
        # A stale identifier here raised ReferenceError and hid the real cause.
        template = self._template()
        self.assertNotIn("input.dispatchEvent", template)
        self.assertIn("filled.dispatchEvent", template)


class VerificationPageIsNotLoggedInTest(unittest.TestCase):
    def test_identity_verification_page_does_not_pass_the_gate(self) -> None:
        from src.automation.official_sites import _passed_security_code_gate

        self.assertFalse(
            _passed_security_code_gate(
                {
                    "url": (
                        "https://mypage.commufa.jp/join/_ui/identity/"
                        "verification/method/EmailVerificationFinishUi/e"
                    ),
                    "passwordFields": 0,
                    "text": "Myコミュファ ID を検証 確認コード コードを再送信",
                }
            )
        )


class VerificationViewIsNotAnotherChallengeTest(unittest.TestCase):
    """The code page mentions 確認コード, so it must not self-report as other.

    Classifying it as a different challenge ended the run immediately after a
    correct code was submitted.
    """

    def _probe(self) -> str:
        from src.automation import security_challenge as challenge_module

        return challenge_module._COMMUFA_CHALLENGE_PROBE

    def test_no_speculative_challenge_wording_remains(self) -> None:
        """Only evidence-backed challenges may end a run.

        This provider's login second factor is the emailed code, and a CAPTCHA
        is detected from the DOM. Guessed wordings produced false positives
        that killed runs right after a correct code.
        """

        probe = self._probe()
        self.assertNotIn("challengeWords", probe)
        self.assertNotIn("秘密の質問", probe)
        # A CAPTCHA is still detected, from the DOM rather than a guess.
        self.assertIn('kind: "captcha"', probe)

    def test_verification_view_is_recognised_without_its_field(self) -> None:
        probe = self._probe()
        self.assertIn("/identity/verification", probe)
        self.assertIn("onVerificationView", probe)


class CommufaCodeFieldWaitTest(unittest.TestCase):
    """The code field can render after its page URL settles."""

    class _Browser:
        def __init__(self, misses: int) -> None:
            self.misses = misses
            self.calls = 0

        def current_page_target(self):
            return {"url": "https://mypage.commufa.jp/join/_ui/identity/verification/x"}

        def evaluate_current_page(self, expression, timeout=10):
            if "securityCode" in expression:
                self.calls += 1
                if self.calls <= self.misses:
                    return {"ok": False, "error": "FIELD_NOT_FOUND"}
                return {"ok": True, "filled": True}
            return {"ok": True}

    def test_waits_for_a_late_rendering_field(self) -> None:
        from src.automation import security_challenge as challenge_module

        browser = self._Browser(misses=2)
        with patch.object(challenge_module.time, "sleep"):
            challenge_module.submit_commufa_security_code(browser, "123456")

        self.assertEqual(3, browser.calls)

    def test_gives_up_when_the_field_never_appears(self) -> None:
        from src.automation import security_challenge as challenge_module

        browser = self._Browser(misses=10_000)
        clock = [0.0]
        with (
            patch.object(challenge_module.time, "sleep"),
            patch.object(
                challenge_module.time,
                "monotonic",
                side_effect=lambda: clock.__setitem__(0, clock[0] + 5) or clock[0],
            ),
        ):
            with self.assertRaises(
                challenge_module.SecurityChallengeSubmissionError
            ):
                challenge_module.submit_commufa_security_code(browser, "123456")
