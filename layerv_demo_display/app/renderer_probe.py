"""Bounded one-shot HA frontend authentication experiment."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from urllib.parse import urlsplit


HA_ORIGIN = os.getenv("HA_FRONTEND_ORIGIN", "http://homeassistant:8123").rstrip("/")
CHROMIUM = os.getenv("CHROMIUM_EXECUTABLE", "/usr/lib/chromium/chromium")
LOGGER = logging.getLogger("demo_display.renderer_probe")


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
            await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            stage = "frontend"
            await page.wait_for_selector("home-assistant", state="attached", timeout=timeout_ms)
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
