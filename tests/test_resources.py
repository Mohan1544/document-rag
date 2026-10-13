import asyncio
import threading

import pytest

from app.config import Settings
from app.errors import ServiceError
from app.resources import BlockingWorkPool
from app.retrieval import SearchIndex
from app.schemas import Passage
from app.service import IndexCache


def index():
    return SearchIndex.build([Passage("p", "GCP", "source.json")], [[1.0, 1.0]])


@pytest.mark.asyncio
async def test_cancelled_thread_retains_worker_capacity_until_it_finishes() -> None:
    pool = BlockingWorkPool(1)
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def first():
        started.set()
        release.wait(timeout=2)

    task = asyncio.create_task(pool.run(first))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        second = asyncio.create_task(pool.run(second_started.set))
        await asyncio.sleep(0.01)
        assert not second_started.is_set()
        release.set()
        await asyncio.wait_for(second, 1)
        assert second_started.is_set()
    finally:
        release.set()
        if second is not None:
            await second
        await pool.aclose()


@pytest.mark.asyncio
async def test_cache_last_waiter_cancels_build_and_shutdown_rejects_new_work() -> None:
    cache = IndexCache(Settings())
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def build():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(cache.get_or_build("source", build))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cache.aclose()
    assert cancelled.is_set() and not cache._inflight
    with pytest.raises(ServiceError) as error:
        await cache.get_or_build("new", build)
    assert error.value.code == "service_stopping"


@pytest.mark.asyncio
async def test_cancelling_one_shared_waiter_preserves_the_other() -> None:
    cache = IndexCache(Settings())
    started = asyncio.Event()
    release = asyncio.Event()
    builds = 0

    async def build():
        nonlocal builds
        builds += 1
        started.set()
        await release.wait()
        return "json", index()

    first = asyncio.create_task(cache.get_or_build("source", build))
    second = asyncio.create_task(cache.get_or_build("source", build))
    try:
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        kind, _ = await second
        assert kind == "json" and builds == 1
    finally:
        release.set()
        await cache.aclose()


@pytest.mark.asyncio
async def test_cache_expiry_and_lru_eviction() -> None:
    cache = IndexCache(Settings(cache_entries=1, cache_ttl_seconds=10))
    clock = [100.0]
    cache._clock = lambda: clock[0]
    builds = 0

    async def build():
        nonlocal builds
        builds += 1
        return "json", index()

    await cache.get_or_build("a", build)
    await cache.get_or_build("a", build)
    assert builds == 1
    clock[0] = 110.0
    await cache.get_or_build("a", build)
    assert builds == 2
    await cache.get_or_build("b", build)
    await cache.get_or_build("a", build)
    assert builds == 4
    await cache.aclose()


@pytest.mark.asyncio
async def test_distinct_builds_are_bounded_and_have_a_deadline() -> None:
    cache = IndexCache(Settings(max_index_builds=1, index_timeout_seconds=1))
    started = asyncio.Event()

    async def build():
        started.set()
        await asyncio.Event().wait()

    first = asyncio.create_task(cache.get_or_build("a", build))
    try:
        await started.wait()
        with pytest.raises(ServiceError) as busy:
            await cache.get_or_build("b", build)
        assert busy.value.code == "index_busy"
        with pytest.raises(ServiceError) as timeout:
            await first
        assert timeout.value.code == "index_timeout"
    finally:
        await cache.aclose()
