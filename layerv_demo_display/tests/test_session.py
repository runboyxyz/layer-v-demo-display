import threading
import unittest

from app.session import DemoSession


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = DemoSession(self.clock)

    def start(self):
        started = threading.Event()
        token = self.session.start(15, lambda value: started.set())
        started.wait(1)
        return token

    def test_token_is_high_entropy_and_only_one_session_is_allowed(self):
        token = self.start()
        self.assertGreaterEqual(len(token), 40)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            self.session.start(15, lambda value: None)

    def test_invalid_token_and_manual_revocation(self):
        token = self.start()
        self.assertFalse(self.session.valid_token("wrong"))
        self.assertTrue(self.session.valid_token(token))
        self.session.end(join_timeout=0)
        self.assertFalse(self.session.valid_token(token))
        self.assertIsNone(self.session.snapshot().frame)

    def test_expiration_invalidates_token_frame_and_stops_worker(self):
        token = self.start()
        stop_event = self.session.stop_event
        self.session.publish_frame(b"jpeg", 0.2)
        self.clock.now += 15 * 60
        self.assertFalse(self.session.valid_token(token))
        self.assertTrue(stop_event.is_set())
        self.assertIsNone(self.session.snapshot().frame)
        self.assertEqual(self.session.snapshot().state, "expired")

    def test_latest_frame_is_served_without_triggering_renderer(self):
        token = self.start()
        self.assertIsNone(self.session.frame_for(token, "viewer"))
        self.session.publish_frame(b"first", 0.1)
        self.session.publish_frame(b"latest", 0.2)
        self.assertEqual(self.session.frame_for(token, "viewer"), b"latest")
        self.assertEqual(self.session.snapshot().viewers, 1)

    def test_frame_requests_are_rate_limited(self):
        self.start()
        for _ in range(10):
            self.assertTrue(self.session.allow_frame_request("viewer"))
        self.assertFalse(self.session.allow_frame_request("viewer"))
        self.clock.now += 11
        self.assertTrue(self.session.allow_frame_request("viewer"))


if __name__ == "__main__":
    unittest.main()
