"""Read and validate the small Phase 1 App configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


OPTIONS_FILE = Path(os.getenv("APP_OPTIONS_FILE", "/data/options.json"))
RESOLUTIONS = {"1280x720": (1280, 720), "1920x1080": (1920, 1080)}
VIDEO_RESOLUTIONS = {"960x540": (960, 540), "1280x720": (1280, 720)}
DURATIONS = {15, 30, 60, 120}


class ConfigurationError(ValueError):
    """Raised when an App option violates the renderer security boundary."""


@dataclass(frozen=True)
class Settings:
    dashboard_path: str = "/demo-home/home"
    ha_frontend_port: int = 80
    resolution: str = "1920x1080"
    renderer_mode: str = "jpeg"
    video_resolution: str = "960x540"
    renderer_target_fps: int = 5
    capture_interval: int = 1
    default_session_duration: int = 60
    hide_ha_sidebar: bool = True
    hide_ha_header: bool = True

    @property
    def viewport(self) -> tuple[int, int]:
        return RESOLUTIONS[self.resolution]

    @property
    def video_viewport(self) -> tuple[int, int]:
        return VIDEO_RESOLUTIONS[self.video_resolution]

    @property
    def ha_origin(self) -> str:
        return f"http://homeassistant:{self.ha_frontend_port}"


def validate_dashboard_path(value: object) -> str:
    """Return a canonical local HA path, rejecting URLs and traversal."""
    if not isinstance(value, str):
        raise ConfigurationError("Dashboard path must be text")
    path = value.strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or "\\" in path
        or any(ord(character) < 32 for character in path)
    ):
        raise ConfigurationError("Dashboard path must be a local absolute path")
    parts = PurePosixPath(path).parts
    if any(part in {".", ".."} for part in parts):
        raise ConfigurationError("Dashboard path cannot contain traversal")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if len(path) > 256:
        raise ConfigurationError("Dashboard path is too long")
    return path


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be true or false")
    return value


def parse_settings(value: object) -> Settings:
    if not isinstance(value, dict):
        raise ConfigurationError("App options must be an object")
    resolution = value.get("resolution", "1920x1080")
    if resolution not in RESOLUTIONS:
        raise ConfigurationError("Resolution must be 1280x720 or 1920x1080")
    ha_frontend_port = value.get("ha_frontend_port", 80)
    if isinstance(ha_frontend_port, str) and ha_frontend_port.isdigit():
        ha_frontend_port = int(ha_frontend_port)
    if ha_frontend_port not in {80, 8123}:
        raise ConfigurationError("Home Assistant frontend port must be 80 or 8123")
    renderer_mode = value.get("renderer_mode", "jpeg")
    if renderer_mode not in {"jpeg", "video"}:
        raise ConfigurationError("Renderer mode must be jpeg or video")
    video_resolution = value.get("video_resolution", "960x540")
    if video_resolution not in VIDEO_RESOLUTIONS:
        raise ConfigurationError("Video resolution must be 960x540 or 1280x720")
    renderer_target_fps = value.get("renderer_target_fps", 5)
    if isinstance(renderer_target_fps, str) and renderer_target_fps.isdigit():
        renderer_target_fps = int(renderer_target_fps)
    if renderer_target_fps not in {5, 10}:
        raise ConfigurationError("Renderer target FPS must be 5 or 10")
    interval = value.get("capture_interval", 1)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 10:
        raise ConfigurationError("Capture interval must be from 1 to 10 seconds")
    duration = value.get("default_session_duration", 60)
    if isinstance(duration, bool) or duration not in DURATIONS:
        raise ConfigurationError("Session duration must be 15, 30, 60, or 120 minutes")
    return Settings(
        dashboard_path=validate_dashboard_path(value.get("dashboard_path", "/demo-home/home")),
        ha_frontend_port=ha_frontend_port,
        resolution=resolution,
        renderer_mode=renderer_mode,
        video_resolution=video_resolution,
        renderer_target_fps=renderer_target_fps,
        capture_interval=interval,
        default_session_duration=duration,
        hide_ha_sidebar=_boolean(value.get("hide_ha_sidebar", True), "Hide sidebar"),
        hide_ha_header=_boolean(value.get("hide_ha_header", True), "Hide header"),
    )


def load_settings(path: Path = OPTIONS_FILE) -> Settings:
    runtime_value = os.getenv("APP_SETTINGS_JSON")
    if runtime_value is not None:
        try:
            return parse_settings(json.loads(runtime_value))
        except (json.JSONDecodeError, ConfigurationError) as error:
            raise ConfigurationError("Could not read validated runtime settings") from error
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("Could not read App options") from error
    return parse_settings(content)
