"""Ingress administration and token-only pixel viewer HTTP server."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import logging
import os
import signal
import threading
import time
from urllib.parse import urlsplit

from .configuration import Settings, load_settings
from .renderer import run_renderer
from .session import DemoSession


LOGGER = logging.getLogger("demo_display")
PORT = int(os.getenv("INGRESS_PORT", "8099"))
RUNTIME_UID = int(os.getenv("APP_RUNTIME_UID", "2200"))
RUNTIME_GID = int(os.getenv("APP_RUNTIME_GID", "2200"))
SETTINGS = Settings()
SESSION = DemoSession()
TRUSTED_PROXIES = frozenset(
    item.strip()
    for item in os.getenv("TRUSTED_INGRESS_PROXIES", "172.30.32.2").split(",")
    if item.strip()
)
VIEWER_SCRIPT_TEMPLATE = r"""const image=document.getElementById('frame');const state=document.getElementById('state');const refresh=()=>{const next=new Image();next.onload=()=>{image.src=next.src;state.textContent='LIVE • READ ONLY'};next.onerror=()=>{state.textContent='WAITING FOR DISPLAY…'};next.src=location.pathname.replace(/\/$/,'')+'/frame?t='+Date.now()};refresh();setInterval(refresh,__INTERVAL__);"""
ADMIN_SCRIPT = """const copy=document.getElementById('copy-path');if(copy)copy.addEventListener('click',async()=>{await navigator.clipboard.writeText(document.getElementById('display-path').textContent);copy.textContent='Copied'});"""
ADMIN_SCRIPT_HASH = base64.b64encode(sha256(ADMIN_SCRIPT.encode()).digest()).decode()

COMMON_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
ADMIN_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
    f"script-src 'sha256-{ADMIN_SCRIPT_HASH}'; frame-ancestors 'self'; "
    "base-uri 'none'; form-action 'self'"
)


def viewer_script(interval: int) -> str:
    return VIEWER_SCRIPT_TEMPLATE.replace("__INTERVAL__", str(interval * 1000))


def viewer_csp(interval: int) -> str:
    digest = base64.b64encode(sha256(viewer_script(interval).encode()).digest()).decode()
    return (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
        f"script-src 'sha256-{digest}'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    )


VIEWER_CSP = viewer_csp(2)


def trusted_ingress(client_ip: str, ingress_path: str) -> bool:
    return (
        client_ip in TRUSTED_PROXIES
        and ingress_path.startswith("/api/hassio_ingress/")
        and "\r" not in ingress_path
        and "\n" not in ingress_path
    )


def drop_runtime_identity(uid: int = RUNTIME_UID, gid: int = RUNTIME_GID) -> None:
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() == 0 or os.getegid() == 0:
        raise RuntimeError("Could not drop the bootstrap identity")


def display_parts(path: str) -> tuple[str, bool] | None:
    """Return (token, is_frame) for an exact public display route."""
    parts = path.split("/")
    if len(parts) == 3 and parts[:2] == ["", "display"] and parts[2]:
        return parts[2], False
    if len(parts) == 4 and parts[:2] == ["", "display"] and parts[2] and parts[3] == "frame":
        return parts[2], True
    return None


def status_payload(settings: Settings) -> dict:
    width, height = settings.viewport
    current = SESSION.snapshot()
    return {
        "development_tool": True,
        "phase": 6,
        "session": current.state,
        "renderer": "periodic" if current.active else "stopped",
        "chromium_running": current.active,
        "dashboard_path": settings.dashboard_path,
        "viewport": {"width": width, "height": height},
        "capture_interval_seconds": settings.capture_interval,
        "default_session_duration_minutes": settings.default_session_duration,
        "expires_at": current.expires_at,
        "last_frame_at": current.last_frame_at,
        "frame_duration_seconds": current.frame_duration,
        "consecutive_failures": current.consecutive_failures,
        "viewers": current.viewers,
    }


def _age(value: float | None) -> str:
    return "Not yet" if value is None else f"{max(0.0, time.time() - value):.1f} seconds ago"


def status_html(settings: Settings, version: str) -> bytes:
    width, height = settings.viewport
    current = SESSION.snapshot()
    active = current.active
    expires = (
        datetime.fromtimestamp(current.expires_at).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        if current.expires_at
        else "—"
    )
    display_path = f"/display/{escape(current.token)}" if current.token else "—"
    actions = (
        f"<p><label>Display path</label><code id=\"display-path\">{display_path}</code></p>"
        '<p><button id="copy-path" type="button">Copy Display Path</button></p>'
        '<form method="post" action="api/session/end"><button class="danger" type="submit">End Demo Session</button></form>'
        if active
        else '<form method="post" action="api/session/start"><button type="submit">Start Demo Session</button></form>'
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LayerV Demo Display</title><style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif}}body{{margin:0;background:#0b0d10;color:#f4f6f8}}
main{{width:min(800px,calc(100% - 32px));margin:40px auto}}.eyebrow{{color:#f7c948;font-size:.76rem;font-weight:800;letter-spacing:.12em}}
h1{{margin:.5rem 0 1.8rem;font-size:clamp(2rem,6vw,3.5rem)}}.card{{background:#15191f;border:1px solid #2a3038;border-radius:18px;padding:24px}}
.state{{display:flex;gap:12px;align-items:center;margin-bottom:22px}}.dot{{width:12px;height:12px;border-radius:50%;background:{'#31c48d' if active else '#7c8797'}}}
dl{{display:grid;grid-template-columns:minmax(130px,1fr) 2fr;gap:12px}}dt{{color:#9da7b5}}dd{{margin:0;overflow-wrap:anywhere}}
button{{padding:.8rem 1rem;border:0;border-radius:8px;font-weight:800;cursor:pointer}}.danger{{background:#c24141;color:white}}code{{display:block;padding:12px;background:#090b0e;overflow-wrap:anywhere}}
.notice{{margin-top:20px;padding:18px;border-left:3px solid #f7c948;background:#1d1b13;line-height:1.5}}footer{{color:#7f8997;margin-top:22px;font-size:.85rem}}
</style></head><body><main><div class="eyebrow">DEVELOPMENT / DEMONSTRATION TOOL</div><h1>LayerV Demo Display</h1><section class="card">
<div class="state"><span class="dot"></span><strong>Demo Session: {'Active' if active else 'Not running'}</strong></div><dl>
<dt>Renderer</dt><dd>{escape(current.state)}</dd><dt>Chromium</dt><dd>{'Running' if active else 'Not running'}</dd>
<dt>Dashboard</dt><dd>{escape(settings.dashboard_path)}</dd><dt>Resolution</dt><dd>{width} × {height}</dd>
<dt>Refresh</dt><dd>{settings.capture_interval} seconds</dd><dt>Expires</dt><dd>{escape(expires)}</dd>
<dt>Last frame</dt><dd>{escape(_age(current.last_frame_at))}</dd><dt>Capture time</dt><dd>{f'{current.frame_duration:.2f} seconds' if current.frame_duration is not None else '—'}</dd>
<dt>Viewers</dt><dd>{current.viewers}</dd><dt>Failures</dt><dd>{current.consecutive_failures}</dd></dl>{actions}
<div class="notice">The Demo Display sends rendered images of the selected dashboard to anyone possessing the active Demo Session link. End the session when the demonstration is complete.</div>
</section><footer>Version {escape(version)} · Independent from the LayerV Gateway</footer></main><script>{ADMIN_SCRIPT}</script></body></html>"""
    return page.encode()


