"""Unit tests for api.thread_pools."""

import unittest


class TestThreadPools(unittest.TestCase):
    """Test that thread pools are correctly configured."""

    def test_pools_exist(self):
        from api.thread_pools import io_pool, inference_pool, channel_pool
        self.assertIsNotNone(io_pool)
        self.assertIsNotNone(inference_pool)
        self.assertIsNotNone(channel_pool)

    def test_pool_max_workers_within_bounds(self):
        from api.thread_pools import io_pool, inference_pool, channel_pool
        # io_pool: default 4, bounds [2, 8]
        self.assertGreaterEqual(io_pool._max_workers, 2)
        self.assertLessEqual(io_pool._max_workers, 8)
        # inference_pool: default 6, bounds [2, 12]
        self.assertGreaterEqual(inference_pool._max_workers, 2)
        self.assertLessEqual(inference_pool._max_workers, 12)
        # channel_pool: default 6, bounds [2, 12]
        self.assertGreaterEqual(channel_pool._max_workers, 2)
        self.assertLessEqual(channel_pool._max_workers, 12)

    def test_shutdown_all_callable(self):
        from api.thread_pools import shutdown_all
        # Just ensure it's callable (don't actually shut down)
        self.assertTrue(callable(shutdown_all))

    def test_pool_size_function(self):
        from api.thread_pools import _pool_size
        self.assertEqual(_pool_size("NONEXISTENT_VAR", 4, 2, 8), 4)
        self.assertEqual(_pool_size("NONEXISTENT_VAR", 1, 2, 8), 2)  # clamped to lo
        self.assertEqual(_pool_size("NONEXISTENT_VAR", 20, 2, 8), 8)  # clamped to hi


if __name__ == "__main__":
    unittest.main()
