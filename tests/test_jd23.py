import asyncio
import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import hashlib
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import uuid

import aiohttp
from aioquic.asyncio import connect, serve
from aioquic.quic.configuration import QuicConfiguration
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from websockets.protocol import State

from joydance.config_handler import ConfigHandler, is_valid_pairing_code
from joydance.constants import Command, PairingState
from joydance.games.harbour_connection import HarbourConnection, HarbourProtocol
from joydance.games.jd23_auth import EFDS_BUILD_KEY, JD23Pairing, efds_ticket, mobile_token, server_info_url
from joydance.games.justdance_2023 import JustDanceGame2023
from joydance.utils import GameConnectionError


IDS = [str(uuid.UUID(int=n)) for n in (1, 2, 3)]


class JD23ConfigTests(unittest.TestCase):
    def test_codes_are_method_specific_and_survive_saved_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "config.cfg")
            config = ConfigHandler(path)
            settings = {"pairing_method": "jd23", "pairing_code": "abcd1234", "name": "JD23+"}
            self.assertTrue(config.is_new_config_valid(settings))
            config.data = settings
            connection = config.save_connection(settings)
            reloaded = ConfigHandler(path)
            self.assertEqual(reloaded.data["pairing_code"], "ABCD1234")
            self.assertEqual(reloaded.connections, [connection])
        for code in ("123456", "1234567", "ABCD12345", "ABCD123\n", "ＡBCD1234", None, 12345678):
            self.assertFalse(is_valid_pairing_code(code, "jd23"))
        self.assertTrue(is_valid_pairing_code("012345"))
        self.assertFalse(is_valid_pairing_code("012345\n"))
        self.assertFalse(is_valid_pairing_code("ABCD1234"))


