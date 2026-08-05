"""Per-key fixed-window API rate limiting.

Deliberately OPT-IN: with no configuration the limiter is absent and every request passes
through untouched (so dev and the test-suite's rapid calls are never throttled). Production
turns it on with RATE_LIMIT_PER_MINUTE=N.

Fixed-window, keyed by X-API-Key (falling back to client IP for unauthenticated calls), so a
noisy or runaway tenant cannot exhaust the service for others. State is in-process: this
protects a single worker. Multiple workers each enforce the limit independently, so the
effective ceiling is N x workers — for a hard global ceiling use a shared store (Redis); that
is intentionally out of scope here and noted rather than faked.
"""
import os
from typing import Optional, Tuple


class FixedWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = 60):
        if limit <= 0:
            raise ValueError("limit must be > 0")
        self.limit = limit
        self.window = window_seconds
        self._buckets: dict = {}   # key -> (window_start_monotonic, count)

    def check(self, key: str, now: float) -> Tuple[bool, int]:
        """Count this request against `key`'s current window.

        Returns (allowed, retry_after_seconds). `now` is a monotonic clock, injected so the
        caller controls the clock (and tests are deterministic).
        """
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= self.window:          # window elapsed -> reset
            start, count = now, 0
        count += 1
        self._buckets[key] = (start, count)
        if count > self.limit:
            retry = int(self.window - (now - start)) + 1
            return False, max(retry, 1)
        return True, 0


# Module-global so main.py can configure it from env at startup and tests can swap it.
_LIMITER: Optional[FixedWindowLimiter] = None


def configure(limit: int, window_seconds: int = 60) -> None:
    """Enable (limit > 0) or disable (limit <= 0) the limiter."""
    global _LIMITER
    _LIMITER = FixedWindowLimiter(limit, window_seconds) if limit > 0 else None


def configure_from_env() -> None:
    limit = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "0") or "0")
    window = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60") or "60")
    configure(limit, window)


def get_limiter() -> Optional[FixedWindowLimiter]:
    return _LIMITER
