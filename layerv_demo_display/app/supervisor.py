"""Minimal root lifecycle supervisor for separated Demo Display processes."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

from .publication import CONNECTOR_CONFIG, CONNECTOR_LOGS, CONNECTOR_STATE, SECRET_FILE
from .configuration import load_settings
from .verification import SMTP_FILE


LOGGER = logging.getLogger("demo_display.supervisor")
SERVER_UID = 2200
CONNECTOR_UID = 2201
RUNTIME_GID = 2202
RUNTIME_DIR = Path("/run/layerv-demo")
PUBLICATION_SOCKET = RUNTIME_DIR / "publication.sock"


def _directory(path: Path, uid: int, mode: int = 0o700) -> None:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    # AppArmor intentionally withholds fowner. Set the mode only while this
    # bootstrap still owns a newly created path; existing UID-owned storage is
    # secured later by the process that owns it.
    if created or path.stat().st_uid == 0:
        path.chmod(mode)
    os.chown(path, uid, RUNTIME_GID)


def _own_tree(path: Path, uid: int) -> None:
    """Migrate only the app's fixed storage trees to their new owner."""
    for root, directories, files in os.walk(path):
        os.chown(root, uid, RUNTIME_GID)
        for name in directories:
            os.chown(Path(root) / name, uid, RUNTIME_GID)
        for name in files:
            os.chown(Path(root) / name, uid, RUNTIME_GID)


def prepare_storage() -> None:
    _directory(SMTP_FILE.parent, SERVER_UID)
    for path in (SECRET_FILE.parent, CONNECTOR_CONFIG.parent, CONNECTOR_STATE, CONNECTOR_LOGS):
        _directory(path, CONNECTOR_UID)
        _own_tree(path, CONNECTOR_UID)
    # The server may traverse to the fixed socket but cannot replace it.
    _directory(RUNTIME_DIR, CONNECTOR_UID, 0o710)


def _demote(uid: int):
    def apply() -> None:
        os.setgroups([])
        os.setgid(RUNTIME_GID)
        os.setuid(uid)
        os.umask(0o077)
    return apply


def _environment(tmpdir: str, include_supervisor: bool) -> dict[str, str]:
    allowed = {
        "APP_DATA_DIR", "APP_VERSION", "INGRESS_PORT", "LANG",
        "LAYERV_API_BASE_URL", "PATH", "TRUSTED_INGRESS_PROXIES",
    }
    if include_supervisor:
        allowed.update(("APP_SETTINGS_JSON", "SUPERVISOR_TOKEN"))
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({
        "HOME": tmpdir,
        "TMPDIR": tmpdir,
        "PUBLICATION_SOCKET": str(PUBLICATION_SOCKET),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": "/app",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    })
    return environment


def _start(module: str, uid: int, name: str, include_supervisor: bool) -> subprocess.Popen:
    tmpdir = f"/tmp/{name}"
    _directory(Path(tmpdir), uid)
    return subprocess.Popen(
        [sys.executable, "-m", module],
        env=_environment(tmpdir, include_supervisor),
        stdin=subprocess.DEVNULL,
        preexec_fn=_demote(uid),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.geteuid() != 0:
        raise RuntimeError("Demo Display supervisor must start as root")
    os.environ["APP_SETTINGS_JSON"] = json.dumps(asdict(load_settings()), separators=(",", ":"))
    prepare_storage()
    PUBLICATION_SOCKET.unlink(missing_ok=True)
    stopping = False

    def stop(*_args) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    broker = _start("app.publication_broker", CONNECTOR_UID, "connector", False)
    deadline = time.monotonic() + 10
    while not PUBLICATION_SOCKET.exists() and broker.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not PUBLICATION_SOCKET.exists():
        raise RuntimeError("LayerV publisher broker failed to start")
    server = _start("app.server", SERVER_UID, "server", True)
    processes = (server, broker)
    try:
        while not stopping and all(process.poll() is None for process in processes):
            time.sleep(0.25)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        failed = next((process.returncode for process in processes if process.returncode), 0)
    raise SystemExit(failed)


if __name__ == "__main__":
    main()
