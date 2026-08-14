"""Ingress administration and token-only pixel viewer HTTP server."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import logging
import os
import secrets
import signal
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .configuration import Settings, load_settings
from .publication import PublicationError
from .publication_client import LayerVPublisher
from .renderer import run_renderer
from .session import DemoSession
from .verification import (
    VerificationError,
    VerificationGate,
    configure_smtp,
    secure_smtp_storage,
    smtp_configured,
)


LOGGER = logging.getLogger("demo_display")
PORT = int(os.getenv("INGRESS_PORT", "8099"))
SETTINGS = Settings()
SESSION = DemoSession()
PUBLISHER: LayerVPublisher | None = None
@dataclass
class Invitation:
    id: str
    token: str
    email: str
    verification: VerificationGate
    publication_id: str
    activation_url: str
    remote_url: str


INVITATIONS: dict[str, Invitation] = {}
INVITATIONS_LOCK = threading.RLock()
ADMIN_NOTICE = ""
ADMIN_NOTICE_LOCK = threading.Lock()
TRUSTED_PROXIES = frozenset(
    item.strip()
    for item in os.getenv("TRUSTED_INGRESS_PROXIES", "172.30.32.2").split(",")
    if item.strip()
)
VIEWER_SCRIPT_TEMPLATE = r"""const image=document.getElementById('frame');const video=document.getElementById('video');const state=document.getElementById('state');const base=location.pathname.replace(/\/$/,'');let fallback=false;const refresh=()=>{const next=new Image();next.onload=()=>{image.src=next.src;state.textContent='LIVE • READ ONLY'};next.onerror=()=>{state.textContent='WAITING FOR DISPLAY…'};next.src=base+'/frame?t='+Date.now()};const polling=()=>{if(fallback)return;fallback=true;video.pause();video.hidden=true;image.hidden=false;image.onerror=()=>{refresh();setInterval(refresh,__INTERVAL__)};image.src=base+'/stream'};const liveEdge=()=>{if(video.buffered.length){const end=video.buffered.end(video.buffered.length-1);if(end-video.currentTime>.65)video.currentTime=Math.max(0,end-.15)}};if(__VIDEO__){video.hidden=false;image.hidden=true;video.onplaying=()=>{state.textContent='LIVE • READ ONLY'};video.onprogress=liveEdge;video.ontimeupdate=liveEdge;video.onerror=polling;video.src=base+'/video';video.play().catch(polling);setTimeout(()=>{if(video.readyState===0)polling()},12000)}else polling();"""
ADMIN_SCRIPT = """document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent);button.textContent='Copied'}));"""
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


def viewer_script(interval: int, video: bool = False) -> str:
    return VIEWER_SCRIPT_TEMPLATE.replace("__INTERVAL__", str(interval * 1000)).replace("__VIDEO__", "true" if video else "false")


def viewer_csp(interval: int, video: bool = False) -> str:
    digest = base64.b64encode(sha256(viewer_script(interval, video).encode()).digest()).decode()
    return (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; "
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


def set_admin_notice(value: str) -> None:
    global ADMIN_NOTICE
    with ADMIN_NOTICE_LOCK:
        ADMIN_NOTICE = value[:500]


def take_admin_notice() -> str:
    global ADMIN_NOTICE
    with ADMIN_NOTICE_LOCK:
        value = ADMIN_NOTICE
        ADMIN_NOTICE = ""
        return value


def invitation_for(token: str) -> Invitation | None:
    with INVITATIONS_LOCK:
        return next(
            (item for key, item in INVITATIONS.items() if secrets.compare_digest(key, token)),
            None,
        )


def clear_invitations() -> None:
    with INVITATIONS_LOCK:
        values = list(INVITATIONS.values())
        INVITATIONS.clear()
    for item in values:
        item.verification.end()


def invitation_notice(kind: str, email: str, sent: bool) -> str:
    if not email:
        return f"{kind} invitation created."
    if sent:
        return f"{kind} invitation created and emailed."
    return f"{kind} invitation created, but email delivery failed; use the links below."


def display_parts(path: str) -> tuple[str, str] | None:
    """Return (token, action) for an exact public display route."""
    parts = path.split("/")
    if len(parts) == 3 and parts[:2] == ["", "display"] and parts[2]:
        return parts[2], "view"
    if len(parts) == 4 and parts[:2] == ["", "display"] and parts[2] and parts[3] == "frame":
        return parts[2], "frame"
    if len(parts) == 4 and parts[:2] == ["", "display"] and parts[2] and parts[3] == "stream":
        return parts[2], "stream"
    if len(parts) == 4 and parts[:2] == ["", "display"] and parts[2] and parts[3] == "video":
        return parts[2], "video"
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
    performance = SESSION.performance()
    return {
        "development_tool": True,
        "phase": 7,
        "session": current.state,
        "renderer": "periodic" if current.active else "stopped",
        "renderer_mode": settings.renderer_mode,
        "chromium_running": current.active,
        "dashboard_path": settings.dashboard_path,
        "viewport": {"width": width, "height": height},
        "fallback_refresh_interval_seconds": settings.capture_interval,
        "default_session_duration_minutes": settings.default_session_duration,
        "expires_at": current.expires_at,
        "last_frame_at": current.last_frame_at,
        "frame_duration_seconds": current.frame_duration,
        "consecutive_failures": current.consecutive_failures,
        "viewers": current.viewers,
        "current_fps": round(performance["fps"], 2),
        "average_source_frame_bytes": round(performance["average_source_frame_bytes"]),
        "encoded_bitrate_bps": round(performance["encoded_bitrate_bps"]),
    }


def _age(value: float | None) -> str:
    return "Not yet" if value is None else f"{max(0.0, time.time() - value):.1f} seconds ago"


def status_html(settings: Settings, version: str, message: str = "") -> bytes:
    width, height = settings.viewport
    current = SESSION.snapshot()
    active = current.active
    performance = SESSION.performance()
    expires = (
        datetime.fromtimestamp(current.expires_at).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        if current.expires_at
        else "—"
    )
    publisher = PUBLISHER
    publication = (
        "Connected" if publisher and publisher.connected else
        "Configured" if publisher and publisher.configured else "Not connected"
    )
    with INVITATIONS_LOCK:
        invitations = list(INVITATIONS.values())
    invitation_cards = "".join(
        f'<section class="secondary"><h3>{escape(item.email or "Link-only viewer")}</h3>'
        f'<p>{"Email code required" if item.verification.required else "Link only"}</p>'
        f'<p><a href="{escape(item.activation_url)}" target="_blank" rel="noopener noreferrer">Open Activation qURL</a> '
        f'<a href="{escape(item.remote_url)}" target="_blank" rel="noopener noreferrer">Open Demo Display</a></p>'
        f'<form method="post" action="api/invitations/revoke"><input type="hidden" name="invitation_id" value="{escape(item.id)}"><button class="danger" type="submit">Revoke this viewer</button></form></section>'
        for item in invitations
    )
    actions = (
        invitation_cards
        + '<section class="secondary"><h3>Invite another viewer</h3><form method="post" action="api/invitations/create">'
        '<label>Viewer email (optional)</label><input name="verification_email" type="email" autocomplete="off">'
        '<label class="check"><input type="checkbox" name="email_verification"> Require email code</label>'
        '<button type="submit">Send invitation</button></form></section>'
        + '<form method="post" action="api/session/end"><button class="danger" type="submit">End Demo Session &amp; Revoke All</button></form>'
        if active
        else '<form method="post" action="api/session/start">'
        '<label class="check"><input type="checkbox" name="email_verification"> Require email code</label>'
        '<label for="verification-email">Viewer email (optional; entering one sends the links)</label>'
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
button,a{{padding:.8rem 1rem;margin-top:14px;border:0;border-radius:8px;font-weight:800;cursor:pointer}}a{{display:inline-block;background:#f7c948;color:#111;text-decoration:none}}.danger{{background:#c24141;color:white}}code{{display:block;padding:12px;background:#090b0e;overflow-wrap:anywhere}}
.notice{{margin-top:20px;padding:18px;border-left:3px solid #f7c948;background:#1d1b13;line-height:1.5}}footer{{color:#7f8997;margin-top:22px;font-size:.85rem}}
</style></head><body><main><div class="eyebrow">DEVELOPMENT / DEMONSTRATION TOOL</div><h1>LayerV Demo Display</h1><section class="card">
{f'<div class="notice">{escape(message)}</div>' if message else ''}<div class="state"><span class="dot"></span><strong>Demo Session: {'Active' if active else 'Not running'}</strong></div><dl>
<dt>Renderer</dt><dd>{escape(current.state)}</dd><dt>Chromium</dt><dd>{'Running' if active else 'Not running'}</dd>
<dt>LayerV</dt><dd>{publication}</dd><dt>Invitations</dt><dd>{len(invitations)}</dd>
<dt>Dashboard</dt><dd>{escape(settings.dashboard_path)}</dd><dt>Resolution</dt><dd>{width} × {height}</dd>
<dt>Renderer mode</dt><dd>{escape(settings.renderer_mode)}</dd><dt>Target FPS</dt><dd>{settings.renderer_target_fps if settings.renderer_mode == 'video' else 'JPEG adaptive'}</dd>
<dt>Current FPS</dt><dd>{performance['fps']:.1f}</dd><dt>Encoded bitrate</dt><dd>{performance['encoded_bitrate_bps'] / 1_000_000:.2f} Mbps</dd>
<dt>Refresh</dt><dd>Live stream (1-second fallback)</dd><dt>Expires</dt><dd>{escape(expires)}</dd>
<dt>Last frame</dt><dd>{escape(_age(current.last_frame_at))}</dd><dt>Capture time</dt><dd>{f'{current.frame_duration:.2f} seconds' if current.frame_duration is not None else '—'}</dd>
<dt>Viewers</dt><dd>{current.viewers}</dd><dt>Failures</dt><dd>{current.consecutive_failures}</dd></dl>{actions}
<div class="notice">The Demo Display sends rendered images of the selected dashboard to anyone possessing the active Demo Session link. End the session when the demonstration is complete.</div>
</section><footer>Version {escape(version)} · Independent from the LayerV Gateway</footer></main><script>{ADMIN_SCRIPT}</script></body></html>"""
    page = page.replace("</section><footer>", "</section>" + connection + smtp + "<footer>")
    return page.encode()


