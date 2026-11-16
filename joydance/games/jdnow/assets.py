"""Bounded public song assets shared read-only by the JDNow controllers in one app."""
import asyncio
from collections import OrderedDict

import aiohttp

from .api import JDNowAPI, service_url


class JDNowAssets:
    MAX_SOURCE_BYTES = 8 * 1024 * 1024
    MAX_ENTRIES = 1024

    def __init__(self):
        self._cache = OrderedDict()
        self._pending = {}
        self._waiters = {}
        self._source_bytes = 0
        self._session = None

    async def get(self, song, path, parse):
        key = service_url(song["base"] + "/data/" + path), parse
        if key in self._cache:
            value, _ = self._cache[key]
            self._cache.move_to_end(key)
            return value
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._load(key, song, path, parse))
            self._pending[key] = task
            task.add_done_callback(self._loaded)
        self._waiters[task] = self._waiters.get(task, 0) + 1
        try:
            # A controller leaving or changing coaches must not cancel another player's load.
            return await asyncio.shield(task)
        finally:
            self._waiters[task] -= 1
            if not self._waiters[task]:
                del self._waiters[task]
                if not task.done():
                    self._pending.pop(key, None)
                    task.cancel()

    def _loaded(self, task):
        if not task.cancelled():
            task.exception()

    async def _load(self, key, song, path, parse):
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            data = await JDNowAPI(self._session).asset(song, path)
            value = parse(data)
            # Parsed Python objects use more memory than their source files.
            self._cache[key] = value, len(data)
            self._source_bytes += len(data)
            while self._source_bytes > self.MAX_SOURCE_BYTES or len(self._cache) > self.MAX_ENTRIES:
                _, (_, size) = self._cache.popitem(last=False)
                self._source_bytes -= size
            return value
        finally:
            if self._pending.get(key) is asyncio.current_task():
                del self._pending[key]

    async def close(self):
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
        self._cache.clear()
        self._source_bytes = 0
