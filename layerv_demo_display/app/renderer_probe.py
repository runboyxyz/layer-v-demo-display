"""Bounded one-shot HA frontend authentication experiment."""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit


HA_ORIGIN = os.getenv("HA_FRONTEND_ORIGIN", "http://homeassistant:8123").rstrip("/")
CHROMIUM = os.getenv("CHROMIUM_EXECUTABLE", "/usr/bin/chromium")


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    detail: str
    frame: bytes | None = None


def allowed_request(url: str, origin: str = HA_ORIGIN) -> bool:
    """Allow HA-origin traffic and browser-local data/blob resources only."""
    parsed = urlsplit(url)
    if parsed.scheme in {"data", "blob"}:
        return True
    expected = urlsplit(origin)
    return (parsed.scheme, parsed.hostname, parsed.port) == (
        expected.scheme,
        expected.hostname,
        expected.port,
    )


def external_auth_script() -> str:
    """Return the documented frontend bridge without embedding a credential."""
    return r"""
(() => {
  const reply = async (raw) => {
    let message;
    try { message = JSON.parse(raw); } catch (_) { return; }
    const callback = message?.payload?.callback || message?.callback;
    if (!['externalAuthSetToken', 'externalAuthRevokeToken'].includes(callback)) return;
    if (callback === 'externalAuthRevokeToken') {
      window[callback]?.(false); return;
    }
    const token = await window.__demoDisplayToken();
    window[callback]?.(true, {access_token: token, expires_in: 1800});
  };
  window.externalAppV2 = {postMessage: reply};
  window.externalApp = {getExternalAuth: reply, revokeExternalAuth: reply};
})();
"""


def run_probe(settings, token: str, timeout_ms: int = 45_000) -> ProbeResult:
    """Render once and always close the isolated Chromium instance."""
    if not token:
        return ProbeResult("failed", "Home Assistant did not provide an App token")
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    width, height = settings.viewport
    browser = context = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=CHROMIUM,
                headless=True,
                # Experiment-only fallback for HA OS container namespaces.
                # The browser remains non-root and AppArmor-confined.
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": width, "height": height},
                service_workers="block",
            )
            page = context.new_page()
            page.expose_function("__demoDisplayToken", lambda: token)
            page.add_init_script(script=external_auth_script())

            def route_request(route):
                if allowed_request(route.request.url):
                    route.continue_()
                else:
                    route.abort("blockedbyclient")

            page.route("**/*", route_request)
            target = f"{HA_ORIGIN}{settings.dashboard_path}?external_auth=1"
            page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_selector("home-assistant", state="attached", timeout=timeout_ms)
            page.wait_for_timeout(2_000)
            if not page.url.startswith(f"{HA_ORIGIN}{settings.dashboard_path}"):
                return ProbeResult("failed", "Home Assistant did not accept the App identity")
            frame = page.screenshot(type="jpeg", quality=75, full_page=False)
            return ProbeResult("succeeded", "Authenticated dashboard pixels captured", frame)
    except PlaywrightTimeout:
        return ProbeResult("failed", "Home Assistant dashboard did not become ready")
    except Exception:
        return ProbeResult("failed", "Chromium authentication probe failed")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
