from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.jobs.inbox import (  # noqa: E402
    ChallengeAlreadyAnsweredError,
    ChallengeExpiredError,
    ChallengeOwnerMismatchError,
    ChallengeResponseInbox,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class ChallengeResponseInboxTest(unittest.TestCase):
    def test_response_is_single_submit_and_single_consume(self) -> None:
        inbox = ChallengeResponseInbox()
        inbox.register(challenge_id="challenge-1", owner_id="owner-1", ttl_seconds=30)
        inbox.submit(challenge_id="challenge-1", owner_id="owner-1", response="654321")
        with self.assertRaises(ChallengeAlreadyAnsweredError):
            inbox.submit(challenge_id="challenge-1", owner_id="owner-1", response="111111")

        self.assertEqual(inbox.wait_and_consume("challenge-1", timeout_seconds=1), "654321")
        self.assertFalse(inbox.is_pending("challenge-1"))

    def test_wrong_owner_cannot_submit(self) -> None:
        inbox = ChallengeResponseInbox()
        inbox.register(challenge_id="challenge-1", owner_id="owner-1", ttl_seconds=30)
        with self.assertRaises(ChallengeOwnerMismatchError):
            inbox.submit(challenge_id="challenge-1", owner_id="owner-2", response="654321")
        self.assertTrue(inbox.is_pending("challenge-1"))

    def test_expired_response_is_rejected(self) -> None:
        clock = MutableClock()
        inbox = ChallengeResponseInbox(monotonic=clock)
        inbox.register(challenge_id="challenge-1", owner_id="owner-1", ttl_seconds=10)
        clock.value = 111
        with self.assertRaises(ChallengeExpiredError):
            inbox.submit(challenge_id="challenge-1", owner_id="owner-1", response="654321")

    def test_waiter_receives_response_without_polling_database(self) -> None:
        inbox = ChallengeResponseInbox()
        inbox.register(challenge_id="challenge-1", owner_id="owner-1", ttl_seconds=30)
        received: list[str | None] = []

        waiter = threading.Thread(
            target=lambda: received.append(
                inbox.wait_and_consume("challenge-1", timeout_seconds=2)
            )
        )
        waiter.start()
        inbox.submit(challenge_id="challenge-1", owner_id="owner-1", response="123456")
        waiter.join(timeout=3)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(received, ["123456"])

    def test_discard_wakes_waiter_without_returning_secret(self) -> None:
        inbox = ChallengeResponseInbox()
        inbox.register(challenge_id="challenge-1", owner_id="owner-1", ttl_seconds=30)
        received: list[str | None] = []
        waiter = threading.Thread(
            target=lambda: received.append(
                inbox.wait_and_consume("challenge-1", timeout_seconds=2)
            )
        )
        waiter.start()
        self.assertTrue(inbox.discard("challenge-1"))
        waiter.join(timeout=3)
        self.assertEqual(received, [None])


if __name__ == "__main__":
    unittest.main()

