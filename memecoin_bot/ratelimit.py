"""Client-side rate limiting and retry.

Public Solana RPC endpoints throttle aggressively. A throttled safety check
fails closed, which means the bot silently stops finding any tradable token
and looks like it is simply seeing a quiet market. Pacing requests and
retrying on 429 keeps a rate limit from masquerading as an empty screen.
"""

from dataclasses import dataclass, field
import logging
import random
import threading
import time

log = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    """Classic token bucket: ``rate`` permits per second, ``capacity`` burst.

    Thread-safe so a future concurrent scanner cannot outrun the limit.
    """

    rate: float
    capacity: float
    tokens: float = field(default=0.0)
    updated_at: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.tokens <= 0:
            self.tokens = self.capacity

    def acquire(self, sleeper=time.sleep) -> float:
        """Block until a permit is available. Returns seconds waited."""

        with self._lock:
            now = time.monotonic()
            elapsed = now - self.updated_at
            self.updated_at = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0

            deficit = 1.0 - self.tokens
            wait = deficit / self.rate if self.rate > 0 else 0.0
            self.tokens = 0.0
            self.updated_at = now + wait

        if wait > 0:
            sleeper(wait)
        return wait


def backoff_delays(
    attempts: int, base: float = 0.5, cap: float = 8.0,
    jitter: random.Random | None = None,
) -> list[float]:
    """Exponential backoff delays with full jitter.

    Jitter matters: without it every client retries in lockstep after an
    outage and re-creates the overload that caused it.
    """

    rng = jitter or random.Random()
    delays = []
    for attempt in range(attempts):
        ceiling = min(cap, base * (2 ** attempt))
        delays.append(rng.uniform(0.0, ceiling))
    return delays
