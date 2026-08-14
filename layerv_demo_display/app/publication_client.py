"""Small Unix-socket client for the isolated LayerV publisher process."""

from __future__ import annotations

import json
import os
import socket

from .publication import PublicationError


SOCKET_PATH = os.getenv("PUBLICATION_SOCKET", "/run/layerv-demo/publication.sock")


class LayerVPublisher:
    """Expose the existing publisher API without sharing its process or files."""

    def _call(self, operation: str, **values):
        request = json.dumps({"operation": operation, **values}, separators=(",", ":")).encode()
        if len(request) > 8192:
            raise PublicationError("LayerV request is too large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(65)
                connection.connect(SOCKET_PATH)
                connection.sendall(request + b"\n")
                response = b""
                while not response.endswith(b"\n") and len(response) <= 16384:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            result = json.loads(response)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise PublicationError("LayerV publisher is unavailable") from error
        if not isinstance(result, dict) or not result.get("ok"):
            message = result.get("error") if isinstance(result, dict) else None
            raise PublicationError(str(message or "LayerV publisher request failed")[:300])
        return result.get("value")

    @property
    def configured(self) -> bool:
        return bool(self._call("status").get("configured"))

    @property
    def connected(self) -> bool:
        return bool(self._call("status").get("connected"))

    @property
    def remote_url(self) -> str:
        return str(self._call("status").get("remote_url") or "")

    @property
    def activation_url(self) -> str:
        return str(self._call("status").get("activation_url") or "")

    @property
    def publications(self) -> list[dict[str, str]]:
        value = self._call("status").get("publications")
        return value if isinstance(value, list) else []

    def connect(self, api_key: str) -> None:
        self._call("connect", api_key=api_key)

    def start_connector(self) -> None:
        # The isolated broker owns connector startup and recovery.
        self._call("status")

    def publish(self, display_token: str, lifetime_minutes: int) -> dict[str, str]:
        value = self._call(
            "publish", display_token=display_token, lifetime_minutes=lifetime_minutes
        )
        if not isinstance(value, dict):
            raise PublicationError("LayerV returned invalid publication data")
        return {str(key): str(item) for key, item in value.items()}

    def revoke(self, publication_id: str = "") -> None:
        self._call("revoke", publication_id=publication_id)

    def close(self) -> None:
        try:
            self.revoke()
        except PublicationError:
            pass
