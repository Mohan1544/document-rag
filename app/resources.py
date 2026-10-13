"""Bound CPU/file work without releasing capacity while cancelled threads still run."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import TypeVar

from app.errors import ServiceError

T = TypeVar("T")


class BlockingWorkPool:
    def __init__(self, workers: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="document-rag")
        self._slots = asyncio.Semaphore(workers)
        self._closed = False

    async def run(self, function: Callable[..., T], *args, **kwargs) -> T:
        if self._closed:
            raise ServiceError(503, "service_stopping", "The service is shutting down.")
        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        if self._closed:
            self._slots.release()
            raise ServiceError(503, "service_stopping", "The service is shutting down.")
        try:
            context = copy_context()
            future = self._executor.submit(context.run, partial(function, *args, **kwargs))
        except RuntimeError:
            self._slots.release()
            raise

        def release(_future) -> None:
            # Cancelling an asyncio await cannot stop running Python code. Capacity
            # is returned only when the underlying thread actually finishes.
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._slots.release)

        future.add_done_callback(release)
        return await asyncio.wrap_future(future)

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
