import asyncio
import time
import unittest

from app.renderer import _apply_chrome_visibility, _bounded


class FakePage:
    async def evaluate(self, script, arguments):
        self.script = script
        self.arguments = arguments


class RendererLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_visibility_options_reach_open_shadow_roots(self):
        page = FakePage()
        settings = type(
            "Settings", (), {"hide_ha_sidebar": True, "hide_ha_header": False}
        )()
        await _apply_chrome_visibility(page, settings)
        self.assertEqual(page.arguments, {"hideSidebar": True, "hideHeader": False})
        self.assertIn("shadowRoot", page.script)
        self.assertIn("ha-sidebar", page.script)
        self.assertIn("app-header", page.script)
    async def test_startup_stage_cannot_exceed_overall_deadline(self):
        with self.assertRaises(asyncio.TimeoutError):
            await _bounded(asyncio.sleep(1), time.monotonic() + 0.01)

    async def test_expired_deadline_does_not_start_operation(self):
        operation = asyncio.sleep(0)
        try:
            with self.assertRaises(TimeoutError):
                await _bounded(operation, time.monotonic() - 1)
        finally:
            operation.close()


if __name__ == "__main__":
    unittest.main()
