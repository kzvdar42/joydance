import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp
from websockets.asyncio.client import connect as websocket_connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus, NegotiationError
from websockets.frames import Close, Frame, Opcode
from websockets.protocol import State

from joydance.constants import JD_PING_INTERVAL, JD_PING_TIMEOUT, JD_SERVICE_TIMEOUT, JDMO_PAIRING_URL, PairingMethod, PairingState
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2
from joydance.utils import GameConnectionError


class GameConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        discovery = patch("joydance.games.justdance_v2.discover_jdmo_game", new=AsyncMock(return_value=None))
        discovery.start()
        self.addCleanup(discovery.stop)
        for module in ("justdance_abstract", "justdance_v1", "justdance_v2"):
            factory = patch("joydance.games." + module + ".logger")
            factory.start()
            self.addCleanup(factory.stop)

    def make_game(self, game_class, **kwargs):
        controller = SimpleNamespace(
            serial="connection-test", is_connected=Mock(return_value=True),
            set_accel_collection=Mock(), set_player_led=AsyncMock(),
            animate_connecting_led=AsyncMock(), get_latest_command=Mock(return_value=None),
            get_joystick_command=Mock(return_value=None),
        )
        game = game_class(controller, pairing_code="123456", host_ip_addr="127.0.0.1",
                          on_state_changed=AsyncMock(), on_game_message=AsyncMock(), **kwargs)
        game.attempt_reconnect = AsyncMock()
        self.addAsyncCleanup(game.disconnect, should_reconnect=False)
        return game

    def socket(self):
        async def messages():
            await asyncio.Event().wait()
            yield ""

        websocket = MagicMock(state=State.OPEN, send=AsyncMock(), close=AsyncMock(), close_code=None, close_reason="")
        websocket.__aiter__.side_effect = messages
        return websocket

    def http_session(self, status=200, body="OK", json_body=None):
        response = SimpleNamespace(status=status, text=AsyncMock(return_value=body), json=AsyncMock(return_value=json_body))
        request = MagicMock()
        request.__aenter__.return_value = response
        session = MagicMock()
        session.__aenter__.return_value = session
        session.get.return_value = request
        session.post.return_value = request
        session.request.return_value = request
        return session

    async def test_jdmo_local_match_bypasses_online_pairing(self):
        game = self.make_game(JustDanceGameV2, pairing_method=PairingMethod.JDMO.value)
        game.requires_punch_pairing = True
        game.tls_certificate = "old certificate"
        with (
            patch("joydance.games.justdance_v2.discover_jdmo_game", new=AsyncMock(return_value="127.0.0.1")) as discover,
            patch.object(game, "_request_jdmo_pairing", new=AsyncMock()) as request,
        ):
            await game.send_pairing_code()
        discover.assert_awaited_once_with("123456", "127.0.0.1")
        request.assert_not_awaited()
        self.assertEqual(game.pairing_url, "ws://127.0.0.1:8080/smartphone")
        self.assertFalse(game.requires_punch_pairing)
        self.assertIsNone(game.tls_certificate)

    async def test_official_pairing_does_not_use_jdmo_discovery(self):
        game = self.make_game(JustDanceGameV2)
        with (
            patch("joydance.games.justdance_v2.discover_jdmo_game", new=AsyncMock()) as discover,
            patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=self.http_session(
                json_body={"pairingUrl": "https://127.0.0.1:8080"},
            )),
        ):
            await game.send_pairing_code()
        discover.assert_not_awaited()
        self.assertEqual(game.pairing_url, "wss://127.0.0.1:8080/smartphone")

    async def test_jdmo_pairing_and_punch_requests_use_the_mod_service(self):
        for punch in (False, True):
            with self.subTest(punch=punch):
                game = self.make_game(JustDanceGameV2, pairing_method=PairingMethod.JDMO.value)
                session = self.http_session(body=json.dumps({"pairingUrl": "http://127.0.0.1:8080"}))
                method = game.send_initiate_punch_pairing if punch else game.send_pairing_code
                with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=session) as factory:
                    await method()
                args, kwargs = session.request.call_args
                path = "initiate-punch-pairing" if punch else "pairing-info"
                self.assertEqual(args, ("POST" if punch else "GET", f"{JDMO_PAIRING_URL}/v1/{path}"))
                self.assertEqual(kwargs["params"], None if punch else {"code": "123456"})
                self.assertEqual(kwargs["json"], {
                    "pairingCode": "123456", "mobileIP": "127.0.0.1", "mobilePort": game.host_port,
                } if punch else None)
                self.assertNotIn("ssl", kwargs)
                self.assertEqual(factory.call_args.kwargs["timeout"].total, JD_SERVICE_TIMEOUT)
                self.assertNotIn("Authorization", factory.call_args.kwargs["headers"])

    async def test_jdmo_falls_back_to_v2_only_for_a_missing_route(self):
        for punch in (False, True):
            for status, body, fallback in (
                (404, "Cannot GET /sessions/v1/pairing-info", True),
                (405, "", True), (404, "null", True),
                (404, '{"code":"INVALID_CODE"}', False),
                (400, '{"code":"INVALID_CODE"}', False),
                (503, "Service unavailable", False),
            ):
                with self.subTest(punch=punch, status=status, body=body):
                    game = self.make_game(JustDanceGameV2, pairing_method=PairingMethod.JDMO.value)
                    session = self.http_session()
                    session.request.side_effect = [
                        self.http_session(status=status, body=body).get.return_value,
                        self.http_session(body='{"pairingUrl":"https://127.0.0.1:8080"}').get.return_value,
                    ]
                    method = game.send_initiate_punch_pairing if punch else game.send_pairing_code
                    with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=session):
                        if fallback:
                            await method()
                        else:
                            with self.assertRaises(GameConnectionError) as caught:
                                await method()
                            self.assertEqual(caught.exception.should_reconnect, status == 503)
                            self.assertEqual(caught.exception.state, PairingState.ERROR_PUNCH_PAIRING if punch else (
                                PairingState.ERROR_CONNECTION if status == 503 else PairingState.ERROR_INVALID_PAIRING_CODE
                            ))
                    self.assertEqual(session.request.call_count, 2 if fallback else 1)
                    if fallback:
                        self.assertIn("/v2/", session.request.call_args.args[1])

    async def test_jdmo_service_transport_failures_keep_the_correct_phase(self):
        for punch in (False, True):
            game = self.make_game(JustDanceGameV2, pairing_method=PairingMethod.JDMO.value)
            session = self.http_session()
            session.request.side_effect = TimeoutError()
            with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=session):
                with self.assertRaises(GameConnectionError) as caught:
                    await (game.send_initiate_punch_pairing() if punch else game.send_pairing_code())
            self.assertTrue(caught.exception.should_reconnect)
            self.assertEqual(caught.exception.state, PairingState.ERROR_PUNCH_PAIRING if punch else PairingState.ERROR_CONNECTION)
            self.assertIn("timed out", str(caught.exception))

    async def test_pairing_urls_preserve_transport_path_and_query(self):
        for url, expected in (
            ("http://127.0.0.1:8080", "ws://127.0.0.1:8080/smartphone"),
            ("https://127.0.0.1:8080/", "wss://127.0.0.1:8080/smartphone"),
            ("ws://127.0.0.1:8080/base/", "ws://127.0.0.1:8080/base/smartphone"),
            ("wss://[::1]:8080/smartphone/?key=test", "wss://[::1]:8080/smartphone?key=test"),
        ):
            with self.subTest(url=url):
                game = self.make_game(JustDanceGameV2)
                session = self.http_session(json_body={"pairingUrl": url, "requiresPunchPairing": "false"})
                with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=session):
                    await game.send_pairing_code()
                self.assertEqual(game.pairing_url, expected)
                self.assertFalse(game.requires_punch_pairing)

    async def test_malformed_pairing_addresses_are_rejected(self):
        for response in (None, {}, {"pairingUrl": 123}, *({"pairingUrl": url} for url in (
            "", "http:///no-host", "ftp://example.test", "ws://[broken", "ws://localhost:99999",
            "ws://user:password@localhost", "ws://localhost/#fragment",
        ))):
            with self.subTest(response=response):
                game = self.make_game(JustDanceGameV2, pairing_method=PairingMethod.JDMO.value)
                with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=self.http_session(body=json.dumps(response))):
                    with self.assertRaisesRegex(GameConnectionError, "Invalid console address"):
                        await game.send_pairing_code()

    async def test_only_jdmo_skips_ubisoft_authentication_and_keeps_punch_pairing(self):
        for method in (PairingMethod.DEFAULT.value, PairingMethod.JDMO.value):
            game = self.make_game(JustDanceGameV2, pairing_method=method)
            game.requires_punch_pairing = True
            with (
                patch.object(game, "get_access_token", new=AsyncMock()) as token,
                patch.object(game, "send_pairing_code", new=AsyncMock()) as code,
                patch.object(game, "hole_punching", new=AsyncMock()) as punch,
                patch.object(game, "connect_ws", new=AsyncMock()) as connect,
            ):
                await game.pair()
            self.assertEqual(token.await_count, int(method == PairingMethod.DEFAULT.value))
            code.assert_awaited_once()
            punch.assert_awaited_once_with(on_listening=game.send_initiate_punch_pairing)
            connect.assert_awaited_once()

    async def test_v2_selects_tls_from_the_url_without_changing_subprotocol(self):
        for scheme in ("ws", "wss"):
            game = self.make_game(JustDanceGameV2)
            game.pairing_url = f"{scheme}://127.0.0.1:8080/smartphone"
            manager = MagicMock()
            manager.__aenter__.return_value = self.socket()
            with (
                patch("joydance.games.justdance_v2.connect", return_value=manager) as connect,
                patch.object(game, "run_connection_tasks", new=AsyncMock()),
            ):
                await game.connect_ws()
            self.assertEqual(connect.call_args.kwargs["subprotocols"], ["v2.phonescoring.jd.ubisoft.com"])
            self.assertEqual(connect.call_args.kwargs["ssl"] is None, scheme == "ws")
            self.assertEqual(connect.call_args.kwargs["server_hostname"], "127.0.0.1" if scheme == "wss" else None)

    async def test_v2_completes_a_real_plaintext_handshake(self):
        game = self.make_game(JustDanceGameV2, pairing_method=PairingMethod.JDMO.value)
        ready, release = asyncio.Event(), asyncio.Event()

        async def server(websocket):
            self.assertEqual(websocket.subprotocol, "v2.phonescoring.jd.ubisoft.com")
            self.assertEqual(json.loads(await websocket.recv())["root"]["clientVersion"], "0.1")
            await websocket.send(json.dumps({"__class": "JD_PhoneDataCmdHandshakeContinue", "phoneID": 2, "protocolVersion": 5}))
            self.assertEqual(json.loads(await websocket.recv())["root"], {"__class": "JD_PhoneDataCmdSync", "phoneID": 2})
            await websocket.send(json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 2}))
            self.assertEqual(json.loads(await websocket.recv())["root"]["__class"], "JD_PhoneDataCmdSyncEnd")
            ready.set()
            await release.wait()

        async with serve(server, "127.0.0.1", 0, subprotocols=["v2.phonescoring.jd.ubisoft.com"]) as listener:
            game.pairing_url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/smartphone"
            task = asyncio.create_task(game.connect_ws())
            try:
                await asyncio.wait_for(ready.wait(), 1)
                self.assertTrue(game.is_connected)
                await game.disconnect(should_reconnect=False)
                await asyncio.wait_for(task, 1)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_v2_keeps_punch_peer_hostname_selection_for_tls_only(self):
        for url, expected in (
            ("wss://192.168.1.3:8080/smartphone", "192.168.1.4"),
            ("wss://10.0.0.3:8080/smartphone", "192.168.1.4"),
            ("wss://10.255.255.255:8080/smartphone", "192.168.1.4"),
            ("wss://172.16.0.0:8080/smartphone", "192.168.1.4"),
            ("wss://172.31.255.255:8080/smartphone", "192.168.1.4"),
            ("wss://192.168.255.255:8080/smartphone", "192.168.1.4"),
            ("wss://172.15.255.255:8080/smartphone", "172.15.255.255"),
            ("wss://172.32.0.0:8080/smartphone", "172.32.0.0"),
            ("wss://8.8.8.8:8080/smartphone", "8.8.8.8"),
            ("wss://127.0.0.1:8080/smartphone", "127.0.0.1"),
            ("wss://169.254.1.3:8080/smartphone", "169.254.1.3"),
            ("wss://192.0.2.3:8080/smartphone", "192.0.2.3"),
            ("wss://[::1]:8080/smartphone", "::1"),
            ("wss://console.example:8080/smartphone", "console.example"),
            ("wss://10.console.example:8080/smartphone", "10.console.example"),
            ("wss://192.168.console.example:8080/smartphone", "192.168.console.example"),
            ("ws://192.168.1.3:8080/smartphone", None),
            ("ws://172.16.1.3:8080/smartphone", None),
        ):
            with self.subTest(url=url):
                game = self.make_game(JustDanceGameV2)
                game.pairing_url = url
                game.console_conn = Mock(getpeername=Mock(return_value=("192.168.1.4", 8080)))
                manager = MagicMock()
                manager.__aenter__.return_value = self.socket()
                with (
                    patch("joydance.games.justdance_v2.connect", return_value=manager) as connect,
                    patch.object(game, "run_connection_tasks", new=AsyncMock()),
                ):
                    await game.connect_ws()
                self.assertEqual(connect.call_args.kwargs["server_hostname"], expected)

    async def test_v2_keeps_private_hostname_without_a_punch_socket(self):
        game = self.make_game(JustDanceGameV2)
        game.pairing_url = "wss://172.16.1.3:8080/smartphone"
        manager = MagicMock()
        manager.__aenter__.return_value = self.socket()
        with (
            patch("joydance.games.justdance_v2.connect", return_value=manager) as connect,
            patch.object(game, "run_connection_tasks", new=AsyncMock()),
        ):
            await game.connect_ws()
        self.assertEqual(connect.call_args.kwargs["server_hostname"], "172.16.1.3")

    async def test_v2_url_parse_failure_uses_connection_error_cleanup(self):
        game = self.make_game(JustDanceGameV2)
        game.pairing_url = "wss://[broken"
        with (
            patch("joydance.games.justdance_v2.connect") as connect,
            patch.object(game, "_handle_connection_error", new=AsyncMock()) as handle_error,
        ):
            await game.connect_ws()
        connect.assert_not_called()
        handle_error.assert_awaited_once()
        self.assertIsInstance(handle_error.await_args.args[0], ValueError)

    async def test_hello_and_sync_publish_connected_once_and_clear_old_errors(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                game.ws = self.socket()
                game._state["error_details"] = "Previous connection failed"
                await game.send_hello()
                hello = json.loads(game.ws.send.await_args.args[0])["root"]
                self.assertEqual(hello["clientVersion"], "0.1")
                self.assertIn("accelAcquisitionFreqHz", hello)
                await game.on_message(json.dumps({"__class": "JD_PhoneDataCmdHandshakeContinue", "phoneID": 7, "protocolVersion": 5}))
                self.assertEqual(json.loads(game.ws.send.await_args.args[0])["root"], {"__class": "JD_PhoneDataCmdSync", "phoneID": 7})
                for _ in range(2):
                    await game.on_message(json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 7}))
                self.assertTrue(game.is_connected)
                self.assertIsNone(game._handshake_deadline)
                self.assertIsNone(game._state["error_details"])
                game._on_state_changed.assert_awaited_once()

    async def test_handshake_timeout_cleans_up_and_retries_in_both_protocols(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                websocket = self.socket()
                manager = MagicMock()
                manager.__aenter__.return_value = websocket
                manager.__aexit__.return_value = False
                game.pairing_url = "wss://127.0.0.1:8080/smartphone" if game_class is JustDanceGameV2 else "ws://127.0.0.1:8080/smartphone"
                with (
                    patch(game_class.__module__ + ".connect", return_value=manager) as connect,
                    patch("joydance.games.justdance_abstract.JD_HANDSHAKE_TIMEOUT", 0.03),
                ):
                    await asyncio.wait_for(game.connect_ws(), 1)
                self.assertEqual(connect.call_args.kwargs["ping_interval"], JD_PING_INTERVAL)
                self.assertEqual(connect.call_args.kwargs["ping_timeout"], JD_PING_TIMEOUT)
                self.assertFalse(game.is_connected)
                self.assertIsNone(game.ws)
                self.assertEqual(game._state["state"], PairingState.ERROR_CONSOLE_CONNECTION.value)
                self.assertIn("handshake", game._state["error_details"])
                self.assertTrue(game._state["connection_requested"])
                websocket.close.assert_awaited_once()
                await game.reconnection_task
                game.attempt_reconnect.assert_awaited_once()

    async def test_handshake_deadline_also_bounds_a_blocked_hello(self):
        game = self.make_game(JustDanceGameV1)
        game.ws = self.socket()

        async def blocked_send(*args):
            await asyncio.Event().wait()

        game.ws.send.side_effect = blocked_send
        with patch("joydance.games.justdance_abstract.JD_HANDSHAKE_TIMEOUT", 0.03):
            with self.assertRaisesRegex(GameConnectionError, "handshake"):
                await asyncio.wait_for(game.run_connection_tasks(), 1)

    async def test_handshake_continue_does_not_remove_the_sync_end_deadline(self):
        game = self.make_game(JustDanceGameV1)
        game.ws = self.socket()

        async def messages():
            yield json.dumps({"__class": "JD_PhoneDataCmdHandshakeContinue", "phoneID": 7, "protocolVersion": 3})
            await asyncio.Event().wait()

        game.ws.__aiter__.side_effect = messages
        with patch("joydance.games.justdance_abstract.JD_HANDSHAKE_TIMEOUT", 0.03):
            with self.assertRaisesRegex(GameConnectionError, "handshake"):
                await asyncio.wait_for(game.run_connection_tasks(), 1)
        self.assertEqual(json.loads(game.ws.send.await_args.args[0])["root"], {"__class": "JD_PhoneDataCmdSync", "phoneID": 7})
        self.assertFalse(game.is_connected)

    async def test_game_rejections_stop_retries_and_keep_the_reason(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for message in (
                {"__class": "JD_PhoneDataCmdHandshakeContinue", "phoneID": 1, "protocolVersion": 6},
                {"__class": "JD_PhoneDataCmdHandshakeContinue", "phoneID": 1, "protocolVersion": "invalid"},
                {"__class": "JD2015_NotPhoneScoring"},
            ):
                with self.subTest(protocol=game_class.__name__, message=message):
                    game = self.make_game(game_class)
                    game.ws = self.socket()
                    with self.assertRaises(GameConnectionError) as caught:
                        await game.on_message(json.dumps(message))
                    await game._handle_connection_error(caught.exception)
                    self.assertEqual(game._state["state"], PairingState.ERROR_CONSOLE_CONNECTION.value)
                    self.assertEqual(game._state["error_details"], str(caught.exception))
                    self.assertFalse(game._state["connection_requested"])
                    self.assertIsNone(game.reconnection_task)

    async def test_failed_sync_reply_does_not_publish_connected(self):
        game = self.make_game(JustDanceGameV1)
        game.ws = self.socket()
        game.ws.send.side_effect = ConnectionError("Socket failed")
        await game.on_message(json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 1}))
        self.assertFalse(game.is_connected)
        self.assertEqual(game._state["state"], PairingState.ERROR_CONSOLE_CONNECTION.value)
        self.assertIn("Socket failed", game._state["error_details"])

    async def test_terminal_send_failure_from_a_connection_worker_does_not_cancel_its_owner(self):
        game = self.make_game(JustDanceGameV1)
        websocket = self.socket()

        async def messages():
            yield json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 1})
            await asyncio.Event().wait()

        websocket.__aiter__.side_effect = messages
        websocket.send.side_effect = [None, ConnectionClosed(Close(4004, "Rejected"), None)]
        manager = MagicMock()
        manager.__aenter__.return_value = websocket
        manager.__aexit__.return_value = False
        game.pairing_url = "ws://127.0.0.1:8080/smartphone"
        game.pair = game.connect_ws
        with patch("joydance.games.justdance_v1.connect", return_value=manager):
            task = await game.start_pair()
            try:
                await asyncio.wait_for(asyncio.shield(task), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(game.is_connected)
        self.assertFalse(game._state["connection_requested"])
        self.assertIn("Rejected", game._state["error_details"])

    async def test_successful_handshake_survives_the_original_deadline(self):
        game = self.make_game(JustDanceGameV1)
        game.ws = self.socket()

        async def messages():
            yield json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 1})
            await asyncio.Event().wait()

        game.ws.__aiter__.side_effect = messages
        with patch("joydance.games.justdance_abstract.JD_HANDSHAKE_TIMEOUT", 0.03):
            task = asyncio.create_task(game.run_connection_tasks())
            try:
                await asyncio.sleep(0.08)
                self.assertTrue(game.is_connected)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_close_reasons_distinguish_rejection_and_silent_failure(self):
        for error, retry, reason in (
            (ConnectionClosed(Close(4004, "Session rejected"), None), False, "Session rejected"),
            (ConnectionClosed(None, Close(1011, "keepalive ping timeout")), True, "keepalive"),
        ):
            with self.subTest(error=error):
                game = self.make_game(JustDanceGameV1)
                game.ws = self.socket()
                game.is_connected = True
                await game._handle_connection_error(error)
                self.assertEqual(game._state["state"], PairingState.ERROR_CONSOLE_CONNECTION.value)
                self.assertIn(reason, game._state["error_details"])
                self.assertEqual(game._state["connection_requested"], retry)

    async def test_websocket_handshake_errors_distinguish_rejections_and_service_failures(self):
        for error, retry, detail in (
            (InvalidStatus(SimpleNamespace(status_code=403)), False, "HTTP 403"),
            (InvalidStatus(SimpleNamespace(status_code=503)), True, "HTTP 503"),
            (NegotiationError("unsupported subprotocol"), False, "pairing method"),
        ):
            with self.subTest(error=error):
                game = self.make_game(JustDanceGameV1)
                await game._handle_connection_error(error)
                self.assertEqual(game._state["connection_requested"], retry)
                self.assertIn(detail, game._state["error_details"])

    async def test_http_failures_distinguish_invalid_codes_and_service_failures(self):
        for method, status, state, retry in (
            ("get_access_token", 401, PairingState.ERROR_CONNECTION, False),
            ("get_access_token", 503, PairingState.ERROR_CONNECTION, True),
            ("send_pairing_code", 404, PairingState.ERROR_INVALID_PAIRING_CODE, False),
            ("send_pairing_code", 410, PairingState.ERROR_INVALID_PAIRING_CODE, False),
            ("send_pairing_code", 503, PairingState.ERROR_CONNECTION, True),
            ("send_pairing_code", 429, PairingState.ERROR_CONNECTION, True),
            ("send_initiate_punch_pairing", 503, PairingState.ERROR_PUNCH_PAIRING, True),
        ):
            with self.subTest(method=method, status=status):
                game = self.make_game(JustDanceGameV2)
                with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=self.http_session(status=status)):
                    with self.assertRaises(GameConnectionError) as caught:
                        await getattr(game, method)()
                await game._handle_connection_error(caught.exception)
                self.assertEqual(game._state["state"], state.value)
                self.assertEqual(game._state["connection_requested"], retry)
                self.assertTrue(game._state["error_details"])

    async def test_pairing_service_requests_have_time_limits(self):
        for method, response in (
            ("get_access_token", {"ticket": "test-ticket"}),
            ("send_pairing_code", {"pairingUrl": "https://127.0.0.1:8080", "tlsCertificate": None}),
            ("send_initiate_punch_pairing", None),
        ):
            with self.subTest(method=method):
                game = self.make_game(JustDanceGameV2)
                with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=self.http_session(json_body=response)) as factory:
                    await getattr(game, method)()
                self.assertEqual(factory.call_args.kwargs["timeout"].total, JD_SERVICE_TIMEOUT)

    async def test_service_transport_failures_keep_the_service_error_status(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                session = self.http_session()
                session.__aenter__.side_effect = aiohttp.ClientConnectionError("Service offline")
                with patch("joydance.games.justdance_abstract.aiohttp.ClientSession", return_value=session):
                    await game.pair()
                self.assertEqual(game._state["state"], PairingState.ERROR_CONNECTION.value)
                self.assertIn("Service offline", game._state["error_details"])

    async def test_pairing_uses_the_listener_callback_in_both_protocols(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                game.requires_punch_pairing = True
                game.pairing_url = "wss://127.0.0.1:8080/smartphone"
                with (
                    patch.object(game, "get_access_token", new=AsyncMock()),
                    patch.object(game, "send_pairing_code", new=AsyncMock()),
                    patch.object(game, "hole_punching", new=AsyncMock()) as punch,
                    patch.object(game, "connect_ws", new=AsyncMock()) as connect,
                ):
                    await game.pair()
                punch.assert_awaited_once_with(on_listening=game.send_initiate_punch_pairing)
                connect.assert_awaited_once()

    async def test_empty_pong_replies_keep_both_protocols_connected(self):
        for game_class, protocol in ((JustDanceGameV1, "v1"), (JustDanceGameV2, "v2")):
            with self.subTest(protocol=protocol):
                game = self.make_game(game_class)
                heartbeats, release = asyncio.Event(), asyncio.Event()
                ping_payloads = []

                async def server(websocket):
                    original_send = websocket.protocol.send_frame

                    def reply(frame):
                        if frame.opcode is Opcode.PONG:
                            ping_payloads.append(frame.data)
                            frame = Frame(Opcode.PONG, b"")
                            if len(ping_payloads) >= 3:
                                heartbeats.set()
                        original_send(frame)

                    with patch.object(websocket.protocol, "send_frame", side_effect=reply):
                        await websocket.recv()
                        await websocket.send(json.dumps({"__class": "JD_PhoneDataCmdHandshakeContinue", "phoneID": 1, "protocolVersion": 5}))
                        await websocket.recv()
                        await websocket.send(json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 1}))
                        await websocket.recv()
                        await asyncio.wait_for(await websocket.ping(b"PING_MSG\0"), 1)
                        await release.wait()

                def connect_fast(uri, **kwargs):
                    kwargs.update(ping_interval=0.02, ping_timeout=0.03, close_timeout=0.02)
                    return websocket_connect(uri, **kwargs)

                async with serve(server, "127.0.0.1", 0, subprotocols=[f"{protocol}.phonescoring.jd.ubisoft.com"], close_timeout=0.05) as listener:
                    game.pairing_url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/smartphone"
                    with patch(game_class.__module__ + ".connect", side_effect=connect_fast):
                        task = asyncio.create_task(game.connect_ws())
                        try:
                            await asyncio.wait_for(heartbeats.wait(), 1)
                            await asyncio.sleep(0.05)
                            self.assertTrue(game.is_connected)
                            self.assertFalse(task.done())
                            self.assertTrue(all(payload == b"" for payload in ping_payloads))
                        finally:
                            release.set()
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

    async def test_silent_local_server_is_detected_after_successful_handshake(self):
        game = self.make_game(JustDanceGameV1)
        ready, release = asyncio.Event(), asyncio.Event()

        async def server(websocket):
            hello = json.loads(await websocket.recv())["root"]
            self.assertEqual(hello["clientVersion"], "0.1")
            await websocket.send(json.dumps({"__class": "JD_PhoneDataCmdHandshakeContinue", "phoneID": 1, "protocolVersion": 3}))
            self.assertEqual(json.loads(await websocket.recv())["root"]["__class"], "JD_PhoneDataCmdSync")
            await websocket.send(json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 1}))
            await websocket.recv()
            websocket.transport.pause_reading()
            ready.set()
            try:
                await release.wait()
            finally:
                websocket.transport.resume_reading()

        def connect_fast(uri, **kwargs):
            kwargs.update(ping_interval=0.02, ping_timeout=0.03, close_timeout=0.02)
            return websocket_connect(uri, **kwargs)

        async with serve(server, "127.0.0.1", 0, subprotocols=["v1.phonescoring.jd.ubisoft.com"], close_timeout=0.05) as listener:
            game.pairing_url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/smartphone"
            with patch("joydance.games.justdance_v1.connect", side_effect=connect_fast):
                task = asyncio.create_task(game.connect_ws())
                try:
                    await asyncio.wait_for(ready.wait(), 1)
                    self.assertTrue(game.is_connected)
                    await asyncio.wait_for(task, 1)
                    self.assertFalse(game.is_connected)
                    self.assertIn("keepalive", game._state["error_details"])
                    self.assertEqual(game._state["state"], PairingState.ERROR_CONSOLE_CONNECTION.value)
                    self.assertTrue(game._state["connection_requested"])
                finally:
                    release.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