def viewer_html(interval: int = 2) -> bytes:
    script = viewer_script(interval)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Demo Display</title><style>html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#050608;color:#d7dde5;font-family:system-ui,sans-serif}}
main{{position:relative;width:100%;height:100%;display:grid;place-items:center}}#frame{{display:block;max-width:100%;max-height:100%;object-fit:contain}}
#state{{position:fixed;top:12px;left:14px;padding:7px 10px;border-radius:999px;background:#111820cc;font-size:12px;font-weight:800;letter-spacing:.12em}}</style></head>
<body><main><img id="frame" alt="Live read-only Home Assistant dashboard"><div id="state">STARTING LIVE DISPLAY…</div></main><script>{script}</script></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "LayerVDemoDisplay"
    sys_version = ""

    def log_message(self, format, *args):
        LOGGER.info("HTTP request completed: status=%s", args[1] if len(args) > 1 else "unknown")

    def _send(
        self, status: int, body: bytes, content_type: str, viewer: bool = False, csp: str | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in COMMON_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Security-Policy", csp or (VIEWER_CSP if viewer else ADMIN_CSP))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _viewer(self, path: str) -> bool:
        route = display_parts(path)
        if route is None:
            return False
        token, frame = route
        if not SESSION.valid_token(token):
            self._send(404, b"This demo session has ended.\n", "text/plain; charset=utf-8", True)
            return True
        if not frame:
            self._send(
                200,
                viewer_html(SETTINGS.capture_interval),
                "text/html; charset=utf-8",
                True,
                viewer_csp(SETTINGS.capture_interval),
            )
            return True
        viewer = self.client_address[0]
        if not SESSION.allow_frame_request(viewer):
            self._send(429, b"Too many requests.\n", "text/plain; charset=utf-8", True)
            return True
        image = SESSION.frame_for(token, viewer)
        if image is None:
            self._send(425, b"Starting live dashboard.\n", "text/plain; charset=utf-8", True)
        else:
            self._send(200, image, "image/jpeg", True)
        return True

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if self._viewer(path):
            return
        ingress_path = self.headers.get("X-Ingress-Path", "")
        if not trusted_ingress(self.client_address[0], ingress_path):
            self._send(403, b"Forbidden\n", "text/plain; charset=utf-8")
            return
        if path == "/api/status":
            body = json.dumps(status_payload(SETTINGS), separators=(",", ":")).encode()
            self._send(200, body, "application/json")
        elif path == "/":
            self._send(200, status_html(SETTINGS, os.getenv("APP_VERSION", "development")), "text/html; charset=utf-8")
        else:
            self._send(404, b"Not found\n", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        ingress_path = self.headers.get("X-Ingress-Path", "")
        if not trusted_ingress(self.client_address[0], ingress_path):
            self._send(403, b"Forbidden\n", "text/plain; charset=utf-8")
            return
        if path == "/api/session/start":
            try:
                SESSION.start(
                    SETTINGS.default_session_duration,
                    lambda token: run_renderer(SESSION, SETTINGS, os.getenv("SUPERVISOR_TOKEN", "")),
                )
                LOGGER.info("Demo Session started")
            except RuntimeError:
                self._send(409, b"A Demo Session is already active.\n", "text/plain; charset=utf-8")
                return
        elif path == "/api/session/end":
            SESSION.end("ended")
            LOGGER.info("Demo Session ended")
        else:
            self._send(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        self._send(200, status_html(SETTINGS, os.getenv("APP_VERSION", "development")), "text/html; charset=utf-8")


def main() -> None:
    global SETTINGS
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SETTINGS = load_settings()
    drop_runtime_identity()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    LOGGER.info("LayerV Demo Display started: session=inactive chromium=stopped")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.end("shutdown")
        server.server_close()
        LOGGER.info("LayerV Demo Display stopped")


if __name__ == "__main__":
    main()
