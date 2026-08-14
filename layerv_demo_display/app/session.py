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
        self._frame_ready = threading.Condition(self._lock)
        self._token: str | None = None
        self._viewer_tokens: set[str] = set()
        self._expires_at: float | None = None
        self._state = "inactive"
        self._frame: bytes | None = None
        self._last_frame_at: float | None = None
        self._frame_duration: float | None = None
        self._failures = 0
        self._viewers: dict[str, float] = {}
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._streams: set[str] = set()
        self._frame_sequence = 0
        self._video_init: bytes | None = None
        self._video_fragments: deque[tuple[int, bytes]] = deque(maxlen=4)
        self._video_sequence = 0
        self._frame_samples: deque[tuple[float, int]] = deque(maxlen=600)
        self._video_samples: deque[tuple[float, int]] = deque(maxlen=300)
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

    def start(self, duration_minutes: int, worker_target: Callable[[str], None]) -> str:
        with self._lock:
            self._expire_locked()
            if self._token is not None:
                raise RuntimeError("A Demo Session is already active")
            token = secrets.token_urlsafe(32)
            self._token = token
            self._viewer_tokens = {token}
            self._expires_at = self._clock() + duration_minutes * 60
            self._state = "starting"
            self._frame = None
            self._last_frame_at = None
            self._frame_duration = None
            self._failures = 0
            self._viewers.clear()
            self._requests.clear()
            self._streams.clear()
            self._frame_sequence = 0
            self._video_init = None
            self._video_fragments.clear()
            self._video_sequence = 0
            self._frame_samples.clear()
            self._video_samples.clear()
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
            self._frame_samples.append((self._clock(), len(frame)))
            self._failures = 0
            self._state = "running"
            self._frame_sequence += 1
            self._frame_ready.notify_all()

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
            return self._valid_token_locked(token)

    def issue_viewer_token(self) -> str:
        with self._lock:
            self._expire_locked()
            if self._token is None:
                raise RuntimeError("A Demo Session is not active")
            token = secrets.token_urlsafe(32)
            self._viewer_tokens.add(token)
            return token

    def revoke_viewer_token(self, token: str) -> bool:
        with self._lock:
            matched = next(
                (value for value in self._viewer_tokens if secrets.compare_digest(value, token)),
                None,
            )
            if matched is None:
                return False
            self._viewer_tokens.remove(matched)
            self._frame_ready.notify_all()
            return True

    def _valid_token_locked(self, token: str) -> bool:
        return self._token is not None and any(
            secrets.compare_digest(value, token) for value in self._viewer_tokens
        )

    def frame_for(self, token: str, viewer: str) -> bytes | None:
        with self._lock:
            self._expire_locked()
            if not self._valid_token_locked(token):
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

    def open_stream(self, token: str, stream_id: str, limit: int = 4) -> bool:
        with self._lock:
            self._expire_locked()
            if (
                self._token is None
                or not self._valid_token_locked(token)
                or len(self._streams) >= limit
            ):
                return False
            self._streams.add(stream_id)
            return True

    def close_stream(self, stream_id: str) -> None:
        with self._lock:
            self._streams.discard(stream_id)

    def wait_for_frame(
        self, token: str, viewer: str, after_sequence: int, timeout: float = 5.0
    ) -> tuple[int, bytes | None]:
        with self._frame_ready:
            deadline = time.monotonic() + timeout
            while self._frame_sequence <= after_sequence:
                self._expire_locked()
                if not self._valid_token_locked(token):
                    return self._frame_sequence, None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._frame_sequence, None
                self._frame_ready.wait(remaining)
            self._viewers[viewer] = self._clock()
            return self._frame_sequence, self._frame

    def publish_video_init(self, value: bytes) -> None:
        with self._frame_ready:
            if self._token is not None and not self.stop_event.is_set():
                self._video_init = value
                self._frame_ready.notify_all()

    def publish_video_fragment(self, value: bytes) -> None:
        with self._frame_ready:
            if self._token is None or self.stop_event.is_set():
                return
            self._video_sequence += 1
            self._video_fragments.append((self._video_sequence, value))
            self._video_samples.append((self._clock(), len(value)))
            self._frame_ready.notify_all()

    def wait_for_video(
        self, token: str, after_sequence: int, timeout: float = 5.0
    ) -> tuple[bytes | None, int, bytes | None]:
        with self._frame_ready:
            deadline = time.monotonic() + timeout
            while self._video_init is None or self._video_sequence <= after_sequence:
                self._expire_locked()
                if not self._valid_token_locked(token):
                    return None, self._video_sequence, None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._video_init, self._video_sequence, None
                self._frame_ready.wait(remaining)
            fragment = self._video_fragments[-1][1] if self._video_fragments else None
            return self._video_init, self._video_sequence, fragment

    def performance(self, window: float = 30.0) -> dict[str, float]:
        with self._lock:
            cutoff = self._clock() - window
            frames = [(stamp, size) for stamp, size in self._frame_samples if stamp > cutoff]
            video = [(stamp, size) for stamp, size in self._video_samples if stamp > cutoff]
            elapsed = max(1.0, min(window, self._clock() - frames[0][0])) if frames else window
            return {
                "fps": len(frames) / elapsed,
                "average_source_frame_bytes": (
                    sum(size for _, size in frames) / len(frames) if frames else 0.0
                ),
                "encoded_bitrate_bps": sum(size for _, size in video) * 8 / elapsed,
            }

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
        self._viewer_tokens.clear()
        self._expires_at = None
        self._frame = None
        self._last_frame_at = None
        self._frame_duration = None
        self._viewers.clear()
        self._requests.clear()
        self._streams.clear()
        self._video_init = None
        self._video_fragments.clear()
        self._frame_samples.clear()
        self._video_samples.clear()
        self._state = state
        self._frame_ready.notify_all()
