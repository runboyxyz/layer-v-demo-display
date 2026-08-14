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
from urllib.parse import parse_qs, urlsplit

from .configuration import Settings, load_settings
from .publication import LayerVPublisher, PublicationError, prepare_storage
from .renderer import run_renderer
from .session import DemoSession
from .verification import (
    VerificationError,
    VerificationGate,
    configure_smtp,
    smtp_configured,
)


LOGGER = logging.getLogger("demo_display")
PORT = int(os.getenv("INGRESS_PORT", "8099"))
RUNTIME_UID = int(os.getenv("APP_RUNTIME_UID", "2200"))
RUNTIME_GID = int(os.getenv("APP_RUNTIME_GID", "2200"))
SETTINGS = Settings()
SESSION = DemoSession()
PUBLISHER: LayerVPublisher | None = None
VERIFICATION = VerificationGate()
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
VERIFICATION_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)


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


def display_parts(path: str) -> tuple[str, str] | None:
    """Return (token, action) for an exact public display route."""
    parts = path.split("/")
    if len(parts) == 3 and parts[:2] == ["", "display"] and parts[2]:
        return parts[2], "view"
    if len(parts) == 4 and parts[:2] == ["", "display"] and parts[2] and parts[3] == "frame":
        return parts[2], "frame"
    if (
        len(parts) == 5
        and parts[:2] == ["", "display"]
        and parts[2]
        and parts[3] == "verify"
        and parts[4] in {"request", "confirm"}
    ):
        return parts[2], f"verify_{parts[4]}"
    return None


