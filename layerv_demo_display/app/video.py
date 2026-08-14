"""Bounded fragmented-MP4 parsing for the experimental H.264 renderer."""

from __future__ import annotations

import struct


class FragmentedMP4:
    """Split an FFmpeg fMP4 byte stream into one init segment and media fragments."""

    def __init__(self):
        self._buffer = bytearray()
        self._initial = bytearray()
        self._fragment = bytearray()
        self.init_segment: bytes | None = None

    def feed(self, value: bytes) -> list[bytes]:
        self._buffer.extend(value)
        completed: list[bytes] = []
        while len(self._buffer) >= 8:
            size = struct.unpack(">I", self._buffer[:4])[0]
            header = 8
            if size == 1:
                if len(self._buffer) < 16:
                    break
                size = struct.unpack(">Q", self._buffer[8:16])[0]
                header = 16
            if size == 0 or size < header or len(self._buffer) < size:
                break
            box = bytes(self._buffer[:size])
            del self._buffer[:size]
            kind = box[4:8]
            if self.init_segment is None:
                self._initial.extend(box)
                if kind == b"moov":
                    self.init_segment = bytes(self._initial)
            else:
                if kind == b"moof" and self._fragment:
                    completed.append(bytes(self._fragment))
                    self._fragment.clear()
                self._fragment.extend(box)
                if kind == b"mdat":
                    completed.append(bytes(self._fragment))
                    self._fragment.clear()
        return completed


def ffmpeg_command(width: int, height: int, fps: int) -> list[str]:
    """Return a fixed, low-latency software H.264 command with no shell expansion."""
    keyframe_interval = max(1, fps // 2)
    return [
        "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps), "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-bf", "0", "-refs", "1",
        "-pix_fmt", "yuv420p", "-vf", f"scale={width}:{height}:in_range=pc:out_range=tv",
        "-g", str(keyframe_interval), "-keyint_min", str(keyframe_interval), "-sc_threshold", "0",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets", "1",
        "-f", "mp4", "pipe:1",
    ]
