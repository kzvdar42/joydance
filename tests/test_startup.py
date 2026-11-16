import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import dance
from joydance.constants import JOYDANCE_VERSION


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_failures_have_a_short_timeout_and_do_not_stop_startup(self):
        for error in (TimeoutError(), dance.aiohttp.ClientConnectionError("offline"), RuntimeError("response unavailable")):
            with self.subTest(error=type(error).__name__):
                request = MagicMock()
                request.__aenter__ = AsyncMock(side_effect=error)
                session = MagicMock()
                session.__aenter__.return_value = session
                session.get.return_value = request
                with patch.object(dance.aiohttp, "ClientSession", return_value=session) as factory, \
                        patch("builtins.print"):
                    await dance.on_startup({"loaded_cfg_path": None, "saved_cfg_path": None})
                self.assertEqual(factory.call_args.kwargs["timeout"].total, 3)
                self.assertEqual(session.get.call_count, 2)

    async def test_cancelling_startup_does_not_retry_the_update_check(self):
        request = MagicMock()
        request.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError)
        session = MagicMock()
        session.__aenter__.return_value = session
        session.get.return_value = request
        with patch.object(dance.aiohttp, "ClientSession", return_value=session), patch("builtins.print"):
            with self.assertRaises(asyncio.CancelledError):
                await dance.on_startup({"loaded_cfg_path": None, "saved_cfg_path": None})
        session.get.assert_called_once()

    async def test_banner_works_with_redirected_windows_output(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="cp1251")
        self.addCleanup(stream.close)
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={"tag_name": "v" + JOYDANCE_VERSION})
        request = MagicMock()
        request.__aenter__.return_value = response
        session = MagicMock()
        session.__aenter__.return_value = session
        session.get.return_value = request
        with patch.object(dance.sys, "stdout", stream), patch.object(dance.aiohttp, "ClientSession", return_value=session):
            await dance.on_startup({"loaded_cfg_path": None, "saved_cfg_path": None})
        stream.flush()
        text = output.getvalue().decode("utf-8")
        self.assertIn("░", text)
        self.assertIn("Running version " + JOYDANCE_VERSION, text)
