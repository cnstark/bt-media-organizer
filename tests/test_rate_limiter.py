"""站点级流控单测(RateLimiter)。"""
import unittest
from unittest.mock import patch

from src.reseed.matcher import RateLimiter, SkipSite


class FakeTime:
    """可控时钟。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


class TestRateLimiter(unittest.TestCase):
    def test_min_interval(self):
        ft = FakeTime()
        limiter = RateLimiter(min_interval=5, per_minute=100, global_interval=0,
                              cooldown_seconds=120)
        with patch("src.reseed.matcher.time.time", ft), \
             patch("src.reseed.matcher.time.sleep", lambda s: ft.advance(s)):
            limiter.acquire("search", "siteA", now=ft.now)
            # 立即再请求 → 需等待 5s
            wait = limiter.acquire("search", "siteA", now=ft.now)
            self.assertAlmostEqual(wait, 5.0)

    def test_per_minute_quota(self):
        ft = FakeTime()
        limiter = RateLimiter(min_interval=0, per_minute=3, global_interval=0,
                              cooldown_seconds=120)
        with patch("src.reseed.matcher.time.time", ft), \
             patch("src.reseed.matcher.time.sleep", lambda s: ft.advance(s)):
            for _ in range(3):
                limiter.acquire("search", "siteA", now=ft.now)
            # 第 4 次 → 需等到窗口最早请求过期(60s 后)
            wait = limiter.acquire("search", "siteA", now=ft.now)
            self.assertAlmostEqual(wait, 60.0)
            ft.advance(60.0)
            limiter.acquire("search", "siteA", now=ft.now)  # 应放行

    def test_global_interval(self):
        ft = FakeTime()
        limiter = RateLimiter(min_interval=0, per_minute=100, global_interval=2.0,
                              cooldown_seconds=120)
        with patch("src.reseed.matcher.time.time", ft), \
             patch("src.reseed.matcher.time.sleep", lambda s: ft.advance(s)):
            limiter.acquire("search", "siteA", now=ft.now)
            # 不同站也受全局间隔约束
            wait = limiter.acquire("search", "siteB", now=ft.now)
            self.assertAlmostEqual(wait, 2.0)

    def test_cooldown_skips_site(self):
        ft = FakeTime()
        limiter = RateLimiter(min_interval=0, per_minute=100, global_interval=0,
                              cooldown_seconds=120)
        with patch("src.reseed.matcher.time.time", ft):
            limiter.cooldown_site("siteA")
            with self.assertRaises(SkipSite):
                limiter.acquire("search", "siteA", now=ft.now)
            # 其他站不受影响
            limiter.acquire("search", "siteB", now=ft.now)
            # 冷却到期后恢复
            ft.advance(121)
            limiter.acquire("search", "siteA", now=ft.now)

    def test_kind_independent(self):
        """搜索与下载配额独立。"""
        ft = FakeTime()
        limiter = RateLimiter(min_interval=0, per_minute=2, global_interval=0,
                              cooldown_seconds=120)
        with patch("src.reseed.matcher.time.time", ft), \
             patch("src.reseed.matcher.time.sleep", lambda s: ft.advance(s)):
            limiter.acquire("search", "siteA", now=ft.now)
            limiter.acquire("search", "siteA", now=ft.now)
            # 搜索配额已满,但下载不受影响
            limiter.acquire("download", "siteA", now=ft.now)


if __name__ == "__main__":
    unittest.main()
