import asyncio
import unittest

import psutil
import uvloop


class TestEventLoop(unittest.TestCase):
    def setUp(self):
        # Store the original event loop policy
        self._original_policy = asyncio.get_event_loop_policy()

    def tearDown(self):
        # Restore the original event loop policy
        asyncio.set_event_loop_policy(self._original_policy)
        print(f"Restored event loop policy: {asyncio.get_event_loop_policy()}")

    def test_default_event_loop(self):
        """Test the default event loop implementation being used"""
        loop = asyncio.new_event_loop()
        loop_class = asyncio.get_event_loop()
        print(f"\nDefault event loop implementation: {loop_class}")

        loop.close()

    def test_uvloop_implementation(self):
        """Test uvloop implementation and performance"""
        # Set uvloop as the event loop policy
        uvloop.install()

        loop = asyncio.new_event_loop()
        print(f"Uvloop event loop implementation: {loop}")
        assert isinstance(loop, uvloop.Loop)

        loop.close()

    async def async_operation(self):
        """Sample async operation for performance testing"""
        await asyncio.sleep(0.1)
        return "done"

    def test_event_loop_performance(self):
        """Compare performance between default loop and uvloop"""
        import time

        # Print CPU information
        print("\nSystem CPU Information:")
        print(f"Physical cores: {psutil.cpu_count(logical=False)}")
        print(f"Total threads: {psutil.cpu_count(logical=True)}")
        print(f"Current CPU usage: {psutil.cpu_percent(interval=1)}%")

        # Test with default event loop
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        start_time = time.time()
        process = psutil.Process()
        print("\nDefault event loop CPU usage:")
        print(f"Starting CPU threads: {len(process.threads())}")
        loop.run_until_complete(
            asyncio.gather(*[self.async_operation() for _ in range(1000000)])
        )
        print(f"Peak CPU threads: {len(process.threads())}")
        default_time = time.time() - start_time
        loop.close()

        # Test with uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        start_time = time.time()
        print("\nuvloop CPU usage:")
        print(f"Starting CPU threads: {len(process.threads())}")
        loop.run_until_complete(
            asyncio.gather(*[self.async_operation() for _ in range(1000000)])
        )
        print(f"Peak CPU threads: {len(process.threads())}")
        # Check CPU affinity if supported by the platform
        try:
            print(f"CPU Affinity: {len(process.cpu_affinity())} cores")
        except AttributeError:
            print("CPU Affinity not supported on this platform")
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
