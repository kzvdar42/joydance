import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import dance
from joydance.constants import JOYDANCE_VERSION


class StartupTests(unittest.IsolatedAsyncioTestCase):
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