class JD23AuthTests(unittest.IsolatedAsyncioTestCase):
    def test_token_matches_frozen_apk_model_fixture(self):
        with (patch("joydance.games.jd23_auth.SDK_APPLICATION_ID", "11111111-2222-3333-4444-555555555555"),
              patch("joydance.games.jd23_auth.SDK_SIGNING_KEY", b"synthetic-key")):
            self.assertEqual(mobile_token("00112233-4455-6677-8899-aabbccddeeff", 1793318400, 0),
                             "UbiMobile_v2 t=MDAxMTIyMzMtNDQ1NS02Njc3LTg4OTktYWFiYmNjZGRlZWZmOlpFakhhTHRTdldsVDBqUXNwOWdOZEVaNllWZzZSSmwvd0lYRmtwSmRnZU09OjExMTExMTExLTIyMjItMzMzMy00NDQ0LTU1NTU1NTU1NTU1NTpSM1ZsYzNSZmFFNTZjR2RoOk1qQXlOaTB4TUMwek1GUXdNRG93TUZvPQ==")

    def test_efds_envelope_matches_long_scrambled_reference_fixture(self):
        with patch("joydance.games.jd23_auth.EFDS_BUILD_KEY", bytes(range(32))):
            self.assertEqual(efds_ticket("X" * 100, b"12345678", 1793318400),
                             "Efds_v1 xddcjhmxbTpAwOALbj2EyJU6zyxL+1RaMLVnolksSiO7jnFiCLr7OSR+vFd8AXQXmcLu7d70rLjWs0ykWsUwk68XUVtK4GpCX484h68aLscTSx1DpDSCsdL9Sovs0rVK9FsZ64rJER7pe49nDKeC5yAdsFOgw20xMjM0NTY3OIiQmKDCU2bA")

    def test_efds_uses_runtime_observed_build_config_instead_of_sdk_fallback(self):
        self.assertEqual(hashlib.sha256(EFDS_BUILD_KEY).hexdigest(),
                         "8f109a7499201e336c1366c8ec2547962be43801345c07d2c9877856af1f36d0")
        frame = bytearray(base64.b64decode(efds_ticket("X" * 2100, b"12345678", 1793318400)[8:]))
        for index in range(len(frame) - 70, -1, -1):
            frame[index] ^= (frame[index + 69] << 5) & 255
        self.assertEqual(frame[:8], bytes.fromhex("c5971c6ef9b18dda"))
        self.assertEqual(struct.unpack_from(">I", frame, 8)[0], 2107)

    def test_server_info_requires_authenticated_service_domains_and_https(self):
        for url in ("http://prod.just-dance.com", "https://prod.just-dance.com.evil.example",
                    "https://user:pass@prod.just-dance.com", "https://127.0.0.1", "https://prod.just-dance.com?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                server_info_url({"parameters": [{"name": "ServerInfo", "fields": {"url": url}}]})
        self.assertEqual(server_info_url({"parameters": [{"name": "ServerInfo", "fields": {"url": "https://prod.just-dance.com/"}}]}),
                         "https://prod.just-dance.com")

    async def test_bootstrap_order_ticket_boundaries_and_no_mobile_registration(self):
        pairing = JD23Pairing()
        self.assertNotEqual(pairing.user_id, JD23Pairing().user_id)
        replies = [dict(ticket="synthetic-ticket", sessionId=IDS[0], spaceId=IDS[1], profileId=IDS[2]),
                   {"populations": [{"subject": "us_staging", "name": "prod"}]},
                   {"parameters": {"us-staging": {"fields": {"spaceId": IDS[2]}}}},
                   {"populations": [{"subject": "environment", "name": "prod"}]},
                   {"parameters": {"ServerInfo": {"fields": {"url": "https://prod-next.just-dance.com"}}}},
                   {}, {"deviceIP": "192.168.1.3", "devicePort": 1234}]
        requests = []
        session = MagicMock()
        session.__aenter__.return_value = session

        def request(method, url, **kwargs):
            requests.append((method, url, {**kwargs, "headers": dict(kwargs["headers"])}))
            response = MagicMock()
            response.status = 200
            response.json = AsyncMock(return_value=replies[len(requests) - 1])
            response.__aenter__.return_value = response
            return response

        session.request.side_effect = request
        on_pairing = AsyncMock()
        with patch("joydance.games.jd23_auth.aiohttp.ClientSession", return_value=session) as client_session:
            result = await pairing.pair("ABCD1234", on_pairing)
        self.assertEqual(client_session.call_args.kwargs["headers"]["User-Agent"],
                         "UbiServices_SDK_Unity_2024.Release.18_ANDROID64_dynamic")
        self.assertEqual(result["deviceIP"], "192.168.1.3")
        self.assertEqual([m for m, _, _ in requests], ["POST", "PUT", "GET", "PUT", "GET", "POST", "GET"])
        self.assertTrue(requests[0][1].endswith("/v3/profiles/sessions"))
        self.assertTrue(requests[0][2]["headers"]["Authorization"].startswith("UbiMobile_v2 t="))
        self.assertEqual(requests[0][2]["headers"]["Ubi-RequestedPlatformType"], "ubimobile")
        self.assertEqual(requests[1][2]["headers"]["Authorization"], "Ubi_v1 t=synthetic-ticket")
        self.assertTrue(requests[1][1].endswith("/v1/profiles/me/populations/data"))
        self.assertEqual(requests[1][2]["json"]["spaceId"], IDS[1])
        self.assertEqual(requests[1][2]["json"]["data"], {"environment": "main_27.0.0-418218_Android_Final_logical-chain_260923"})
        self.assertEqual(requests[2][2]["params"], {"parameterGroups": "us-staging"})
        self.assertEqual(requests[2][2]["headers"]["Ubi-Populations"], "us_staging.prod")
        self.assertEqual(requests[3][2]["json"]["spaceId"], IDS[2])
        self.assertTrue(requests[4][1].endswith(f"/v2/spaces/{IDS[2]}/parameters"))
        self.assertEqual(requests[4][2]["params"], {"parameterGroups": "ServerInfo"})
        self.assertEqual(requests[4][2]["headers"]["Ubi-Populations"], "us_staging.prod,environment.prod")
        self.assertTrue(requests[5][1].endswith("/session/v2/session"))
        self.assertTrue(requests[5][2]["headers"]["Authorization"].startswith("Efds_v1 "))
        self.assertEqual(requests[6][2]["headers"]["Ubi-SessionId"], IDS[0])
        self.assertEqual(requests[6][2]["params"], {"code": "ABCD1234"})
        self.assertFalse(any("mobile-info" in url for _, url, _ in requests))
        self.assertTrue(all(kwargs["allow_redirects"] is False for _, _, kwargs in requests))
        on_pairing.assert_awaited_once()

    async def test_population_header_rejects_malformed_or_injected_assignments(self):
        for assignments in (None, [], {}, [{}], [{"subject": "us_staging", "name": "prod\r\nAuthorization: secret"}],
                            [{"subject": "us_staging", "name": "prod"}] * 33):
            pairing = JD23Pairing()
            pairing._request = AsyncMock(return_value={"populations": assignments})
            headers, populations = {"Ubi-Populations": "existing.prod"}, {"existing": "prod"}
            with self.subTest(assignments=assignments), self.assertRaises(ValueError):
                await pairing._assign_environment(None, IDS[1], headers, populations)
            self.assertEqual(headers, {"Ubi-Populations": "existing.prod"})
            self.assertEqual(populations, {"existing": "prod"})

    async def test_service_errors_do_not_expose_response_bodies_or_credentials(self):
        for status, stage, code_request, retry, state in (
            (403, "Ubisoft login", False, False, PairingState.ERROR_CONNECTION),
            (404, "pairing-code lookup", True, False, PairingState.ERROR_INVALID_PAIRING_CODE),
            (429, "Ubisoft ServerInfo lookup", False, True, PairingState.ERROR_CONNECTION),
            (503, "EFDS session creation", False, True, PairingState.ERROR_CONNECTION),
        ):
            session, response = MagicMock(), MagicMock(status=status)
            response.content.read = AsyncMock(return_value=json.dumps({
                "errorCode": 4000, "message": "secret-ticket", "errorContext": "secret-code",
            }).encode())
            response.__aenter__.return_value = response
            session.request.return_value = response
            with self.subTest(status=status), self.assertRaises(GameConnectionError) as caught:
                await JD23Pairing()._request(session, "GET", "https://prod.just-dance.com?code=secret-code",
                                             stage=stage, code_request=code_request)
            self.assertEqual(caught.exception.state, state)
            self.assertEqual(caught.exception.should_reconnect, retry)
            self.assertNotIn("secret", str(caught.exception))
            if not code_request:
                self.assertIn(stage, str(caught.exception))
                self.assertIn(f"HTTP {status}, service error 4000", str(caught.exception))
            response.content.read.assert_awaited_once_with(4096)
            response.json.assert_not_called()

    async def test_unreadable_or_unsafe_error_details_preserve_http_failure(self):
        for body in (b"<html>Forbidden</html>", b'{"errorCode":', b"[]",
                     b'{"errorCode":"secret-ticket"}', b'{"errorCode":true}',
                     b'{"errorCode":9999999}', asyncio.TimeoutError(), aiohttp.ClientPayloadError()):
            session, response = MagicMock(), MagicMock(status=403)
            response.content.read = AsyncMock(**({"side_effect": body} if isinstance(body, Exception)
                                                else {"return_value": body}))
            response.__aenter__.return_value = response
            session.request.return_value = response
            with self.subTest(body=body), self.assertRaises(GameConnectionError) as caught:
                await JD23Pairing()._request(session, "POST", "https://prod.just-dance.com", stage="Ubisoft login")
            self.assertEqual(str(caught.exception), "JD23+ Ubisoft login failed (HTTP 403).")


class HarbourFrameTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        reader = asyncio.StreamReader()
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        protocol = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        return HarbourConnection(protocol, reader, writer)

    async def test_framing_uses_utf8_byte_length_and_handles_fragmented_coalesced_frames(self):
        connection = self.connection()
        await connection.send('{"name":"é"}')
        data = connection.writer.write.call_args.args[0]
        self.assertEqual(struct.unpack("<H", data[:2])[0], len(data) - 2)
        self.assertTrue(data.endswith(b"\0"))

        async def feed():
            for byte in data + b"\x04\0a\0b\0":
                connection.reader.feed_data(bytes([byte]))
                await asyncio.sleep(0)
            connection.reader.feed_eof()

        task = asyncio.create_task(feed())
        self.assertEqual([message async for message in connection], ['{"name":"é"}', "a", "b"])
        await task

    async def test_invalid_or_truncated_frames_fail_instead_of_hanging(self):
        for wire in (b"\0\0", b"\xff\xff", b"\x01", b"\x02\0x", b"\x01\0x"):
            connection = self.connection()
            connection.reader.feed_data(wire)
            connection.reader.feed_eof()
            with self.subTest(wire=wire), self.assertRaises(GameConnectionError):
                await anext(connection.__aiter__())
        connection = self.connection()
        for text in ("x" * 32768, "a\0b"):
            with self.assertRaises(ValueError):
                await connection.send(text)


class JD23GameTests(unittest.IsolatedAsyncioTestCase):
    def make_game(self):
        controller = SimpleNamespace(serial="controller-test", is_connected=Mock(return_value=True),
                                     set_accel_collection=Mock(), set_player_led=AsyncMock(),
                                     animate_connecting_led=AsyncMock(), get_latest_command=Mock(return_value=None),
                                     get_joystick_command=Mock(return_value=None), get_accel_events=Mock(return_value=[]),
                                     available_shortcuts=set(), rumble_enabled=True,
                                     handle_rumble_on_sound_index=AsyncMock())
        game = JustDanceGame2023(controller, pairing_code="ABCD1234", on_state_changed=AsyncMock(), on_game_message=AsyncMock())
        game.ws = SimpleNamespace(send=AsyncMock(), state=State.OPEN, close=AsyncMock())
        game.attempt_reconnect = AsyncMock()
        self.addAsyncCleanup(game.disconnect, should_reconnect=False)
        return game

    async def receive(self, game, name, param="", category=1):
        await game.on_message(json.dumps(dict(commandCategory=category, commandName=name, commandParameter=param)))

    def sent(self, game):
        return [json.loads(call.args[0]) for call in game.ws.send.await_args_list]

    async def handshake(self, game, mode="HandDevice", enabled="false", main=True):
        await self.receive(game, "Connect_HandshakeBackFromServer", "0", 0)
        await self.receive(game, "Connect_HandshakeOKFromServerAs" + ("MainPlayer" if main else "Guest"),
                           f"Player/{enabled}/{mode}/1", 0)

    async def test_handshake_marks_connected_only_after_server_ack_and_stops_connecting_led(self):
        game = self.make_game()
        await game.send_hello()
        await self.receive(game, "Connect_HandshakeBackFromServer", "0", 0)
        self.assertFalse(game.is_connected)
        self.assertEqual(self.sent(game)[1]["commandParameter"], "userId=[Unknown Player]platform=[Android]")
        with patch.object(game, "_stop_connecting_led", new=AsyncMock()) as stop:
            await self.receive(game, "Connect_HandshakeOKFromServerAsMainPlayer", "Player/false/HandDevice/1", 0)
        stop.assert_awaited_once()
        self.assertTrue(game.is_connected)
        game.controller.set_player_led.assert_awaited_once_with(2, flash=True)

    async def test_incompatible_version_and_refusals_are_not_retried(self):
        for name, param in (("Connect_HandshakeBackFromServer", "1"), ("Connect_DeviceConnectionRefusal", "GroupFull"),
                            ("Connect_HandshakeOKFromServerAsGuest", "Invalid")):
            game = self.make_game()
            with self.subTest(name=name), self.assertRaises(GameConnectionError) as caught:
                await self.receive(game, name, param, 0)
            self.assertFalse(caught.exception.should_reconnect)

    async def test_verified_feedback_triggers_once_on_join_and_on_menu_shortcuts(self):
        game = self.make_game()
        with self.assertLogs("joydance.games.justdance_2023", level="DEBUG") as logs:
            await self.handshake(game)
            await game._rumble_task
        game.controller.handle_rumble_on_sound_index.assert_awaited_once_with(8)
        self.assertTrue(any("feedback trigger='ConnectionAcknowledged' rumble_enabled=True" in line for line in logs.output))
        await self.receive(game, "Connect_HandshakeOKFromServerAsMainPlayer", "Player/false/HandDevice/1", 0)
        self.assertEqual(game.controller.handle_rumble_on_sound_index.await_count, 1)
        for command in ("NavigationBack", "NavigationFullscreen", "NavigationSweat", "NavigationQuickChat",
                        "NavigationOpenGroupBar", "NavigationChangeAvatar", "NavigationDohActivitiesBoard",
                        "NavigationDohDecadeSelector"):
            with self.subTest(command=command):
                game.controller.handle_rumble_on_sound_index.reset_mock()
                await game.send_message(command)
                await game._rumble_task
                game.controller.handle_rumble_on_sound_index.assert_awaited_once_with(8)

    async def test_feedback_respects_toggle_and_does_not_guess_gameplay_or_directional_cues(self):
        game = self.make_game()
        await self.handshake(game)
        await game._rumble_task
        game.controller.handle_rumble_on_sound_index.reset_mock()
        game.controller.rumble_enabled = False
        await game.send_message("NavigationBack")
        await game._stop_rumble()
        game.controller.rumble_enabled = True
        game.is_input_allowed = True
        await game.send_message("NavigationMoveDirection", {"parameter": '{"x":1,"y":0}'})
        await game.send_message("NavigationValidate")
        await self.receive(game, "Request_EnableScoring", "true")
        await self.receive(game, "Request_Ping")
        await game.send_message("NavigationPauseGame")
        game.controller.handle_rumble_on_sound_index.assert_not_awaited()
        game.ws.send.side_effect = OSError("Test failure")
        with self.assertRaises(OSError):
            await game.send_message("NavigationBack")
        self.assertIsNone(game._rumble_task)

    async def test_navigation_shortcut_master_gates_and_hold(self):
        game = self.make_game()
        await self.handshake(game, main=False)
        self.assertFalse(game.is_command_allowed(Command.ACCEPT))
        await self.receive(game, "Request_EnableNavigation", "true")
        name, data = await game.preprocess_command(Command.LEFT)
        await game.send_message(name, data)
        self.assertEqual([p["commandName"] for p in self.sent(game)[-3:]],
                         ["NavigationMoveStarted", "NavigationMoveDirection", "NavigationMoveEnded"])
        self.assertEqual(json.loads(self.sent(game)[-2]["commandParameter"]), {"x": -1, "y": 0})
        await self.receive(game, "Request_EnableScaShortcutButtonOnMasterOnly", "QuickChat")
        self.assertFalse(game.is_command_allowed(Command.JD23_QUICK_CHAT))
        await self.receive(game, "Request_EnableScaShortcutButton", "2")
        self.assertEqual(await game.preprocess_command(Command.JD23_GROUP), ("NavigationChangeAvatar", {}))
        await self.receive(game, "Request_OnHold", "true")
        self.assertFalse(game.is_command_allowed(Command.JD23_GROUP))
        await self.receive(game, "Request_OnHold", "false")
        await self.receive(game, "Request_DisableScaShortcutButton", "GroupBarOrAvatarChange")
        self.assertFalse(game.is_command_allowed(Command.JD23_GROUP))

    async def test_navigation_revoked_mid_gesture_still_sends_end(self):
        game = self.make_game()
        await self.handshake(game)
        game.is_input_allowed = True
        name, data = await game.preprocess_command(Command.RIGHT)

        async def revoke(packet):
            if json.loads(packet)["commandName"] == "NavigationMoveStarted":
                game.is_input_allowed = False

        game.ws.send.side_effect = revoke
        game.ws.send.reset_mock()
        await game.send_message(name, data)
        self.assertEqual([p["commandName"] for p in self.sent(game)], ["NavigationMoveStarted", "NavigationMoveEnded"])

    async def test_motion_is_gated_ordered_and_never_replays_a_previous_song(self):
        game = self.make_game()
        await self.handshake(game)
        game.accel_data = [(999, 999, 999)]
        await self.receive(game, "Request_EnableScoring", "true")
        self.assertEqual(game.accel_data, [])
        game.accel_data = [(n, -n, n / 10) for n in range(23)]
        await game.send_accelerometer_data(1)
        packets = [p for p in self.sent(game) if p["commandName"] == "MotionScoringData"]
        self.assertEqual([len(json.loads(p["commandParameter"])["AccelerometerData"]) for p in packets], [10, 10, 3])
        self.assertEqual(json.loads(packets[0]["commandParameter"])["AccelerometerData"][0], {"x": 0, "y": 0, "z": 0})
        self.assertEqual(set(json.loads(packets[0]["commandParameter"])), {"AccelerometerData"})
        self.assertTrue(game.is_command_allowed(Command.PAUSE))
        self.assertFalse(game.is_command_allowed(Command.ACCEPT))
        await self.receive(game, "Request_EnableScoring", "false")
        game.controller.set_accel_collection.assert_called_with(False)
        game.ws.send.reset_mock()
        await game.send_accelerometer_data(10)
        game.ws.send.assert_not_awaited()

    async def test_stop_scoring_during_send_discards_remaining_batches(self):
        game = self.make_game()
        await self.handshake(game, enabled="true")
        game.accel_data = [(1, 2, 3)] * 21

        async def stop(*args):
            game._set_scoring(False)

        game.ws.send.side_effect = stop
        game.ws.send.reset_mock()
        with self.assertLogs("joydance.games.justdance_2023", level="DEBUG") as logs:
            await game.send_accelerometer_data(0)
        self.assertEqual(game.ws.send.await_count, 1)
        self.assertTrue(any("completed_after_stop batches=1 samples=10" in line for line in logs.output))

    async def test_camera_selection_requests_hand_device_without_faking_calibration(self):
        game = self.make_game()
        await self.handshake(game, mode="Camera", enabled="true")
        self.assertFalse(game.should_start_accelerometer)
        self.assertEqual(self.sent(game)[-1], dict(commandCategory=3, commandName="ScoringModeChange", commandParameter="0/True"))
        with self.assertRaisesRegex(GameConnectionError, "hand-device"):
            await self.receive(game, "CalibrateCameraScoring ")

    async def test_heartbeat_period_and_timeout(self):
        game = self.make_game()
        await self.handshake(game)
        await self.receive(game, "Request_SetLongPing", "true")
        await self.receive(game, "Request_Ping")
        game._last_pong = time_zero = 100
        game._last_ping = time_zero
        task = None
        with patch("joydance.games.justdance_2023.time", SimpleNamespace(monotonic=lambda: 102)):
            task = asyncio.create_task(game._heartbeat())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.sent(game)[-1], dict(commandCategory=0, commandName="Connect_PingBack", commandParameter="100"))
        game._last_ping = 0
        with self.assertRaisesRegex(GameConnectionError, "heartbeats"):
            await game._heartbeat()

    async def test_command_logs_include_unknown_feedback_without_private_payloads_or_ping_spam(self):
        game = self.make_game()
        with self.assertLogs("joydance.games.justdance_2023", level="DEBUG") as logs:
            await self.receive(game, "Connect_HandshakeBackFromServer", "0", 0)
            await self.receive(game, "Connect_HandshakeOKFromServerAsGuest", "Private Player/false/HandDevice/1", 0)
            await self.receive(game, "Request_EnableNavigation", "true")
            await self.receive(game, "Audio_Play", '{"token":"secret-ticket"}', 7)
            await self.receive(game, "Haptics_Play", "https://private.example/secret", 7)
            await game.send_message("NavigationValidate")
            for _ in range(8):
                await self.receive(game, "Request_Ping")
                await game.send_message("Connect_PingBack", {"category": 0, "parameter": "10"})
        text = "\n".join(logs.output)
        self.assertIn("<<< category=7 command='Audio_Play'", text)
        self.assertIn("command='Haptics_Play'", text)
        self.assertIn("command='Request_EnableNavigation' parameter='true'", text)
        self.assertIn(">>> category=2 command='NavigationValidate'", text)
        self.assertIn("parameter='[redacted]' parameter_chars=25", text)
        self.assertEqual(text.count("command='Request_Ping'"), 1)
        self.assertNotIn("Connect_PingBack", text)
        for private in ("Private Player", "secret-ticket", "private.example"):
            self.assertNotIn(private, text)

    async def test_motion_logs_aggregate_successful_batches_and_report_empty_frames_and_stop(self):
        game = self.make_game()
        clock = [100.0]
        with (patch("joydance.games.justdance_2023.time", SimpleNamespace(monotonic=lambda: clock[0])),
              self.assertLogs("joydance.games.justdance_2023", level="DEBUG") as logs):
            game._set_scoring(True)
            game.accel_data = [(98765.25, -98765.25, 0)] * 23
            await game.send_accelerometer_data(1)
            clock[0] = 100.5
            game.accel_data = [(98765.25, 0, 0)] * 7
            await game.send_accelerometer_data(2)
            await game.send_accelerometer_data(3)
            self.assertFalse(any("JD23+ motion" in line for line in logs.output))
            clock[0] = 101.25
            await game.send_accelerometer_data(1)
            clock[0] = 101.5
            game.accel_data = [(98765.25, 0, 0)] * 2
            await game.send_accelerometer_data(2)
            game._set_scoring(False)
        summaries = [line for line in logs.output if "JD23+ motion" in line]
        self.assertEqual(len(summaries), 2)
        self.assertIn("interval=1.250s batches=4 samples=30 samples_per_s=24.0 empty_frames=2 final=False", summaries[0])
        self.assertIn("interval=0.250s batches=1 samples=2", summaries[1])
        self.assertIn("final=True", summaries[1])
        self.assertNotIn("98765", "\n".join(logs.output))
        self.assertNotIn("MotionScoringData", "\n".join(logs.output))

    async def test_motion_logs_zero_samples_when_empty_and_exclude_closed_or_failed_sends(self):
        game = self.make_game()
        clock = [100.0]
        with (patch("joydance.games.justdance_2023.time", SimpleNamespace(monotonic=lambda: clock[0])),
              self.assertLogs("joydance.games.justdance_2023", level="DEBUG") as logs):
            game._set_scoring(True)
            clock[0] = 101.0
            await game.send_accelerometer_data(1)
            game.ws.state = State.CLOSED
            game.accel_data = [(1, 2, 3)] * 12
            await game.send_accelerometer_data(1)
            game.ws.state = State.OPEN
            game.ws.send.side_effect = OSError("Test failure")
            game.accel_data = [(1, 2, 3)] * 10
            with self.assertRaises(OSError):
                await game.send_accelerometer_data(1)
            game._reset_input_state()
        summaries = [line for line in logs.output if "JD23+ motion" in line]
        self.assertEqual(len(summaries), 2)
        self.assertIn("batches=0 samples=0 samples_per_s=0.0 empty_frames=1", summaries[0])
        self.assertIn("batches=0 samples=0", summaries[1])
        self.assertIn("final=True", summaries[1])

    async def test_bad_pairing_endpoint_fails_without_opening_quic(self):
        for address, port in (("8.8.8.8", 1234), ("127.0.0.1", 1234), ("192.168.1.2", 0), ("192.168.1.2", True)):
            game = self.make_game()
            game.pairing.pair = AsyncMock(return_value={"deviceIP": address, "devicePort": port})
            game.connect_ws = AsyncMock()
            game._handle_connection_error = AsyncMock()
            await game.pair()
            game.connect_ws.assert_not_awaited()
            self.assertFalse(game._handle_connection_error.call_args.args[0].should_reconnect)

    async def test_application_selects_jd23_and_cleans_up_replaced_session(self):
        import dance

        game = self.make_game()
        game.ws = None
        config = SimpleNamespace(data={}, validate_config_with_error=ConfigHandler.validate_config_with_error)
        previous = SimpleNamespace(disconnect=AsyncMock())
        app = {"controllers": {game.controller.serial: game.controller}, "controllers_info": defaultdict(dict),
               "joydance_connections": {game.controller.serial: previous}, "websockets": set(),
               "joydance_connection_locks": defaultdict(asyncio.Lock), "config_handler": config}

        def factory(**kwargs):
            result = JustDanceGame2023(**kwargs)
            result.pair = asyncio.Event().wait
            self.addAsyncCleanup(result.disconnect, should_reconnect=False)
            return result

        data = dict(controller_serial=game.controller.serial, pairing_method="jd23", pairing_code="abcd1234",
                    host_ip_addr="", console_ip_addr="192.168.1.3")
        with patch.object(dance, "JustDanceGame2023", new=factory):
            await dance.connect_controller(app, None, data)
        result = app["joydance_connections"][game.controller.serial]
        self.assertIsInstance(result, JustDanceGame2023)
        self.assertEqual(result.pairing_code, "ABCD1234")
        self.assertEqual(app["controllers_info"][game.controller.serial]["pairing_code"], "ABCD1234")
        self.assertEqual(config.data["pairing_code"], "ABCD1234")
        self.assertIsNone(result.console_ip_addr)
        previous.disconnect.assert_awaited_once_with(should_reconnect=False)


def certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-test-console")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("local-test-console")]), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


class HarbourQuicTests(unittest.IsolatedAsyncioTestCase):
    async def test_game_session_handshake_motion_and_manual_disconnect_over_real_quic(self):
        key, cert = certificate()
        config = QuicConfiguration(is_client=False, alpn_protocols=["ubisoft.harbour.keel/1.0"])
        config.certificate, config.private_key = cert, key
        motion = asyncio.get_running_loop().create_future()
        tasks = []

        async def handle(reader, writer):
            async def receive():
                size = struct.unpack("<H", await reader.readexactly(2))[0]
                payload = await reader.readexactly(size)
                self.assertEqual(payload[-1:], b"\0")
                return json.loads(payload[:-1])

            def send(category, name, parameter):
                payload = json.dumps(dict(commandCategory=category, commandName=name, commandParameter=parameter)).encode() + b"\0"
                writer.write(struct.pack("<H", len(payload)) + payload)

            try:
                self.assertEqual((await receive())["commandName"], "Connect_HelloFromClient")
                send(0, "Connect_HandshakeBackFromServer", "0")
                self.assertEqual((await receive())["commandParameter"], "userId=[Unknown Player]platform=[Android]")
                send(0, "Connect_HandshakeOKFromServerAsMainPlayer", "Test Player/false/HandDevice/0")
                send(1, "Request_EnableScoring", "true")
                await writer.drain()
                packet = await receive()
                self.assertEqual(packet["commandName"], "MotionScoringData")
                motion.set_result(json.loads(packet["commandParameter"])["AccelerometerData"])
                await reader.read()
            except Exception as error:
                if not motion.done():
                    motion.set_exception(error)
                else:
                    raise
            finally:
                writer.close()

        server = await serve("127.0.0.1", 0, configuration=config,
                             stream_handler=lambda r, w: tasks.append(asyncio.create_task(handle(r, w))))
        self.addCleanup(server.close)
        game = JD23GameTests.make_game(self)
        game.ws = None
        game.console_endpoint = ("127.0.0.1", server._transport.get_extra_info("sockname")[1])
        pending = [(0.25, -0.5, 1.0)]
        game.controller.get_accel_events.side_effect = lambda: [pending.pop()] if pending else []
        with patch("joydance.games.justdance_2023.ROOT_CA", cert.public_bytes(Encoding.PEM)):
            task = asyncio.create_task(game.connect_ws())
            try:
                self.assertEqual(await asyncio.wait_for(motion, 3), [{"x": 0.25, "y": -0.5, "z": 1.0}])
                self.assertTrue(game.is_connected)
                await game.disconnect(should_reconnect=False)
                await asyncio.wait_for(task, 3)
                self.assertFalse(game.is_connected)
                self.assertFalse(game.should_start_accelerometer)
                self.assertIsNone(game.ws)
                self.assertIsNone(game.reconnection_task)
            finally:
                task.cancel()
                await asyncio.gather(task, *tasks, return_exceptions=True)

    async def test_real_quic_v1_alpn_stream_zero_framing_and_ca_verification(self):
        key, cert = certificate()
        server_config = QuicConfiguration(is_client=False, alpn_protocols=["ubisoft.harbour.keel/1.0"])
        server_config.certificate, server_config.private_key = cert, key
        received = asyncio.get_running_loop().create_future()
        tasks = []

        async def handle(reader, writer):
            try:
                self.assertEqual(writer.get_extra_info("stream_id"), 0)
                header = await reader.readexactly(2)
                payload = await reader.readexactly(struct.unpack("<H", header)[0])
                received.set_result(payload)
                for chunk in (b"\x03", b"\0", b"ok", b"\0"):
                    writer.write(chunk)
                    await writer.drain()
                writer.write_eof()
                writer.close()
            except Exception as error:
                if not received.done():
                    received.set_exception(error)

        server = await serve("127.0.0.1", 0, configuration=server_config, retry=True,
                             stream_handler=lambda r, w: tasks.append(asyncio.create_task(handle(r, w))))
        self.addCleanup(server.close)
        port = server._transport.get_extra_info("sockname")[1]
        config = QuicConfiguration(is_client=True, alpn_protocols=["ubisoft.harbour.keel/1.0"], cadata=cert.public_bytes(Encoding.PEM))
        async with connect("127.0.0.1", port, configuration=config, create_protocol=HarbourProtocol) as protocol:
            reader, writer = await protocol.create_stream()
            connection = HarbourConnection(protocol, reader, writer)
            await connection.send("hello")
            self.assertEqual(await asyncio.wait_for(received, 2), b"hello\0")
            self.assertEqual([value async for value in connection], ["ok"])
            await connection.close()
        await asyncio.gather(*tasks)

        # A console with the wrong CA must still be rejected despite skipping the LAN hostname.
        _, other_ca = certificate()
        config = QuicConfiguration(is_client=True, alpn_protocols=["ubisoft.harbour.keel/1.0"], cadata=other_ca.public_bytes(Encoding.PEM))
        with self.assertRaises(ConnectionError):
            async with connect("127.0.0.1", port, configuration=config, create_protocol=HarbourProtocol):
                self.fail("Untrusted console connected")
