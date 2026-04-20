import asyncio
from contextlib import asynccontextmanager


class ByteBudget:
    """Async byte-granular semaphore. Limits the sum of outstanding bytes
    reserved by concurrent callers to `capacity`. Used to cap total in-flight
    download bytes so we don't blow disk/memory when many large TG files are
    being streamed at once.

    - `acquire(n)` blocks until enough budget is free, then reserves n bytes.
      A request larger than capacity is clamped to capacity (avoids
      deadlock — a 2GB file in a 1GB budget still eventually runs, solo).
    - `release(n)` returns bytes; over-release is clamped to capacity.
    - Waiters are served in FIFO order.
    """

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._available = capacity
        # FIFO queue of (need, future). Kept as a list (small N in practice).
        self._waiters: list[tuple[int, asyncio.Future]] = []

    @property
    def available(self) -> int:
        return self._available

    @property
    def capacity(self) -> int:
        return self._capacity

    async def acquire(self, n: int) -> None:
        need = min(max(n, 0), self._capacity)

        # Fast path: nothing queued and budget available right now.
        if not self._waiters and self._available >= need:
            self._available -= need
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters.append((need, fut))
        # Nudge in case capacity already allows serving earlier entries.
        self._try_wake()
        try:
            await fut
        except BaseException:
            # Cancelled/errored before grant: drop from queue if still there.
            self._waiters = [(w, f) for (w, f) in self._waiters if f is not fut]
            raise

    def release(self, n: int) -> None:
        if n <= 0:
            return
        self._available = min(self._capacity, self._available + n)
        self._try_wake()

    def _try_wake(self) -> None:
        # FIFO: only wake the head waiter, and only if it fits. Prevents
        # starvation of a big waiter behind many small ones.
        while self._waiters:
            need, fut = self._waiters[0]
            if fut.done():
                # Already cancelled: drop and continue.
                self._waiters.pop(0)
                continue
            if self._available < need:
                return
            self._waiters.pop(0)
            self._available -= need
            fut.set_result(None)

    @asynccontextmanager
    async def slot(self, n: int):
        """`async with budget.slot(n):` reserves n on entry, releases on exit
        (even on exception). Prefer this over manual acquire/release so leaks
        can't happen on mid-download errors."""
        granted = min(max(n, 0), self._capacity)
        await self.acquire(granted)
        try:
            yield
        finally:
            self.release(granted)
