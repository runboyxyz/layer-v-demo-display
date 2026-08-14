"""In-memory periodic HA dashboard renderer for one bounded session."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .renderer_probe import CHROMIUM, HA_ORIGIN, allowed_request, external_auth_script


LOGGER = logging.getLogger("demo_display.renderer")


def run_renderer(session, settings, token: str) -> None:
    """Thread entry point; never persist credentials or frames."""
    failed = False
    try:
        asyncio.run(_capture_loop(session, settings, token))
    except Exception as error:
        failed = not session.stop_event.is_set()
        if failed:
            LOGGER.warning("Renderer stopped after unrecoverable error: error_type=%s", type(error).__name__)
    finally:
        session.renderer_stopped(failed=failed)
        LOGGER.info("Renderer stopped")


async def _capture_loop(session, settings, token: str) -> None:
    from playwright.async_api import async_playwright

    width, height = settings.viewport
    browser = context = stop_task = startup_watchdog = None
    LOGGER.info("Renderer starting")
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=CHROMIUM,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )

            async def close_when_stopped():
                await asyncio.to_thread(session.stop_event.wait)
                if browser is not None:
                    await browser.close()

            stop_task = asyncio.create_task(close_when_stopped())

            async def stop_if_startup_hangs():
                await asyncio.sleep(60)
                LOGGER.warning("Renderer startup deadline exceeded")
                session.renderer_stopped(failed=True)

            startup_watchdog = asyncio.create_task(stop_if_startup_hangs())
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                service_workers="block",
            )
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
            await page.goto(target, wait_until="commit", timeout=45_000)
            await page.wait_for_selector("home-assistant", state="attached", timeout=45_000)
            await page.wait_for_selector("hui-root", state="visible", timeout=45_000)
            await page.wait_for_timeout(2_000)
            if not page.url.startswith(f"{HA_ORIGIN}{settings.dashboard_path}"):
                raise RuntimeError("Home Assistant rejected the App identity")
            startup_watchdog.cancel()
            await asyncio.gather(startup_watchdog, return_exceptions=True)
            startup_watchdog = None
            session.renderer_started()
            LOGGER.info("Renderer started")

            while not session.stop_event.is_set():
                if not session.snapshot().active:
                    break
                started = time.monotonic()
                try:
                    frame = await page.screenshot(type="jpeg", quality=75, full_page=False, timeout=20_000)
                    session.publish_frame(frame, time.monotonic() - started)
                except Exception as error:
                    failures = session.renderer_failure()
                    LOGGER.warning(
                        "Frame capture failed: attempt=%s error_type=%s", failures, type(error).__name__
                    )
                    if failures >= 3:
                        raise RuntimeError("Frame capture retry limit reached") from error
                remaining = settings.capture_interval - (time.monotonic() - started)
                if remaining > 0:
                    await asyncio.to_thread(session.stop_event.wait, remaining)
    finally:
        if startup_watchdog is not None:
            startup_watchdog.cancel()
            await asyncio.gather(startup_watchdog, return_exceptions=True)
        if stop_task is not None:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
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
