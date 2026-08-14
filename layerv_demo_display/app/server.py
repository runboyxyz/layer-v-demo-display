"""Phase 1 Ingress-only status server; no renderer exists in this build."""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import threading
from urllib.parse import urlsplit

from .configuration import ConfigurationError, Settings, load_settings
from .renderer_probe import ProbeResult, run_probe


LOGGER = logging.getLogger("demo_display")
PORT = int(os.getenv("INGRESS_PORT", "8099"))
RUNTIME_UID = int(os.getenv("APP_RUNTIME_UID", "2200"))
RUNTIME_GID = int(os.getenv("APP_RUNTIME_GID", "2200"))
SETTINGS = Settings()
PROBE = ProbeResult("not_run", "Authentication probe has not run")
PROBE_LOCK = threading.Lock()
TRUSTED_PROXIES = frozenset(
    item.strip()
    for item in os.getenv("TRUSTED_INGRESS_PROXIES", "172.30.32.2").split(",")
    if item.strip()
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "img-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def trusted_ingress(client_ip: str, ingress_path: str) -> bool:
    return (
        client_ip in TRUSTED_PROXIES
        and ingress_path.startswith("/api/hassio_ingress/")
        and "\r" not in ingress_path
        and "\n" not in ingress_path
    )


def drop_runtime_identity(uid: int = RUNTIME_UID, gid: int = RUNTIME_GID) -> None:
    """Permanently drop the root bootstrap identity before serving HTTP."""
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() == 0 or os.getegid() == 0:
        raise RuntimeError("Could not drop the bootstrap identity")


def status_payload(settings: Settings) -> dict:
    width, height = settings.viewport
    return {
        "development_tool": True,
        "phase": 1,
        "session": "inactive",
        "renderer": "probe_only",
        "chromium_running": False,
        "dashboard_path": settings.dashboard_path,
        "viewport": {"width": width, "height": height},
        "capture_interval_seconds": settings.capture_interval,
        "default_session_duration_minutes": settings.default_session_duration,
        "authentication_probe": PROBE.outcome,
    }


def status_html(settings: Settings, version: str) -> bytes:
    width, height = settings.viewport
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LayerV Demo Display</title><style>
:root {{ color-scheme: dark; font-family: system-ui,sans-serif; }}
body {{ margin:0; background:#0b0d10; color:#f4f6f8; }}
main {{ width:min(760px,calc(100% - 32px)); margin:40px auto; }}
.eyebrow {{ color:#f7c948; font-size:.76rem; font-weight:800; letter-spacing:.12em; }}
h1 {{ margin:.5rem 0 1.8rem; font-size:clamp(2rem,6vw,3.5rem); }}
.card {{ background:#15191f; border:1px solid #2a3038; border-radius:18px; padding:24px; }}
.state {{ display:flex; gap:12px; align-items:center; margin-bottom:22px; }}
.dot {{ width:12px; height:12px; border-radius:50%; background:#7c8797; }}
dl {{ display:grid; grid-template-columns:minmax(130px,1fr) 2fr; gap:12px; }}
dt {{ color:#9da7b5; }} dd {{ margin:0; overflow-wrap:anywhere; }}
.notice {{ margin-top:20px; padding:18px; border-left:3px solid #f7c948; background:#1d1b13; line-height:1.5; }}
footer {{ color:#7f8997; margin-top:22px; font-size:.85rem; }}
</style></head><body><main>
<div class="eyebrow">DEVELOPMENT / DEMONSTRATION TOOL</div>
<h1>LayerV Demo Display</h1><section class="card">
<div class="state"><span class="dot"></span><strong>Demo Session: Not running</strong></div>
<dl><dt>Phase</dt><dd>1 — App packaging and status UI</dd>
<dt>Renderer</dt><dd>One-shot authentication probe only</dd><dt>Chromium</dt><dd>Not running</dd>
<dt>Dashboard</dt><dd>{escape(settings.dashboard_path)}</dd>
<dt>Resolution</dt><dd>{width} × {height}</dd>
<dt>Future refresh</dt><dd>{settings.capture_interval} seconds</dd>
<dt>Future duration</dt><dd>{settings.default_session_duration} minutes</dd>
<dt>Authentication probe</dt><dd>{escape(PROBE.outcome)} — {escape(PROBE.detail)}</dd></dl>
<form method="post" action="api/probe"><button type="submit">Run one-shot authentication probe</button></form>
{"<p><img src='api/probe-frame' alt='Authenticated dashboard probe' style='max-width:100%'></p>" if PROBE.frame else ""}
<div class="notice">This experimental App will send rendered images of the selected dashboard to anyone possessing a future active Demo Session link. Phase 1 cannot start or publish a session.</div>
</section><footer>Version {escape(version)} · Independent from the LayerV Gateway</footer>
</main></body></html>"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LayerVDemoDisplay"
    sys_version = ""

    def log_message(self, format, *args):
        # Ingress paths can contain authentication material.
        LOGGER.info("Ingress request completed: status=%s", args[1] if len(args) > 1 else "unknown")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ingress_path = self.headers.get("X-Ingress-Path", "")
        if not trusted_ingress(self.client_address[0], ingress_path):
            self._send(403, b"Forbidden\n", "text/plain; charset=utf-8")
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        settings = SETTINGS
        if path == "/api/status":
            body = json.dumps(status_payload(settings), separators=(",", ":")).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/api/probe-frame" and PROBE.frame:
            self._send(200, PROBE.frame, "image/jpeg")
            return
        if path == "/":
            self._send(200, status_html(settings, os.getenv("APP_VERSION", "development")), "text/html; charset=utf-8")
            return
        self._send(404, b"Not found\n", "text/plain; charset=utf-8")

    def do_POST(self):
        global PROBE
        ingress_path = self.headers.get("X-Ingress-Path", "")
        if not trusted_ingress(self.client_address[0], ingress_path):
            self._send(403, b"Forbidden\n", "text/plain; charset=utf-8")
            return
        if (urlsplit(self.path).path.rstrip("/") or "/") != "/api/probe":
            self._send(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        if not PROBE_LOCK.acquire(blocking=False):
            self._send(409, b"A probe is already running.\n", "text/plain; charset=utf-8")
            return
        try:
            PROBE = ProbeResult("running", "Starting isolated Chromium")
            PROBE = run_probe(SETTINGS, os.getenv("SUPERVISOR_TOKEN", ""))
        finally:
            PROBE_LOCK.release()
        body = status_html(SETTINGS, os.getenv("APP_VERSION", "development"))
        self._send(200, body, "text/html; charset=utf-8")


def main() -> None:
    global SETTINGS
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Supervisor owns /data/options.json as root. Read it once, then discard
    # root before the network listener is created. App option changes already
    # require the normal Home Assistant App restart.
    SETTINGS = load_settings()
    drop_runtime_identity()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOGGER.info("LayerV Demo Display Phase 1 started: renderer=absent session=inactive")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        LOGGER.info("LayerV Demo Display stopped")


if __name__ == "__main__":
    main()