def verification_html(token: str, error: str = "") -> bytes:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Verify Demo Display</title>
<style>:root{{color-scheme:dark;font-family:system-ui}}body{{margin:0;background:#090b10;color:#f4f6f8}}main{{width:min(460px,calc(100% - 32px));margin:12vh auto;background:#15191f;padding:28px;border-radius:18px}}label{{display:block;margin:18px 0 8px}}input{{box-sizing:border-box;width:100%;padding:14px}}button{{margin-top:18px;padding:14px;font-weight:800}}</style></head>
<body><main><h1>Email verification</h1><p>Enter the six-digit code sent to the authorized email address.</p>{f'<p>{escape(error)}</p>' if error else ''}
<form method="post" action="/display/{escape(token)}/verify/confirm"><label>Verification code</label><input name="code" inputmode="numeric" pattern="[0-9]{{6}}" autocomplete="one-time-code" required><button type="submit">Verify</button></form>
<form method="post" action="/display/{escape(token)}/verify/request"><button type="submit">Send a new code</button></form></main></body></html>""".encode()


def viewer_html(interval: int = 2, video: bool = False) -> bytes:
    script = viewer_script(interval, video)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Demo Display</title><style>html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#050608;color:#d7dde5;font-family:system-ui,sans-serif}}
main{{position:relative;width:100%;height:100%;display:grid;place-items:center}}#frame,#video{{display:block;max-width:100%;max-height:100%;object-fit:contain}}
#state{{position:fixed;top:12px;left:14px;padding:7px 10px;border-radius:999px;background:#111820cc;font-size:12px;font-weight:800;letter-spacing:.12em}}</style></head>
<body><main><video id="video" autoplay muted playsinline hidden></video><img id="frame" alt="Live read-only Home Assistant dashboard"><div id="state">STARTING LIVE DISPLAY…</div></main><script>{script}</script></body></html>""".encode()


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
        invitation = invitation_for(token)
        if invitation is None:
            self._send(404, b"This invitation has been revoked.\n", "text/plain; charset=utf-8", True)
            return True
        gate = invitation.verification
        if gate.required and not gate.authorized(self._grant_cookie()):
            if action == "view":
                try:
                    gate.ensure_code()
                    error = ""
                except VerificationError as delivery_error:
                    error = str(delivery_error)
                self._send(200, verification_html(token, error), "text/html; charset=utf-8", True, VERIFICATION_CSP)
            else:
                self._send(403, b"Email verification required.\n", "text/plain; charset=utf-8", True)
            return True
        if action == "view":
            self._send(
                200,
                viewer_html(SETTINGS.capture_interval, SETTINGS.renderer_mode == "video"),
                "text/html; charset=utf-8",
                True,
                viewer_csp(SETTINGS.capture_interval, SETTINGS.renderer_mode == "video"),
            )
            return True
        viewer = self.client_address[0]
        if action == "video":
            self._video_stream(token, viewer)
            return True
        if action == "stream":
            self._stream(token, viewer)
            return True
        if not SESSION.allow_frame_request(viewer):
            self._send(429, b"Too many requests.\n", "text/plain; charset=utf-8", True)
            return True
        image = SESSION.frame_for(token, viewer)
        if image is None:
            self._send(425, b"Starting live dashboard.\n", "text/plain; charset=utf-8", True)
        else:
            self._send(200, image, "image/jpeg", True)
        return True

    def _video_stream(self, token: str, viewer: str) -> None:
        if SETTINGS.renderer_mode != "video":
            self._send(404, b"Video mode is not active.\n", "text/plain; charset=utf-8", True)
            return
        stream_id = secrets.token_urlsafe(12)
        if not SESSION.open_stream(token, stream_id):
            self._send(429, b"Too many live viewers.\n", "text/plain; charset=utf-8", True)
            return
        LOGGER.info("Experimental video viewer connected")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            for name, value in COMMON_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            sequence = 0
            sent_init = False
            while SESSION.valid_token(token):
                initial, sequence, fragment = SESSION.wait_for_video(token, sequence)
                if initial is not None and not sent_init:
                    self.wfile.write(initial)
                    sent_init = True
                if fragment is not None:
                    self.wfile.write(fragment)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            SESSION.close_stream(stream_id)

    def _stream(self, token: str, viewer: str) -> None:
        stream_id = secrets.token_urlsafe(12)
        if not SESSION.open_stream(token, stream_id):
            self._send(429, b"Too many live viewers.\n", "text/plain; charset=utf-8", True)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            for name, value in COMMON_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Security-Policy", VIEWER_CSP)
            self.end_headers()
            sequence = -1
            while SESSION.valid_token(token):
                sequence, image = SESSION.wait_for_frame(token, viewer, sequence)
                if image is None:
                    continue
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(image)).encode()
                    + b"\r\n\r\n"
                    + image
                    + b"\r\n"
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            SESSION.close_stream(stream_id)

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
            self._send(
                200,
                status_html(
                    SETTINGS,
                    os.getenv("APP_VERSION", "development"),
                    take_admin_notice(),
                ),
                "text/html; charset=utf-8",
            )
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
            if SESSION.snapshot().active:
                set_admin_notice("A Demo Session is already active.")
                self._redirect_admin()
                return
            try:
                email = form.get("verification_email", "").strip()
                require_code = form.get("email_verification") == "on"
                if require_code and not email:
                    raise VerificationError("Enter an email when verification is required")
                if email and not (PUBLISHER and PUBLISHER.configured):
                    raise PublicationError(
                        "Connect LayerV before starting an emailed Demo Session"
                    )
                token = SESSION.start(
                    SETTINGS.default_session_duration,
                    lambda token: run_renderer(SESSION, SETTINGS, os.getenv("SUPERVISOR_TOKEN", "")),
                )
                sent = self._create_invitation(token, email, require_code)
                message = invitation_notice("Demo", email, sent)
                LOGGER.info("Demo Session started")
            except (RuntimeError, PublicationError, VerificationError) as error:
                SESSION.end("failed")
                clear_invitations()
                if PUBLISHER:
                    PUBLISHER.revoke()
                set_admin_notice(str(error))
                self._redirect_admin()
                return
        elif path == "/api/session/end":
            SESSION.end("ended")
            clear_invitations()
            if PUBLISHER:
                PUBLISHER.revoke()
            LOGGER.info("Demo Session ended")
        elif path == "/api/invitations/create":
            try:
                email = form.get("verification_email", "").strip()
                require_code = form.get("email_verification") == "on"
                if require_code and not email:
                    raise VerificationError("Enter an email when verification is required")
                token = SESSION.issue_viewer_token()
                sent = self._create_invitation(token, email, require_code)
                message = invitation_notice("Viewer", email, sent)
            except (RuntimeError, PublicationError, VerificationError) as error:
                if 'token' in locals():
                    SESSION.revoke_viewer_token(token)
                message = str(error)
        elif path == "/api/invitations/revoke":
            invitation_id = form.get("invitation_id", "")
            with INVITATIONS_LOCK:
                invitation = next((item for item in INVITATIONS.values() if secrets.compare_digest(item.id, invitation_id)), None)
                if invitation:
                    INVITATIONS.pop(invitation.token, None)
            if invitation:
                SESSION.revoke_viewer_token(invitation.token)
                invitation.verification.end()
                if PUBLISHER:
                    PUBLISHER.revoke(invitation.publication_id)
                message = "Viewer invitation revoked."
            else:
                message = "Viewer invitation was already revoked."
        elif path == "/api/layerv/connect":
            try:
                if PUBLISHER is None:
                    raise PublicationError("LayerV publisher is unavailable")
                PUBLISHER.connect(form.get("api_key", ""))
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
        if message:
            set_admin_notice(message)
        self._redirect_admin()

    def _create_invitation(self, token: str, email: str, require_code: bool) -> bool:
        with INVITATIONS_LOCK:
            if len(INVITATIONS) >= 20:
                raise PublicationError("A Demo Session supports at most 20 viewer invitations")
        if PUBLISHER is None or not PUBLISHER.configured:
            raise PublicationError("Connect LayerV before creating an invitation")
        gate = VerificationGate()
        gate.begin(email, required=require_code)
        current = SESSION.snapshot()
        remaining = max(1, int(((current.expires_at or time.time()) - time.time()) / 60))
        published = PUBLISHER.publish(token, remaining)
        if not all(published.get(key) for key in ("id", "activation_url", "remote_url")):
            raise PublicationError("LayerV returned incomplete invitation data")
        invitation = Invitation(
            id=secrets.token_urlsafe(12), token=token, email=email,
            verification=gate, publication_id=published["id"],
            activation_url=published["activation_url"], remote_url=published["remote_url"],
        )
        with INVITATIONS_LOCK:
            INVITATIONS[token] = invitation
        if email:
            try:
                gate.send_invitation(invitation.activation_url, invitation.remote_url)
            except VerificationError:
                # Keep the independently revocable invitation available for manual sharing.
                set_admin_notice("Invitation email failed; links remain available below.")
                return False
            return True
        return False

    def _redirect_admin(self) -> None:
        # Every administrative action is exactly two path components beneath
        # the Ingress root. Returning there prevents later relative forms from
        # nesting under the previous action URL.
        self._send(
            303,
            b"",
            "text/plain; charset=utf-8",
            extra_headers={"Location": "../../"},
        )

    def _verification(self, token: str, action: str) -> None:
        invitation = invitation_for(token)
        if not SESSION.valid_token(token) or invitation is None or not invitation.verification.required:
            self._send(404, b"This demo session has ended.\n", "text/plain; charset=utf-8", True)
            return
        try:
            form = self._form()
            if action == "verify_request":
                invitation.verification.request_code()
                self._send(200, verification_html(token), "text/html; charset=utf-8", True, VERIFICATION_CSP)
                return
            snapshot = SESSION.snapshot()
            grant = invitation.verification.confirm(form.get("code", ""), snapshot.expires_at or time.time())
            self._send(
                303, b"", "text/plain; charset=utf-8", True,
                extra_headers={
                    "Location": f"/display/{token}",
                    "Set-Cookie": f"demo_verification={grant}; Path=/display/{token}; HttpOnly; Secure; SameSite=Strict",
                },
            )
        except (UnicodeDecodeError, ValueError, VerificationError) as error:
            self._send(400, verification_html(token, str(error)), "text/html; charset=utf-8", True, VERIFICATION_CSP)

    def _sync_session(self) -> None:
        current = SESSION.snapshot()
        if not current.active and PUBLISHER and PUBLISHER.remote_url:
            clear_invitations()
            PUBLISHER.revoke()


def main() -> None:
    global SETTINGS, PUBLISHER
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SETTINGS = load_settings()
    if os.geteuid() == 0:
        raise RuntimeError("HTTP server must be launched by the runtime supervisor")
    secure_smtp_storage()
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
        clear_invitations()
        PUBLISHER.close()
        server.server_close()
        LOGGER.info("LayerV Demo Display stopped")


if __name__ == "__main__":
    main()
