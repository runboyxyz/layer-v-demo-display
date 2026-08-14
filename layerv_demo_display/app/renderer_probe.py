"""Bounded one-shot HA frontend authentication experiment."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import re
from urllib.parse import urlsplit


HA_ORIGIN = os.getenv("HA_FRONTEND_ORIGIN", "http://homeassistant").rstrip("/")
CHROMIUM = os.getenv("CHROMIUM_EXECUTABLE", "/usr/lib/chromium/chromium")
LOGGER = logging.getLogger("demo_display.renderer_probe")
NETWORK_ERROR = re.compile(r"net::(ERR_[A-Z0-9_]+)")


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    detail: str
    frame: bytes | None = None


def allowed_request(url: str, origin: str = HA_ORIGIN) -> bool:
    """Allow HA-origin HTTP/WebSocket traffic and browser-local resources."""
    parsed = urlsplit(url)
    if parsed.scheme in {"data", "blob"}:
        return True
    expected = urlsplit(origin)
    scheme_pairs = {"http": {"http", "ws"}, "https": {"https", "wss"}}

    def effective_port(value):
        if value.port is not None:
            return value.port
        return 443 if value.scheme in {"https", "wss"} else 80

    return (
        parsed.username is None
        and parsed.password is None
        and parsed.scheme in scheme_pairs.get(expected.scheme, set())
        and parsed.hostname == expected.hostname
        and effective_port(parsed) == effective_port(expected)
    )


def navigation_error_code(error: Exception) -> str:
    """Extract only Chromium's non-sensitive network error identifier."""
    match = NETWORK_ERROR.search(str(error))
    return match.group(1) if match else "ERR_UNKNOWN"


def external_auth_script() -> str:
    """Return the documented frontend bridge without embedding a credential."""
    return r"""
(() => {
  const coreSocket = new URL('/api/websocket', window.location.origin).href
    .replace(/^http/, 'ws');
  const supervisorSocket = 'ws://supervisor/core/websocket';
  const NativeWebSocket = window.WebSocket;
  const RestrictedWebSocket = function(url, protocols) {
    const target = String(url) === coreSocket ? supervisorSocket : url;
    return protocols === undefined
      ? new NativeWebSocket(target)
      : new NativeWebSocket(target, protocols);
  };
  RestrictedWebSocket.prototype = NativeWebSocket.prototype;
  Object.setPrototypeOf(RestrictedWebSocket, NativeWebSocket);
  window.WebSocket = RestrictedWebSocket;

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
  window.externalApp = {getExternalAuth: reply, revokeExternalAuth: reply};
})();
"""


def run_probe(settings, token: str, timeout_ms: int = 45_000) -> ProbeResult:
    """Render once and always close the isolated Chromium instance."""
    if not token:
        return ProbeResult("failed", "Home Assistant did not provide an App token")
    return asyncio.run(_run_probe(settings, token, timeout_ms))


async def _run_probe(settings, token: str, timeout_ms: int) -> ProbeResult:
    """Use the async transport so early driver errors are not masked."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from playwright.async_api import async_playwright

    width, height = settings.viewport
    browser = context = None
    stage = "playwright_driver"
    try:
        async with async_playwright() as playwright:
            stage = "chromium_launch"
            browser = await playwright.chromium.launch(
                executable_path=CHROMIUM,
                headless=True,
                # Experiment-only fallback for HA OS container namespaces.
                # The browser remains non-root and AppArmor-confined.
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            stage = "context"
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                service_workers="block",
            )
            stage = "page"
            page = await context.new_page()
            await page.expose_function("__demoDisplayToken", lambda: token)
            await page.add_init_script(script=external_auth_script())

            async def route_request(route):
                if allowed_request(route.request.url):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")

            await page.route("**/*", route_request)
            target = f"{HA_ORIGIN}{settings.dashboard_path}?external_auth=1"
            stage = "navigation"
            # HA dashboards can keep DOMContentLoaded pending while cards and
            # integrations initialize. A committed same-origin response is the
            # network boundary; frontend readiness is checked independently
            # using the authoritative root element below.
            await page.goto(target, wait_until="commit", timeout=timeout_ms)
            stage = "frontend"
            await page.wait_for_selector("home-assistant", state="attached", timeout=timeout_ms)
            await page.wait_for_selector("hui-root", state="visible", timeout=timeout_ms)
            await page.wait_for_timeout(2_000)
            if not page.url.startswith(f"{HA_ORIGIN}{settings.dashboard_path}"):
                return ProbeResult("failed", "Home Assistant did not accept the App identity")
            stage = "capture"
            frame = await page.screenshot(type="jpeg", quality=75, full_page=False)
            return ProbeResult("succeeded", "Authenticated dashboard pixels captured", frame)
    except PlaywrightTimeout:
        LOGGER.warning("Authentication probe timed out: stage=%s", stage)
        return ProbeResult("failed", f"Probe timed out during {stage}")
    except Exception as error:
        # Before a context exists, no token bridge or target URL has been sent
        # to Chromium. Playwright's launch transcript is therefore safe and is
        # needed to diagnose HA OS confinement. Once a page can exist, keep all
        # exception messages redacted because they may contain navigated URLs.
        if stage == "navigation":
            code = navigation_error_code(error)
            LOGGER.warning("Authentication probe navigation failed: code=%s", code)
            return ProbeResult("failed", f"Navigation failed ({code})")
        if stage in {"chromium_launch", "context"}:
            LOGGER.warning(
                "Authentication probe failed before navigation: stage=%s "
                "error_type=%s detail=%s",
                stage,
                type(error).__name__,
                str(error),
            )
        else:
            LOGGER.warning(
                "Authentication probe failed: stage=%s error_type=%s",
                stage,
                type(error).__name__,
            )
        return ProbeResult("failed", f"Probe failed during {stage} ({type(error).__name__})")
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
