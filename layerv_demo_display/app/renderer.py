"""In-memory periodic HA dashboard renderer for one bounded session."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from .renderer_probe import CHROMIUM, HA_ORIGIN, allowed_request, external_auth_script


LOGGER = logging.getLogger("demo_display.renderer")


def run_renderer(session, settings, token: str) -> None:
    """Thread entry point; never persist credentials or frames."""
    failed = False
    startup_complete = threading.Event()

    def expire_stalled_startup() -> None:
        if not startup_complete.wait(75) and not session.stop_event.is_set():
            LOGGER.warning("Renderer startup watchdog expired")
            session.renderer_stopped(failed=True)

    watchdog = threading.Thread(target=expire_stalled_startup, daemon=True)
    watchdog.start()
    try:
        asyncio.run(_capture_loop(session, settings, token, startup_complete))
    except Exception as error:
        failed = not session.stop_event.is_set()
        if failed:
            LOGGER.warning("Renderer stopped after error: error_type=%s", type(error).__name__)
    finally:
        session.renderer_stopped(failed=failed)
        LOGGER.info("Renderer stopped")


async def _bounded(awaitable, deadline: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Renderer startup deadline exceeded")
    return await asyncio.wait_for(awaitable, timeout=remaining)


async def _apply_chrome_visibility(page, settings) -> None:
    """Hide HA shell chrome through each open shadow root before capture."""
    await page.evaluate(
        """({hideSidebar,hideHeader}) => {
          const selectors=[];
          if(hideSidebar)selectors.push('#drawer','ha-sidebar','app-drawer','ha-drawer','[slot="sidebar"]');
          if(hideHeader)selectors.push('app-header','[slot="header"]');
          const visit=root=>{
            for(const selector of selectors){
              for(const node of root.querySelectorAll(selector)){
                node.style.setProperty('display','none','important');
                node.setAttribute('data-demo-display-hidden','');
              }
            }
            for(const node of root.querySelectorAll('*'))if(node.shadowRoot)visit(node.shadowRoot);
          };
          visit(document);
          const roots=[document];
          for(let index=0;index<roots.length;index++){
            const root=roots[index];
            for(const node of root.querySelectorAll('*'))if(node.shadowRoot)roots.push(node.shadowRoot);
            for(const main of root.querySelectorAll('home-assistant,home-assistant-main')){
              main.style.setProperty('--mdc-drawer-width','0px');
              main.style.setProperty('--app-drawer-width','0px');
            }
          }
          window.dispatchEvent(new Event('resize'));
        }""",
        {"hideSidebar": settings.hide_ha_sidebar, "hideHeader": settings.hide_ha_header},
    )


async def _capture_loop(session, settings, token: str, startup_complete=None) -> None:
    from playwright.async_api import async_playwright

    width, height = settings.viewport
    manager = async_playwright()
    playwright = browser = context = stop_task = None
    deadline = time.monotonic() + 60
    LOGGER.info("Renderer starting: stage=playwright_driver")
    try:
        playwright = await _bounded(manager.start(), deadline)
        LOGGER.info("Renderer startup: stage=chromium_launch")
        browser = await _bounded(
            playwright.chromium.launch(
                executable_path=CHROMIUM,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            ),
            deadline,
        )

        async def close_when_stopped():
            await asyncio.to_thread(session.stop_event.wait)
            if browser is not None:
                await browser.close()

        stop_task = asyncio.create_task(close_when_stopped())
        LOGGER.info("Renderer startup: stage=context")
        context = await _bounded(
            browser.new_context(
                viewport={"width": width, "height": height},
                service_workers="block",
            ),
            deadline,
        )
        page = await _bounded(context.new_page(), deadline)
        await _bounded(page.expose_function("__demoDisplayToken", lambda: token), deadline)
        await _bounded(page.add_init_script(script=external_auth_script()), deadline)

        async def route_request(route):
            if allowed_request(route.request.url):
                await route.continue_()
            else:
                await route.abort("blockedbyclient")

        await _bounded(page.route("**/*", route_request), deadline)
        target = f"{HA_ORIGIN}{settings.dashboard_path}?external_auth=1"
        LOGGER.info("Renderer startup: stage=navigation")
        await _bounded(page.goto(target, wait_until="commit", timeout=45_000), deadline)
        LOGGER.info("Renderer startup: stage=ha_shell")
        await _bounded(
            page.wait_for_selector("home-assistant", state="attached", timeout=45_000), deadline
        )
        LOGGER.info("Renderer startup: stage=lovelace")
        await _bounded(page.wait_for_selector("hui-root", state="visible", timeout=45_000), deadline)
        await _bounded(page.wait_for_timeout(2_000), deadline)
        await _bounded(_apply_chrome_visibility(page, settings), deadline)
        if not page.url.startswith(f"{HA_ORIGIN}{settings.dashboard_path}"):
            raise RuntimeError("Home Assistant rejected the App identity")
        session.renderer_started()
        if startup_complete is not None:
            startup_complete.set()
        LOGGER.info("Renderer started")

        while not session.stop_event.is_set():
            if not session.snapshot().active:
                break
            started = time.monotonic()
            try:
                await _apply_chrome_visibility(page, settings)
                frame = await page.screenshot(
                    type="jpeg", quality=75, full_page=False, timeout=20_000
                )
                session.publish_frame(frame, time.monotonic() - started)
            except Exception as error:
                failures = session.renderer_failure()
                LOGGER.warning(
                    "Frame capture failed: attempt=%s error_type=%s", failures, type(error).__name__
                )
                if failures >= 3:
                    raise RuntimeError("Frame capture retry limit reached") from error
            # Chromium capture takes roughly 350 ms on HA Green. A short pause
            # produces a shared 2–3 FPS stream without continuously pegging it.
            await asyncio.to_thread(session.stop_event.wait, 0.1)
    finally:
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
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
