"""Independent LayerV connector and temporary qURL publication."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess  # nosec B404 - fixed executable and arguments only
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid


DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/data"))
SECRET_FILE = DATA_DIR / "connector-secrets" / "layerv-api-key"
CONNECTOR_CONFIG = DATA_DIR / "connector-config-v2" / "qurl-proxy.yaml"
CONNECTOR_STATE = DATA_DIR / "connector-state-v2"
CONNECTOR_LOGS = DATA_DIR / "connector-logs-v2"
INSTALLATION_FILE = CONNECTOR_STATE / "installation-id"
LAYERV_API = os.getenv("LAYERV_API_BASE_URL", "https://api.layerv.ai").rstrip("/")
RESOURCE_PATTERN = re.compile(r"(?m)^\s*resource_id:\s*[\"']?([^#\s\"']+)")
URL_PATTERN = re.compile(r"(?:https?|wss?)://\S+", re.IGNORECASE)
SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,}")
LOGGER = logging.getLogger("demo_display.publication")


class PublicationError(RuntimeError):
    """Safe administrator-facing publication failure."""


def safe_connector_message(value: str) -> str:
    """Bound and redact untrusted connector diagnostics before app logging."""
    message = URL_PATTERN.sub("[url]", value.strip())
    message = SECRET_PATTERN.sub("[identifier]", message)
    return message[:300]


def secure_storage_modes() -> None:
    """Set directory modes after the supervisor assigns their owner."""
    for path in (SECRET_FILE.parent, CONNECTOR_CONFIG.parent, CONNECTOR_STATE, CONNECTOR_LOGS):
        path.chmod(0o700)


def _atomic_write(path: Path, value: str, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


class LayerVPublisher:
    """Own the connector and independently revocable temporary qURLs."""

    def __init__(self):
        self._lock = threading.RLock()
        self._connector: subprocess.Popen | None = None
        self._resource_id = self._read_resource_id()
        self._publications: dict[str, dict[str, str]] = {}

    @property
    def configured(self) -> bool:
        return SECRET_FILE.is_file() and bool(self._resource_id)

    @property
    def remote_url(self) -> str:
        with self._lock:
            return next(iter(self._publications.values()), {}).get("remote_url", "")

    @property
    def activation_url(self) -> str:
        with self._lock:
            return next(iter(self._publications.values()), {}).get("activation_url", "")

    @property
    def publications(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(value) for value in self._publications.values()]

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connector is not None and self._connector.poll() is None

    def _read_resource_id(self) -> str:
        try:
            content = CONNECTOR_CONFIG.read_text(encoding="utf-8")
        except OSError:
            return ""
        match = RESOURCE_PATTERN.search(content)
        return match.group(1) if match else ""

    def _connector_id(self) -> str:
        if INSTALLATION_FILE.exists():
            value = INSTALLATION_FILE.read_text(encoding="utf-8").strip()
        else:
            value = str(uuid.uuid4())
            _atomic_write(INSTALLATION_FILE, value + "\n")
        try:
            normalized = str(uuid.UUID(value)).replace("-", "")[:16]
        except ValueError as error:
            raise PublicationError("Saved LayerV installation identity is invalid") from error
        return f"ha-demo-display-{normalized}"

    def connect(self, api_key: str) -> None:
        key = str(api_key).strip()
        if len(key) < 10 or len(key) > 4096 or any(char.isspace() for char in key):
            raise PublicationError("Enter a valid LayerV API key")
        _atomic_write(SECRET_FILE, key + "\n")
        if not self._resource_id:
            # Resolve and persist all fixed registration inputs before the
            # subprocess exception boundary. Filesystem setup failures must
            # never be mislabeled as connector exec denials.
            connector_id = self._connector_id()
            environment = self._environment(include_key=True)
            try:
                completed = subprocess.run(  # nosec B603
                    [
                        "/usr/local/bin/qurl-connector", "-c", str(CONNECTOR_CONFIG),
                        "add", "--target", "http://127.0.0.1:8099",
                        "--id", connector_id, "--no-verify",
                    ],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                self._resource_id = self._read_resource_id()
                if self._resource_id:
                    LOGGER.info(
                        "Connector registration recovered: stage=bootstrap category=route_config_present"
                    )
                    self.start_connector()
                    return
                LOGGER.warning(
                    "Connector registration failed: stage=bootstrap category=timeout"
                )
                raise PublicationError(
                    "LayerV connector registration timed out before creating a route"
                ) from error
            except PermissionError as error:
                LOGGER.warning(
                    "Connector registration failed: stage=launch category=permission_denied"
                )
                raise PublicationError(
                    "LayerV connector could not launch under the App security profile"
                ) from error
            except OSError as error:
                LOGGER.warning(
                    "Connector registration failed: stage=launch category=process_error"
                )
                raise PublicationError("LayerV connector registration could not launch") from error
            self._resource_id = self._read_resource_id()
            if completed.returncode != 0 and not self._resource_id:
                LOGGER.warning(
                    "Connector registration failed: stage=registration exit_code=%s",
                    completed.returncode,
                )
                SECRET_FILE.unlink(missing_ok=True)
                raise PublicationError("LayerV rejected connector registration")
            if not self._resource_id:
                raise PublicationError("LayerV registration returned no resource identity")
            diagnostic = safe_connector_message(completed.stderr or "")
            if diagnostic:
                LOGGER.info("Connector registration diagnostic: %s", diagnostic)
        self.start_connector()

    def _environment(self, include_key: bool = False) -> dict[str, str]:
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(CONNECTOR_STATE),
            "LANG": "C.UTF-8",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "QURL_CONNECTOR_ID": self._connector_id(),
            "LAYERV_AGENT_STATE_DIR": str(CONNECTOR_STATE),
        }
        if include_key:
            environment["QURL_API_KEY_FILE"] = str(SECRET_FILE)
        return environment

    def _state_complete(self) -> bool:
        required = (
            "agent_id", "private_key", "public_key", "tunnel_identities.json",
            "etc/config.toml", "etc/server.toml",
        )
        return all((CONNECTOR_STATE / relative).is_file() for relative in required)

    def start_connector(self) -> None:
        with self._lock:
            if not self.configured or self.connected:
                return
            self._connector = subprocess.Popen(  # nosec B603
                ["/usr/local/bin/qurl-connector", "-c", str(CONNECTOR_CONFIG), "run"],
                env=self._environment(include_key=not self._state_complete()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            threading.Thread(
                target=self._monitor_connector,
                args=(self._connector,),
                name="connector-diagnostics",
                daemon=True,
            ).start()

    @staticmethod
    def _monitor_connector(connector: subprocess.Popen) -> None:
        if connector.stdout is not None:
            for line in connector.stdout:
                diagnostic = safe_connector_message(line)
                if diagnostic:
                    LOGGER.info("Connector diagnostic: %s", diagnostic)
        LOGGER.warning("Connector stopped: exit_code=%s", connector.wait())

    def publish(self, display_token: str, lifetime_minutes: int) -> dict[str, str]:
        if not self.configured:
            raise PublicationError("Connect LayerV before publishing the display")
        self.start_connector()
        payload = json.dumps({
            "expires_in": f"{lifetime_minutes}m",
            "label": "Home Assistant Demo Display",
        }).encode("utf-8")
        result = self._request("POST", f"/v1/resources/{quote(self._resource_id, safe='')}/qurls", payload)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise PublicationError("LayerV returned invalid qURL data")
        site = str(data.get("qurl_site") or "").rstrip("/")
        activation = str(data.get("qurl_link") or data.get("qurl") or "").strip()
        qurl_id = str(data.get("qurl_id") or data.get("qurl_display_id") or data.get("id") or "")
        if not site or not activation or not qurl_id:
            raise PublicationError("LayerV returned an incomplete qURL")
        with self._lock:
            publication_id = secrets.token_urlsafe(12)
            value = {
                "id": publication_id,
                "qurl_id": qurl_id,
                "activation_url": activation,
                "remote_url": f"{site}/display/{display_token}",
            }
            self._publications[publication_id] = value
            return dict(value)

    def revoke(self, publication_id: str = "") -> None:
        with self._lock:
            if publication_id:
                values = [self._publications.pop(publication_id, {})]
            else:
                values = list(self._publications.values())
                self._publications.clear()
        if not self._resource_id or not SECRET_FILE.is_file():
            return
        for value in values:
            qurl_id = value.get("qurl_id", "")
            if qurl_id:
                try:
                    self._request(
                        "DELETE",
                        f"/v1/resources/{quote(self._resource_id, safe='')}/qurls/{quote(qurl_id, safe='')}",
                    )
                except PublicationError:
                    continue

    def _request(self, method: str, path: str, body: bytes | None = None) -> dict:
        try:
            key = SECRET_FILE.read_text(encoding="utf-8").strip()
            request = Request(
                LAYERV_API + path,
                data=body,
                method=method,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            with urlopen(request, timeout=20) as response:  # nosec B310
                raw = response.read()
            return json.loads(raw) if raw else {}
        except HTTPError as error:
            if method == "DELETE" and error.code in (404, 410):
                return {}
            raise PublicationError(f"LayerV rejected the request ({error.code})") from error
        except (OSError, URLError, json.JSONDecodeError) as error:
            raise PublicationError("Could not complete the LayerV request") from error

    def close(self) -> None:
        self.revoke()
        with self._lock:
            connector = self._connector
            self._connector = None
        if connector is not None and connector.poll() is None:
            connector.terminate()
            try:
                connector.wait(timeout=10)
            except subprocess.TimeoutExpired:
                connector.kill()
