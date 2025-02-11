import asyncio
import sys
import unittest

import uvloop


class TestEventLoop(unittest.TestCase):
    def setUp(self):
        # Store the original event loop policy
        self._original_policy = asyncio.get_event_loop_policy()

    def tearDown(self):
        # Restore the original event loop policy
        asyncio.set_event_loop_policy(self._original_policy)

    def test_default_event_loop(self):
        """Test the default event loop implementation being used"""
        loop = asyncio.new_event_loop()
        loop_class = loop.__class__.__name__

        print(f"\nDefault event loop implementation: {loop_class}")
        print(f"Event loop module: {loop.__class__.__module__}")

        if sys.platform == "win32":
            self.assertEqual(loop_class, "ProactorEventLoop")
        else:
            self.assertEqual(loop_class, "SelectorEventLoop")

        loop.close()

    def test_uvloop_implementation(self):
        """Test uvloop implementation and performance"""
        # Set uvloop as the event loop policy
        uvloop.install()

        loop = asyncio.new_event_loop()
        loop_class = loop.__class__.__name__

        print(f"\nuvloop implementation: {loop_class}")
        print(f"Event loop module: {loop.__class__.__module__}")

        self.assertEqual(loop_class, "Loop")
        self.assertEqual(loop.__class__.__module__, "uvloop")

        loop.close()

    async def async_operation(self):
        """Sample async operation for performance testing"""
        await asyncio.sleep(0.1)
        return "done"

    def test_event_loop_performance(self):
        """Compare performance between default loop and uvloop"""
        import time

        # Test with default event loop
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        start_time = time.time()
        loop.run_until_complete(
            asyncio.gather(*[self.async_operation() for _ in range(1000)])
        )
        default_time = time.time() - start_time
        loop.close()

        # Test with uvloop
        uvloop.install()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        start_time = time.time()
        loop.run_until_complete(
            asyncio.gather(*[self.async_operation() for _ in range(1000)])
        )
        uvloop_time = time.time() - start_time
        loop.close()

        print("\nPerformance comparison:")
        print(f"Default event loop time: {default_time:.4f} seconds")
        print(f"uvloop time: {uvloop_time:.4f} seconds")
        print(
            f"Speed improvement: {((default_time - uvloop_time) / default_time) * 100:.2f}%"
        )


if __name__ == "__main__":
    unittest.main()