def status_payload(settings: Settings) -> dict:
    width, height = settings.viewport
    current = SESSION.snapshot()
    return {
        "development_tool": True,
        "phase": 7,
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


def status_html(settings: Settings, version: str, message: str = "") -> bytes:
    width, height = settings.viewport
    current = SESSION.snapshot()
    active = current.active
    expires = (
        datetime.fromtimestamp(current.expires_at).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        if current.expires_at
        else "—"
    )
    display_path = f"/display/{escape(current.token)}" if current.token else "—"
    publisher = PUBLISHER
    remote_url = publisher.remote_url if publisher else ""
    publication = (
        "Connected" if publisher and publisher.connected else
        "Configured" if publisher and publisher.configured else "Not connected"
    )
    actions = (
        f"<p><label>{'Remote LayerV link' if remote_url else 'Local display path'}</label>"
        f"<code id=\"display-path\">{escape(remote_url or display_path)}</code></p>"
        '<p><button id="copy-path" type="button">Copy Display Link</button></p>'
        '<form method="post" action="api/session/end"><button class="danger" type="submit">End Demo Session</button></form>'
        if active
        else '<form method="post" action="api/session/start">'
        '<label class="check"><input type="checkbox" name="email_verification"> Require email code</label>'
        '<label for="verification-email">Viewer email (required when checked)</label>'
        '<input id="verification-email" name="verification_email" type="email" autocomplete="off">'
        '<button type="submit">Start Demo Session</button></form>'
    )
    connection = "" if publisher and publisher.configured else """
<section class="card secondary"><h2>Connect LayerV</h2><p>Use a dedicated API key with connector bootstrap and qURL read/write scopes.</p>
<form method="post" action="api/layerv/connect"><label for="layerv-key">LayerV API key</label>
<input id="layerv-key" name="api_key" type="password" autocomplete="off" required>
<button type="submit">Connect LayerV</button></form></section>"""
    smtp = f"""<section class="card secondary"><h2>Email-code delivery</h2><p>Status: {'Configured' if smtp_configured() else 'Not configured'}</p>
<form method="post" action="api/smtp/configure"><label>SMTP host</label><input name="smtp_host" required>
<label>SMTP port</label><input name="smtp_port" inputmode="numeric" value="587" required>
<label>Username</label><input name="smtp_username" autocomplete="off"><label>Password</label><input name="smtp_password" type="password" autocomplete="off">
<label>From address</label><input name="smtp_from" type="email" required>
<label class="check"><input name="smtp_starttls" type="checkbox" checked> Use STARTTLS</label>
<button type="submit">Save email settings</button></form></section>"""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LayerV Demo Display</title><style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif}}body{{margin:0;background:#0b0d10;color:#f4f6f8}}
main{{width:min(800px,calc(100% - 32px));margin:40px auto}}.eyebrow{{color:#f7c948;font-size:.76rem;font-weight:800;letter-spacing:.12em}}
h1{{margin:.5rem 0 1.8rem;font-size:clamp(2rem,6vw,3.5rem)}}.card{{background:#15191f;border:1px solid #2a3038;border-radius:18px;padding:24px}}
.secondary{{margin-top:18px}}h2{{margin-top:0}}label{{display:block;color:#9da7b5;margin:14px 0 6px}}input{{box-sizing:border-box;width:100%;padding:.75rem;background:#090b0e;color:white;border:1px solid #39414c;border-radius:8px}}.check{{display:flex;gap:8px;align-items:center;color:#f4f6f8}}.check input{{width:auto}}
.state{{display:flex;gap:12px;align-items:center;margin-bottom:22px}}.dot{{width:12px;height:12px;border-radius:50%;background:{'#31c48d' if active else '#7c8797'}}}
dl{{display:grid;grid-template-columns:minmax(130px,1fr) 2fr;gap:12px}}dt{{color:#9da7b5}}dd{{margin:0;overflow-wrap:anywhere}}
button{{padding:.8rem 1rem;margin-top:14px;border:0;border-radius:8px;font-weight:800;cursor:pointer}}.danger{{background:#c24141;color:white}}code{{display:block;padding:12px;background:#090b0e;overflow-wrap:anywhere}}
.notice{{margin-top:20px;padding:18px;border-left:3px solid #f7c948;background:#1d1b13;line-height:1.5}}footer{{color:#7f8997;margin-top:22px;font-size:.85rem}}
</style></head><body><main><div class="eyebrow">DEVELOPMENT / DEMONSTRATION TOOL</div><h1>LayerV Demo Display</h1><section class="card">
{f'<div class="notice">{escape(message)}</div>' if message else ''}<div class="state"><span class="dot"></span><strong>Demo Session: {'Active' if active else 'Not running'}</strong></div><dl>
<dt>Renderer</dt><dd>{escape(current.state)}</dd><dt>Chromium</dt><dd>{'Running' if active else 'Not running'}</dd>
<dt>LayerV</dt><dd>{publication}</dd><dt>Viewer access</dt><dd>{'Email code required' if VERIFICATION.required else 'Link only'}</dd>
<dt>Dashboard</dt><dd>{escape(settings.dashboard_path)}</dd><dt>Resolution</dt><dd>{width} × {height}</dd>
<dt>Refresh</dt><dd>{settings.capture_interval} seconds</dd><dt>Expires</dt><dd>{escape(expires)}</dd>
<dt>Last frame</dt><dd>{escape(_age(current.last_frame_at))}</dd><dt>Capture time</dt><dd>{f'{current.frame_duration:.2f} seconds' if current.frame_duration is not None else '—'}</dd>
<dt>Viewers</dt><dd>{current.viewers}</dd><dt>Failures</dt><dd>{current.consecutive_failures}</dd></dl>{actions}
<div class="notice">The Demo Display sends rendered images of the selected dashboard to anyone possessing the active Demo Session link. End the session when the demonstration is complete.</div>
</section><footer>Version {escape(version)} · Independent from the LayerV Gateway</footer></main><script>{ADMIN_SCRIPT}</script></body></html>"""
    page = page.replace("</section><footer>", "</section>" + connection + smtp + "<footer>")
    return page.encode()


def verification_html(token: str, code_sent: bool = False, error: str = "") -> bytes:
    action = "confirm" if code_sent else "request"
    field = (
        '<label>Verification code</label><input name="code" inputmode="numeric" pattern="[0-9]{6}" required>'
        if code_sent else '<label>Email address</label><input name="email" type="email" required>'
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Verify Demo Display</title>
<style>:root{{color-scheme:dark;font-family:system-ui}}body{{margin:0;background:#090b10;color:#f4f6f8}}main{{width:min(460px,calc(100% - 32px));margin:12vh auto;background:#15191f;padding:28px;border-radius:18px}}label{{display:block;margin:18px 0 8px}}input{{box-sizing:border-box;width:100%;padding:14px}}button{{margin-top:18px;padding:14px;font-weight:800}}</style></head>
<body><main><h1>Email verification</h1><p>{'Enter the code sent to your email.' if code_sent else 'Enter the authorized email address to receive a six-digit code.'}</p>{f'<p>{escape(error)}</p>' if error else ''}
<form method="post" action="/display/{escape(token)}/verify/{action}">{field}<button type="submit">{'Verify' if code_sent else 'Send code'}</button></form></main></body></html>""".encode()


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
        self, status: int, body: bytes, content_type: str, viewer: bool = False,
        csp: str | None = None, extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in COMMON_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Security-Policy", csp or (VIEWER_CSP if viewer else ADMIN_CSP))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _grant_cookie(self) -> str:
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "demo_verification":
                return value
        return ""

    def _form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request body") from error
        if length < 0 or length > 16_384:
            raise ValueError("Request body is too large")
        values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        return {name: items[-1] for name, items in values.items() if items}

    def _viewer(self, path: str) -> bool:
        route = display_parts(path)
        if route is None:
            return False
        token, action = route
        if action.startswith("verify_"):
            return False
        if not SESSION.valid_token(token):
            self._send(404, b"This demo session has ended.\n", "text/plain; charset=utf-8", True)
            return True
        if VERIFICATION.required and not VERIFICATION.authorized(self._grant_cookie()):
            if action == "view":
                self._send(200, verification_html(token), "text/html; charset=utf-8", True, VERIFICATION_CSP)
            else:
                self._send(403, b"Email verification required.\n", "text/plain; charset=utf-8", True)
            return True
        if action == "view":
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
        self._sync_session()
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
        route = display_parts(path)
        if route is not None and route[1].startswith("verify_"):
            self._verification(route[0], route[1])
            return
        ingress_path = self.headers.get("X-Ingress-Path", "")
        if not trusted_ingress(self.client_address[0], ingress_path):
            self._send(403, b"Forbidden\n", "text/plain; charset=utf-8")
            return
        try:
            form = self._form()
        except (UnicodeDecodeError, ValueError):
            self._send(400, b"Invalid form submission.\n", "text/plain; charset=utf-8")
            return
        message = ""
        if path == "/api/session/start":
            try:
                verification_email = (
                    form.get("verification_email", "")
                    if form.get("email_verification") == "on" else ""
                )
                VERIFICATION.begin(verification_email)
                token = SESSION.start(
                    SETTINGS.default_session_duration,
                    lambda token: run_renderer(SESSION, SETTINGS, os.getenv("SUPERVISOR_TOKEN", "")),
                )
                if PUBLISHER and PUBLISHER.configured:
                    PUBLISHER.publish(token, SETTINGS.default_session_duration)
                LOGGER.info("Demo Session started")
            except (RuntimeError, PublicationError, VerificationError) as error:
                SESSION.end("failed")
                VERIFICATION.end()
                if PUBLISHER:
                    PUBLISHER.revoke()
                self._send(409, status_html(SETTINGS, os.getenv("APP_VERSION", "development"), str(error)), "text/html; charset=utf-8")
                return
        elif path == "/api/session/end":
            SESSION.end("ended")
            VERIFICATION.end()
            if PUBLISHER:
                PUBLISHER.revoke()
            LOGGER.info("Demo Session ended")
        elif path == "/api/layerv/connect":
            try:
                if PUBLISHER is None:
                    raise PublicationError("LayerV publisher is unavailable")
                PUBLISHER.connect(form.get("api_key", ""))
                current = SESSION.snapshot()
                if current.active and current.token and not PUBLISHER.remote_url:
                    remaining = max(1, int((current.expires_at - time.time()) / 60))
                    PUBLISHER.publish(current.token, remaining)
                message = "LayerV connected successfully."
            except PublicationError as error:
                message = str(error)
        elif path == "/api/smtp/configure":
            try:
                configure_smtp(form)
                message = "Email-code delivery settings saved."
            except VerificationError as error:
                message = str(error)
        else:
            self._send(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        self._send(200, status_html(SETTINGS, os.getenv("APP_VERSION", "development"), message), "text/html; charset=utf-8")

    def _verification(self, token: str, action: str) -> None:
        if not SESSION.valid_token(token) or not VERIFICATION.required:
            self._send(404, b"This demo session has ended.\n", "text/plain; charset=utf-8", True)
            return
        try:
            form = self._form()
            if action == "verify_request":
                VERIFICATION.request_code(form.get("email", ""))
                self._send(200, verification_html(token, True), "text/html; charset=utf-8", True, VERIFICATION_CSP)
                return
            snapshot = SESSION.snapshot()
            grant = VERIFICATION.confirm(form.get("code", ""), snapshot.expires_at or time.time())
            self._send(
                303, b"", "text/plain; charset=utf-8", True,
                extra_headers={
                    "Location": f"/display/{token}",
                    "Set-Cookie": f"demo_verification={grant}; Path=/display/{token}; HttpOnly; Secure; SameSite=Strict",
                },
            )
        except (UnicodeDecodeError, ValueError, VerificationError) as error:
            self._send(400, verification_html(token, action == "verify_confirm", str(error)), "text/html; charset=utf-8", True, VERIFICATION_CSP)

    def _sync_session(self) -> None:
        current = SESSION.snapshot()
        if not current.active and PUBLISHER and PUBLISHER.remote_url:
            VERIFICATION.end()
            PUBLISHER.revoke()


def main() -> None:
    global SETTINGS, PUBLISHER
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SETTINGS = load_settings()
    prepare_storage(RUNTIME_UID, RUNTIME_GID)
    drop_runtime_identity()
    PUBLISHER = LayerVPublisher()
    PUBLISHER.start_connector()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    LOGGER.info("LayerV Demo Display started: session=inactive chromium=stopped")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.end("shutdown")
        VERIFICATION.end()
        PUBLISHER.close()
        server.server_close()
        LOGGER.info("LayerV Demo Display stopped")


if __name__ == "__main__":
    main()
