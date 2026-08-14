"""Optional in-memory email-code gate for the pixel viewer."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from hashlib import sha256
from html import escape
import hmac
import json
import os
from pathlib import Path
import secrets
import smtplib
import ssl
import threading
import time
from urllib.parse import urlsplit


SMTP_FILE = Path(os.getenv("APP_DATA_DIR", "/data")) / "secrets" / "smtp.json"


class VerificationError(RuntimeError):
    """Safe error for administrator or viewer display."""


def secure_smtp_storage() -> None:
    """Enforce the mode after the server is running as the directory owner."""
    SMTP_FILE.parent.chmod(0o700)


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def configure_smtp(values: dict[str, str]) -> None:
    host = values.get("smtp_host", "").strip()
    sender = values.get("smtp_from", "").strip()
    username = values.get("smtp_username", "").strip()
    password = values.get("smtp_password", "")
    try:
        port = int(values.get("smtp_port", "587"))
    except ValueError as error:
        raise VerificationError("SMTP port must be a number") from error
    if not host or len(host) > 253 or not sender or "@" not in sender:
        raise VerificationError("Enter a valid SMTP host and sender address")
    if not 1 <= port <= 65535 or len(username) > 320 or len(password) > 4096:
        raise VerificationError("SMTP settings are invalid")
    _atomic_json(SMTP_FILE, {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from": sender,
        "starttls": values.get("smtp_starttls") == "on",
    })


def smtp_configured() -> bool:
    return SMTP_FILE.is_file()


def _smtp_settings() -> dict:
    try:
        value = json.loads(SMTP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError("Email delivery is not configured") from error
    if not isinstance(value, dict):
        raise VerificationError("Email delivery configuration is invalid")
    return value


@dataclass
class PendingCode:
    digest: bytes
    expires_at: float
    attempts: int = 0


class VerificationGate:
    """Require a short code before serving any frame bytes."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._recipient = ""
        self._pending: PendingCode | None = None
        self._grants: dict[str, float] = {}
        self._last_delivery = 0.0

    @property
    def required(self) -> bool:
        with self._lock:
            return bool(self._recipient)

    def begin(self, recipient: str = "") -> None:
        email = recipient.strip().lower()
        if email and (len(email) > 320 or "@" not in email or not smtp_configured()):
            raise VerificationError("Configure SMTP and enter a valid verification email")
        with self._lock:
            self._recipient = email
            self._pending = None
            self._grants.clear()
            self._last_delivery = 0.0

    def end(self) -> None:
        with self._lock:
            self._recipient = ""
            self._pending = None
            self._grants.clear()

    def ensure_code(self) -> None:
        """Send the first code without exposing or asking for the recipient."""
        with self._lock:
            pending = self._pending
            if pending is not None and pending.expires_at > self._clock() and pending.attempts < 5:
                return
        self.request_code()

    def request_code(self) -> None:
        with self._lock:
            recipient = self._recipient
        if not recipient:
            raise VerificationError("Email verification is not enabled")
        with self._lock:
            if self._last_delivery > self._clock() - 60:
                raise VerificationError("Wait before requesting another verification code")
            self._last_delivery = self._clock()
        code = f"{secrets.randbelow(1_000_000):06d}"
        digest = sha256(code.encode("ascii")).digest()
        message = EmailMessage()
        message["Subject"] = "Your LayerV Demo Display code"
        message.set_content(f"Your verification code is {code}. It expires in 10 minutes.")
        self._send_message(message, recipient)
        with self._lock:
            self._pending = PendingCode(digest, self._clock() + 600)

    def send_invitation(self, activation_url: str, display_url: str) -> None:
        """Send the two-step LayerV invitation to the configured recipient."""
        with self._lock:
            recipient = self._recipient
        if not recipient:
            return
        for value in (activation_url, display_url):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname:
                raise VerificationError("LayerV invitation links are unavailable")
        safe_activation = escape(activation_url, quote=True)
        safe_display = escape(display_url, quote=True)
        button = (
            "display:inline-block;padding:13px 18px;border-radius:10px;"
            "background:#1769d2;color:#ffffff;text-decoration:none;"
            "font-size:15px;font-weight:700"
        )
        text = (
            "You have been invited to a temporary LayerV Demo Display.\n\n"
            f"1. Activate LayerV access:\n{activation_url}\n\n"
            f"2. Open the Demo Display:\n{display_url}\n\n"
            "Opening the display sends a separate one-time verification code to this address.\n"
        )
        html = f"""<!doctype html><html lang="en"><body style="margin:0;padding:28px 12px;background:#f3f6fa;color:#172033;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#fff;border:1px solid #dfe6ef;border-radius:18px;overflow:hidden"><tr><td style="padding:24px 30px;background:#10233f;color:#fff"><div style="font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#9fc7ff">LayerV Demo Display</div><h1 style="margin:8px 0 0;font-size:25px">Your demo invitation</h1></td></tr><tr><td style="padding:30px"><p style="margin:0 0 24px;font-size:16px;line-height:1.6">Complete both steps below to view the temporary read-only display.</p><div style="margin:0 0 24px;padding:20px;border:1px solid #dfe6ef;border-radius:12px"><strong>Step 1 — Activate LayerV access</strong><p><a href="{safe_activation}" style="{button}">Activate LayerV Access</a></p><div style="font-size:11px;overflow-wrap:anywhere">{safe_activation}</div></div><div style="margin:0 0 24px;padding:20px;border:1px solid #dfe6ef;border-radius:12px"><strong>Step 2 — Open the read-only display</strong><p><a href="{safe_display}" style="{button}">Open Demo Display</a></p><div style="font-size:11px;overflow-wrap:anywhere">{safe_display}</div></div><p style="color:#66758a;font-size:13px;line-height:1.6">Opening the display sends a separate one-time verification code to this email address.</p></td></tr></table></td></tr></table></body></html>"""
        message = EmailMessage()
        message["Subject"] = "Your LayerV Demo Display invitation"
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        self._send_message(message, recipient)

    def _send_message(self, message: EmailMessage, recipient: str) -> None:
        settings = _smtp_settings()
        message["From"] = settings["from"]
        message["To"] = recipient
        try:
            with smtplib.SMTP(settings["host"], int(settings["port"]), timeout=20) as client:
                if settings.get("starttls"):
                    client.starttls(context=ssl.create_default_context())
                if settings.get("username"):
                    client.login(settings["username"], settings.get("password", ""))
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            with self._lock:
                self._last_delivery = 0.0
            raise VerificationError("The verification email could not be sent") from error

    def confirm(self, code: str, session_expires_at: float) -> str:
        with self._lock:
            pending = self._pending
            if pending is None or pending.expires_at <= self._clock() or pending.attempts >= 5:
                raise VerificationError("Request a new verification code")
            pending.attempts += 1
            supplied = sha256(code.strip().encode("ascii", errors="ignore")).digest()
            if not hmac.compare_digest(pending.digest, supplied):
                raise VerificationError("The verification code is incorrect")
            grant = secrets.token_urlsafe(32)
            self._grants[grant] = min(session_expires_at, self._clock() + 3600)
            self._pending = None
            return grant

    def authorized(self, grant: str) -> bool:
        if not self.required:
            return True
        with self._lock:
            now = self._clock()
            self._grants = {key: expiry for key, expiry in self._grants.items() if expiry > now}
            return any(hmac.compare_digest(key, grant) for key in self._grants)
