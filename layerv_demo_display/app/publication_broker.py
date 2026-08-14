"""LayerV publisher broker running as its own unprivileged identity."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import socketserver

from .publication import LayerVPublisher, PublicationError, secure_storage_modes


SOCKET_PATH = Path(os.getenv("PUBLICATION_SOCKET", "/run/layerv-demo/publication.sock"))
PUBLISHER = LayerVPublisher()


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(8193)
        if not raw.endswith(b"\n") or len(raw) > 8192:
            self._reply({"ok": False, "error": "Invalid publisher request"})
            return
        try:
            request = json.loads(raw)
            operation = request.get("operation")
            if operation == "status":
                value = {
                    "configured": PUBLISHER.configured,
                    "connected": PUBLISHER.connected,
                    "remote_url": PUBLISHER.remote_url,
                    "activation_url": PUBLISHER.activation_url,
                    "publications": PUBLISHER.publications,
                }
            elif operation == "connect":
                PUBLISHER.connect(str(request.get("api_key") or ""))
                value = None
            elif operation == "publish":
                token = str(request.get("display_token") or "")
                lifetime = int(request.get("lifetime_minutes") or 0)
                if not 1 <= lifetime <= 120:
                    raise PublicationError("Invalid publication lifetime")
                value = PUBLISHER.publish(token, lifetime)
            elif operation == "revoke":
                PUBLISHER.revoke(str(request.get("publication_id") or ""))
                value = None
            else:
                raise PublicationError("Unknown publisher request")
            self._reply({"ok": True, "value": value})
        except (PublicationError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._reply({"ok": False, "error": str(error)[:300]})
        except OSError:
            self._reply({"ok": False, "error": "LayerV publisher storage is unavailable"})

    def _reply(self, value: dict) -> None:
        self.wfile.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")


class Server(socketserver.UnixStreamServer):
    """Serialize publication changes and their in-memory qURL state."""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    secure_storage_modes()
    SOCKET_PATH.unlink(missing_ok=True)
    server = Server(str(SOCKET_PATH), Handler)
    SOCKET_PATH.chmod(0o660)
    PUBLISHER.start_connector()
    signal.signal(
        signal.SIGTERM,
        lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        PUBLISHER.close()
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
