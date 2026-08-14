"""Single in-memory Demo Session lifecycle and viewer accounting."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import secrets
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class SessionSnapshot:
    active: bool
    state: str
    token: str | None
    expires_at: float | None
    frame: bytes | None
    last_frame_at: float | None
    frame_duration: float | None
    consecutive_failures: int
    viewers: int


class DemoSession:
    """Own exactly one temporary token, renderer worker, and current frame."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._token: str | None = None
        self._expires_at: float | None = None
        self._state = "inactive"
        self._frame: bytes | None = None
        self._last_frame_at: float | None = None
        self._frame_duration: float | None = None
        self._failures = 0
        self._viewers: dict[str, float] = {}
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

    def start(self, duration_minutes: int, worker_target: Callable[[str], None]) -> str:
        with self._lock:
            self._expire_locked()
            if self._token is not None:
                raise RuntimeError("A Demo Session is already active")
            token = secrets.token_urlsafe(32)
            self._token = token
            self._expires_at = self._clock() + duration_minutes * 60
            self._state = "starting"
            self._frame = None
            self._last_frame_at = None
            self._frame_duration = None
            self._failures = 0
            self._viewers.clear()
            self._requests.clear()
            self.stop_event = threading.Event()
            self.worker = threading.Thread(
                target=worker_target, args=(token,), name="demo-renderer", daemon=True
            )
            self.worker.start()
            return token

    def end(self, reason: str = "ended", join_timeout: float = 10.0) -> None:
        with self._lock:
            worker = self.worker
            self._invalidate_locked(reason)
        if worker is not None and worker is not threading.current_thread():
            worker.join(join_timeout)

    def renderer_started(self) -> None:
        with self._lock:
            if self._token is not None:
                self._state = "running"

    def publish_frame(self, frame: bytes, duration: float) -> None:
        with self._lock:
            if self._token is None or self.stop_event.is_set():
                return
            self._frame = frame
            self._last_frame_at = self._clock()
            self._frame_duration = duration
            self._failures = 0
            self._state = "running"

    def renderer_failure(self) -> int:
        with self._lock:
            self._failures += 1
            self._state = "unhealthy"
            return self._failures

    def renderer_stopped(self, failed: bool = False) -> None:
        with self._lock:
            if self._token is not None:
                self._invalidate_locked("failed" if failed else "ended")

    def valid_token(self, token: str) -> bool:
        with self._lock:
            self._expire_locked()
            return self._token is not None and secrets.compare_digest(self._token, token)

    def frame_for(self, token: str, viewer: str) -> bytes | None:
        with self._lock:
            self._expire_locked()
            if self._token is None or not secrets.compare_digest(self._token, token):
                return None
            self._viewers[viewer] = self._clock()
            return self._frame

    def allow_frame_request(self, viewer: str, limit: int = 10, window: int = 10) -> bool:
        with self._lock:
            now = self._clock()
            requests = self._requests[viewer]
            while requests and requests[0] <= now - window:
                requests.popleft()
            if len(requests) >= limit:
                return False
            requests.append(now)
            return True

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            self._expire_locked()
            now = self._clock()
            self._viewers = {key: seen for key, seen in self._viewers.items() if seen > now - 10}
            return SessionSnapshot(
                active=self._token is not None,
                state=self._state,
                token=self._token,
                expires_at=self._expires_at,
                frame=self._frame,
                last_frame_at=self._last_frame_at,
                frame_duration=self._frame_duration,
                consecutive_failures=self._failures,
                viewers=len(self._viewers),
            )

    def expired(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._token is None and self._state == "expired"

    def _expire_locked(self) -> None:
        if self._token is not None and self._expires_at is not None and self._clock() >= self._expires_at:
            self._invalidate_locked("expired")

    def _invalidate_locked(self, state: str) -> None:
        self.stop_event.set()
        self._token = None
        self._expires_at = None
        self._frame = None
        self._last_frame_at = None
        self._frame_duration = None
        self._viewers.clear()
        self._requests.clear()
        self._state = state
