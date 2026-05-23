"""Rate limiting combining concurrency control and temporal spacing.

Provides a generic Throttle class that enforces both a maximum number of
concurrent requests (semaphore) and a minimum time interval between request
starts. This prevents API rate limit violations during concurrent agent
sessions in batch mode.

Throttle instances are designed as module-level singletons, configured from
settings, and shared across all concurrent sessions in the process.

Examples:
    Pure concurrency limiting (up to 3 simultaneous requests)::

        >>> api_throttle = Throttle(max_concurrent=3)
        >>> async with api_throttle:
        ...     result = await do_request()

    Concurrency + temporal spacing (1 request every 2 seconds)::

        >>> rate_limited = Throttle(max_concurrent=1, min_interval=2.0)
        >>> async with rate_limited:
        ...     return await do_request()
"""

import asyncio
import time
from types import TracebackType


class LoopState:
    """Per-event-loop state for a Throttle instance."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self.semaphore = semaphore
        self.last_request_time: float = 0.0
        self.lock = asyncio.Lock()


class Throttle:
    """Async context manager enforcing concurrency limits and temporal spacing.

    Combines an asyncio.Semaphore (max concurrent) with a minimum time interval
    between request starts. Creates internal state lazily per-event-loop to
    avoid "bound to a different event loop" errors.

    Args:
        max_concurrent: Maximum simultaneous requests allowed.
        min_interval: Minimum seconds between consecutive request starts.
            0.0 disables temporal spacing (pure concurrency limiting).
    """

    def __init__(self, max_concurrent: int, min_interval: float = 0.0) -> None:
        self._max_concurrent = max_concurrent
        self._min_interval = min_interval
        self._state: dict[int, LoopState] = {}

    def get_state(self) -> LoopState:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        if loop_id not in self._state:
            self._state[loop_id] = LoopState(
                asyncio.Semaphore(self._max_concurrent),
            )
        return self._state[loop_id]

    async def __aenter__(self) -> None:
        state = self.get_state()
        await state.semaphore.acquire()
        if self._min_interval > 0:
            async with state.lock:
                if state.last_request_time > 0:
                    elapsed = time.monotonic() - state.last_request_time
                    remaining = self._min_interval - elapsed
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                state.last_request_time = time.monotonic()

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        self.get_state().semaphore.release()
