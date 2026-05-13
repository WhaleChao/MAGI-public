"""Unit tests for skills.bridge.embedding_router core logic."""

import math
import unittest


class TestCosineSimilarity(unittest.TestCase):
    """Test the _cosine_similarity helper."""

    def test_identical_vectors(self):
        from skills.bridge.embedding_router import _cosine_similarity
        v = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0, places=5)

    def test_orthogonal_vectors(self):
        from skills.bridge.embedding_router import _cosine_similarity
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), 0.0, places=5)

    def test_opposite_vectors(self):
        from skills.bridge.embedding_router import _cosine_similarity
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), -1.0, places=5)

    def test_zero_vector(self):
        from skills.bridge.embedding_router import _cosine_similarity
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), 0.0)

    def test_known_value(self):
        from skills.bridge.embedding_router import _cosine_similarity
        a = [1.0, 2.0, 3.0]
        b = [4.0, 5.0, 6.0]
        expected = (4 + 10 + 18) / (math.sqrt(14) * math.sqrt(77))
        self.assertAlmostEqual(_cosine_similarity(a, b), expected, places=5)


class TestContentHash(unittest.TestCase):
    """Test the _content_hash helper."""

    def test_deterministic(self):
        from skills.bridge.embedding_router import _content_hash
        h1 = _content_hash("test")
        h2 = _content_hash("test")
        self.assertEqual(h1, h2)

    def test_different_inputs(self):
        from skills.bridge.embedding_router import _content_hash
        h1 = _content_hash("hello")
        h2 = _content_hash("world")
        self.assertNotEqual(h1, h2)

    def test_length(self):
        from skills.bridge.embedding_router import _content_hash
        h = _content_hash("anything")
        self.assertEqual(len(h), 16)


class TestEmbeddingRouterInit(unittest.TestCase):
    """Test EmbeddingRouter initialization and state."""

    def test_default_state(self):
        from skills.bridge.embedding_router import EmbeddingRouter
        router = EmbeddingRouter()
        self.assertFalse(router._ready)
        self.assertEqual(router._skill_vectors, {})
        self.assertEqual(router._msg_cache, {})
        self.assertEqual(router._msg_cache_size, 128)

    def test_route_returns_none_when_not_ready(self):
        from skills.bridge.embedding_router import EmbeddingRouter
        router = EmbeddingRouter()
        result = router.route("hello world")
        self.assertIsNone(result)

    def test_route_top_n_returns_empty_when_not_ready(self):
        from skills.bridge.embedding_router import EmbeddingRouter
        router = EmbeddingRouter()
        result = router.route_top_n("hello world")
        self.assertEqual(result, [])

    def test_route_empty_message(self):
        from skills.bridge.embedding_router import EmbeddingRouter
        router = EmbeddingRouter()
        router._ready = True  # fake ready
        result = router.route("")
        self.assertIsNone(result)
        result2 = router.route("   ")
        self.assertIsNone(result2)


class TestThresholds(unittest.TestCase):
    """Test that threshold configs are sane."""

    def test_direct_above_guided(self):
        from skills.bridge.embedding_router import _DIRECT_THRESH, _GUIDED_THRESH
        self.assertGreater(_DIRECT_THRESH, _GUIDED_THRESH)

    def test_thresholds_positive(self):
        from skills.bridge.embedding_router import _DIRECT_THRESH, _GUIDED_THRESH
        self.assertGreater(_DIRECT_THRESH, 0)
        self.assertGreater(_GUIDED_THRESH, 0)


if __name__ == "__main__":
    unittest.main()
