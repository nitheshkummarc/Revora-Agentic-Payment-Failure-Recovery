"""A small in-memory sliding-window rate limiter for the public demo.

In-process and per-instance, which suits a single demo server; it resets on
restart and is not shared across replicas. Its job is to stop one visitor, or a
burst of traffic, from exhausting the model provider's quota.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Optional


class RateLimiter:
    """Allows at most `limit` hits per key within any `window_seconds`."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> Optional[float]:
        """Record a hit and return None, or return the seconds until the next
        hit would be allowed without recording anything."""
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window_seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                return self.window_seconds - (now - hits[0])
            hits.append(now)
            return None
