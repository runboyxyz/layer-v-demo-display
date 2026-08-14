import asyncio
import time
import unittest

from app.renderer import _bounded


class RendererLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
