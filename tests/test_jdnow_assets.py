import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from joydance.games.jdnow.assets import JDNowAssets
from joydance.utils import GameConnectionError


class JDNowAssetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assets = JDNowAssets()
        self.addAsyncCleanup(self.assets.close)
        self.session = Mock(close=AsyncMock())
        session_patch = patch("joydance.games.jdnow.assets.aiohttp.ClientSession", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.download = AsyncMock(return_value=b"data")
        asset_patch = patch("joydance.games.jdnow.assets.JDNowAPI.asset", self.download)
        asset_patch.start()
        self.addCleanup(asset_patch.stop)
        self.song = {"base": "https://cdn.justdancenow.com/song-v1"}

    async def test_six_waiters_share_download_and_parse_after_one_cancels(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def download(*args):
            started.set()
            await release.wait()
            return b"data"

        self.download.side_effect = download
        parse = Mock(side_effect=bytes.decode)
        tasks = [asyncio.create_task(self.assets.get(self.song, "move", parse)) for _ in range(6)]
        try:
            await asyncio.wait_for(started.wait(), 1)
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            release.set()
            self.assertEqual(await asyncio.gather(*tasks[1:]), ["data"] * 5)
            self.assertEqual(await self.assets.get(self.song, "move", parse), "data")
            self.download.assert_awaited_once()
            parse.assert_called_once_with(b"data")
            self.assertIs(self.assets._session, self.session)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_last_waiter_cancels_download_and_a_retry_is_fresh(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def download(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.download.side_effect = download
        task = asyncio.create_task(self.assets.get(self.song, "move", bytes.decode))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.download.side_effect = None
        self.assertEqual(await self.assets.get(self.song, "move", bytes.decode), "data")
        self.assertEqual(self.download.await_count, 2)
        self.assertFalse(self.assets._pending)
        self.assertFalse(self.assets._waiters)

    async def test_failed_downloads_and_parses_are_not_cached(self):
        self.download.side_effect = [GameConnectionError("Test download failure"), b"data"]
        with self.assertRaises(GameConnectionError):
            await self.assets.get(self.song, "move", bytes.decode)
        self.assertEqual(await self.assets.get(self.song, "move", bytes.decode), "data")
        parse = Mock(side_effect=[ValueError("Test parse failure"), "valid"])
        self.download.side_effect = None
        with self.assertRaises(ValueError):
            await self.assets.get(self.song, "other", parse)
        self.assertEqual(await self.assets.get(self.song, "other", parse), "valid")
        self.assertEqual(self.download.await_count, 4)
        self.assertEqual(parse.call_count, 2)

    async def test_cache_evicts_least_recent_assets_with_byte_and_entry_limits(self):
        for byte_limit, entry_limit in ((8, JDNowAssets.MAX_ENTRIES), (100, 2)):
            self.assets.MAX_SOURCE_BYTES, self.assets.MAX_ENTRIES = byte_limit, entry_limit
            self.download.reset_mock()
            for path in ("first", "second", "first", "third", "second"):
                await self.assets.get(self.song, path, bytes.decode)
            self.assertEqual(self.download.await_count, 4)
            self.assertEqual(len(self.assets._cache), 2)
            self.assertLessEqual(self.assets._source_bytes, byte_limit)
            self.assets._cache.clear()
            self.assets._source_bytes = 0

    async def test_revision_and_parser_are_part_of_the_cache_key(self):
        parse = Mock(return_value="parsed")
        self.assertEqual(await self.assets.get(self.song, "move", bytes.decode), "data")
        self.assertEqual(await self.assets.get(self.song, "move", parse), "parsed")
        revised = {"base": "https://cdn.justdancenow.com/song-v2"}
        self.assertEqual(await self.assets.get(revised, "move", bytes.decode), "data")
        self.assertEqual(self.download.await_count, 3)
        with self.assertRaises(ValueError):
            await self.assets.get({"base": "https://example.com/song"}, "move", bytes.decode)
        self.assertEqual(self.download.await_count, 3)

    async def test_shutdown_cancels_loads_and_closes_the_shared_session(self):
        await self.assets.get(self.song, "cached", bytes.decode)
        started = asyncio.Event()

        async def download(*args):
            started.set()
            await asyncio.Event().wait()

        self.download.side_effect = download
        task = asyncio.create_task(self.assets.get(self.song, "pending", bytes.decode))
        await asyncio.wait_for(started.wait(), 1)
        await self.assets.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.session.close.assert_awaited_once()
        self.assertFalse(self.assets._cache)
        self.assertFalse(self.assets._pending)
