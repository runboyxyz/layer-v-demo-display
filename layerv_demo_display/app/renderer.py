"""In-memory periodic HA dashboard renderer for one bounded session."""

from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
import logging
import threading
import time

from .renderer_probe import CHROMIUM, HA_ORIGIN, allowed_request, external_auth_script
from .video import FragmentedMP4, ffmpeg_command


LOGGER = logging.getLogger("demo_display.renderer")
VIDEO_HEARTBEAT_SECONDS = 2.0


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


async def _jpeg_loop(page, session, settings) -> None:
    while not session.stop_event.is_set() and session.snapshot().active:
        started = time.monotonic()
        try:
            await _apply_chrome_visibility(page, settings)
            frame = await page.screenshot(type="jpeg", quality=75, full_page=False, timeout=20_000)
            session.publish_frame(frame, time.monotonic() - started)
        except Exception as error:
            failures = session.renderer_failure()
            LOGGER.warning("Frame capture failed: attempt=%s error_type=%s", failures, type(error).__name__)
            if failures >= 3:
                raise RuntimeError("Frame capture retry limit reached") from error
        await asyncio.to_thread(session.stop_event.wait, 0.1)


async def _video_loop(context, page, session, settings) -> None:
    """Encode newest-frame-only CDP JPEGs into a shared low-latency fMP4 stream."""
    width, height = settings.viewport
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_command(width, height, settings.renderer_target_fps),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    cdp = await context.new_cdp_session(page)
    frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
    parser = FragmentedMP4()

    async def on_frame(parameters):
        await cdp.send("Page.screencastFrameAck", {"sessionId": parameters["sessionId"]})
        try:
            value = base64.b64decode(parameters["data"], validate=True)
        except (ValueError, TypeError):
            return
        if frames.full():
            frames.get_nowait()
        frames.put_nowait(value)

    async def read_output():
        assert process.stdout is not None
        while value := await process.stdout.read(65_536):
            fragments = parser.feed(value)
            if parser.init_segment is not None:
                session.publish_video_init(parser.init_segment)
            for fragment in fragments:
                session.publish_video_fragment(fragment)

    async def drain_errors():
        assert process.stderr is not None
        while line := await process.stderr.readline():
            LOGGER.warning("Video encoder diagnostic: %s", line.decode("utf-8", "replace").strip()[:300])

    cdp.on("Page.screencastFrame", on_frame)
    reader = asyncio.create_task(read_output())
    diagnostics = asyncio.create_task(drain_errors())
    # A static dashboard may not trigger an immediate CDP screencast event.
    # Seed FFmpeg so it can emit the MP4 initialization segment promptly.
    initial_frame = await page.screenshot(
        type="jpeg", quality=60, full_page=False, timeout=20_000
    )
    frames.put_nowait(initial_frame)
    await cdp.send("Page.startScreencast", {
        "format": "jpeg", "quality": 60, "maxWidth": width, "maxHeight": height,
        "everyNthFrame": 1,
    })
    LOGGER.info("Experimental video encoder started: resolution=%sx%s target_fps=%s", width, height, settings.renderer_target_fps)
    latest = None
    last_digest = None
    last_encoded = 0.0
    interval = 1 / settings.renderer_target_fps
    try:
        while not session.stop_event.is_set() and session.snapshot().active:
            started = time.monotonic()
            try:
                latest = await asyncio.wait_for(frames.get(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            digest = sha256(latest).digest() if latest is not None else None
            changed = digest is not None and digest != last_digest
            heartbeat_due = latest is not None and now - last_encoded >= VIDEO_HEARTBEAT_SECONDS
            if changed or heartbeat_due:
                session.publish_frame(latest, time.monotonic() - started)
                assert process.stdin is not None
                process.stdin.write(latest)
                await process.stdin.drain()
                last_digest = digest
                last_encoded = now
            if process.returncode is not None:
                raise RuntimeError("Video encoder stopped")
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.to_thread(session.stop_event.wait, remaining)
    finally:
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.terminate()
            await process.wait()
        await asyncio.gather(reader, diagnostics, return_exceptions=True)


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

        if settings.renderer_mode == "video":
            try:
                await _video_loop(context, page, session, settings)
            except Exception as error:
                LOGGER.warning("Experimental video failed; falling back to JPEG: error_type=%s", type(error).__name__)
                await _jpeg_loop(page, session, settings)
        else:
            await _jpeg_loop(page, session, settings)
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
