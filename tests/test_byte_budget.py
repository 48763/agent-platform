import asyncio
import pytest

from agents.tg_transfer.byte_budget import ByteBudget


@pytest.mark.asyncio
async def test_acquire_within_capacity_returns_immediately():
    """A request under the free budget must not block."""
    budget = ByteBudget(capacity=1000)
    await asyncio.wait_for(budget.acquire(500), timeout=0.1)
    assert budget.available == 500


@pytest.mark.asyncio
async def test_acquire_beyond_capacity_blocks_until_release():
    """If the request exceeds remaining budget, the caller waits until enough
    bytes are released. This is how we keep total in-flight downloads ≤ cap."""
    budget = ByteBudget(capacity=1000)
    await budget.acquire(800)

    # 300 more would exceed 1000 — must block.
    acquirer = asyncio.create_task(budget.acquire(300))
    await asyncio.sleep(0.02)
    assert not acquirer.done(), "acquire(300) should be blocked"

    # Free up 200 → still not enough (only 200+200=400 < 300? wait 200+200=400 >= 300 actually yes).
    # Release 100 → available = 200 + 100 = 300, just enough.
    budget.release(100)
    await asyncio.wait_for(acquirer, timeout=0.5)
    # After grant: 200 free - 300 requested + 100 released = ... just verify it unblocked.
    assert acquirer.done()


@pytest.mark.asyncio
async def test_release_wakes_multiple_waiters_in_order():
    """Two waiters both blocked; a release big enough for the first unblocks
    only that first one (FIFO), not the second still-too-large waiter."""
    budget = ByteBudget(capacity=100)
    await budget.acquire(100)  # fully reserved

    w1 = asyncio.create_task(budget.acquire(40))
    await asyncio.sleep(0.01)
    w2 = asyncio.create_task(budget.acquire(80))
    await asyncio.sleep(0.01)
    assert not w1.done()
    assert not w2.done()

    # Release 40 → w1 wakes, w2 still needs 80, available now 0
    budget.release(40)
    await asyncio.wait_for(w1, timeout=0.5)
    assert w1.done()
    await asyncio.sleep(0.02)
    assert not w2.done(), "w2 should still be blocked waiting for 80"

    # Release 80 → w2 wakes
    budget.release(80)
    await asyncio.wait_for(w2, timeout=0.5)
    assert w2.done()


@pytest.mark.asyncio
async def test_request_larger_than_capacity_is_capped():
    """A request bigger than total capacity must not block forever. It should
    be satisfied once the pool is fully free (treated as capacity-sized)."""
    budget = ByteBudget(capacity=100)
    # 500 > 100: without capping this would deadlock. Must complete.
    await asyncio.wait_for(budget.acquire(500), timeout=0.5)
    # Even though caller asked 500, only up to capacity is held.
    assert budget.available == 0


@pytest.mark.asyncio
async def test_release_more_than_held_clamps_to_capacity():
    """Double-release (e.g. buggy caller) must not inflate budget past cap —
    otherwise the backpressure guarantee is lost."""
    budget = ByteBudget(capacity=100)
    await budget.acquire(50)
    budget.release(50)
    budget.release(50)  # bogus extra release
    assert budget.available == 100


@pytest.mark.asyncio
async def test_slot_context_manager_releases_on_exit():
    """`async with budget.slot(n):` must reserve on entry and release on exit
    (including exceptions) — prevents budget leaks from mid-download errors."""
    budget = ByteBudget(capacity=100)

    async with budget.slot(60):
        assert budget.available == 40

    assert budget.available == 100


@pytest.mark.asyncio
async def test_slot_releases_on_exception():
    budget = ByteBudget(capacity=100)
    with pytest.raises(RuntimeError):
        async with budget.slot(70):
            raise RuntimeError("boom")
    assert budget.available == 100
