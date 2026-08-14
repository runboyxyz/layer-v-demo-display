"""Optional in-memory email-code gate for the pixel viewer."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets
import smtplib
import ssl
import threading
import time


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

    def request_code(self, supplied_email: str) -> None:
        with self._lock:
            recipient = self._recipient
        if not recipient or not hmac.compare_digest(recipient, supplied_email.strip().lower()):
            raise VerificationError("The email address could not be verified")
        with self._lock:
            if self._last_delivery > self._clock() - 60:
                raise VerificationError("Wait before requesting another verification code")
            self._last_delivery = self._clock()
        code = f"{secrets.randbelow(1_000_000):06d}"
        digest = sha256(code.encode("ascii")).digest()
        settings = _smtp_settings()
        message = EmailMessage()
        message["Subject"] = "Your LayerV Demo Display code"
        message["From"] = settings["from"]
        message["To"] = recipient
        message.set_content(f"Your verification code is {code}. It expires in 10 minutes.")
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
        with self._lock:
            self._pending = PendingCode(digest, self._clock() + 600)

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
