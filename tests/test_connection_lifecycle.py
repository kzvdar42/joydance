import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.protocol import State

from joydance.constants import Command, PairingState
from joydance.controllers.joycon.pycon.constants import (
    JOYCON_L_PRODUCT_ID,
    JOYCON_VENDOR_ID,
)
from joydance.controllers.joycon.pycon.joycon import JoyCon
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2
from joydance.utils import ControllerConnectionError


class ConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_from_song_accepts_pause_menu_input(self):
        controller = SimpleNamespace(
            serial="test-controller",
            is_connected=Mock(return_value=True),
            set_player_led=AsyncMock(),
            get_joystick_command=Mock(return_value=None),
        )

        def button(commands_to_check=None):
            if commands_to_check is None or Command.ACCEPT in commands_to_check:
                return Command.ACCEPT
            return None

        controller.get_latest_command = Mock(side_effect=button)
        game = JustDanceGameV1(
            controller, on_state_changed=AsyncMock(), on_game_message=AsyncMock()
        )
        game.ws = SimpleNamespace(state=State.OPEN, close=AsyncMock())
        game.is_connected = True
        game.should_start_accelerometer = True
        game.is_input_allowed = True
        game.accel_data = [(1, 2, 3)]
        await game.disconnect(should_reconnect=False)

        game.ws = SimpleNamespace(state=State.OPEN, send=AsyncMock(), close=AsyncMock())
        await game.on_message(
            json.dumps({"__class": "JD_PhoneDataCmdSyncEnd", "phoneID": 1})
        )
        await game.on_message(
            json.dumps(
                {
                    "__class": "JD_PhoneUiSetupData",
                    "isPopup": 1,
                    "inputSetup": {"isEnabled": 1},
                    "setupData": {
                        "mainCarousel": {
                            "rows": [{"items": [{"actions": [{"command": ""}]}]}]
                        }
                    },
                }
            )
        )

        sent = asyncio.Event()
        with patch.object(
            game, "send_message", new=AsyncMock(side_effect=lambda *args: sent.set())
        ) as send:
            task = asyncio.create_task(game.send_command())
            try:
                await asyncio.wait_for(sent.wait(), 1)
                send.assert_awaited_once_with(
                    "ValidateAction_PhoneCommandData",
                    {"rowIndex": 0, "itemIndex": 0, "actionIndex": 0},
                )
                controller.get_latest_command.assert_called_once_with()
                self.assertEqual(game.accel_data, [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_manual_stop_cancels_token_request_and_retry_delay(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for phase in ("token_request", "retry_delay"):
                with self.subTest(protocol=game_class.__name__, phase=phase):
                    controller = SimpleNamespace(
                        serial="test-controller",
                        is_connected=Mock(return_value=True),
                        set_player_led=AsyncMock(),
                        get_state=AsyncMock(return_value={"serial": "test-controller"}),
                    )
                    on_state_changed = AsyncMock()
                    game = game_class(controller, on_state_changed=on_state_changed)
                    game.reconnection_start_retry_delay = 30
                    token_started = asyncio.Event()
                    token_release = asyncio.Event()

                    async def get_token():
                        token_started.set()
                        await token_release.wait()
                        await game.on_state_changed(
                            {"state": PairingState.ERROR_CONNECTION.value},
                        )
                        raise RuntimeError("Token service unavailable")

                    with (
                        patch.object(
                            game, "get_access_token", side_effect=get_token
                        ) as token,
                        patch.object(
                            game, "send_pairing_code", new=AsyncMock()
                        ) as pairing,
                        patch("joydance.games.justdance_v1.logger"),
                        patch("joydance.games.justdance_v2.logger"),
                    ):
                        retry = asyncio.create_task(game.attempt_reconnect())
                        game.reconnection_task = retry
                        try:
                            await asyncio.wait_for(token_started.wait(), 1)
                            pair = game.pair_task
                            if phase == "retry_delay":
                                token_release.set()
                                await asyncio.wait_for(asyncio.shield(pair), 1)
                            self.assertTrue((await game.get_state())["connection_requested"])
                            await asyncio.wait_for(
                                game.disconnect(should_reconnect=False), 1
                            )
                            self.assertTrue(retry.cancelled())
                            self.assertTrue(pair.done())
                            self.assertIsNone(game.reconnection_task)
                            self.assertIsNone(game.pair_task)
                            state = await game.get_state()
                            self.assertFalse(state["connection_requested"])
                            self.assertEqual(
                                state["state"], PairingState.DISCONNECTED.value
                            )
                            on_state_changed.assert_awaited_with(
                                controller.serial, state
                            )
                            token.assert_awaited_once()
                            pairing.assert_not_awaited()
                        finally:
                            token_release.set()
                            await game.disconnect(should_reconnect=False)

    async def test_connection_refused_is_reported_as_console_error(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                controller = SimpleNamespace(serial="test-controller")
                game = game_class(controller, on_state_changed=AsyncMock())
                game.pairing_url = "ws://127.0.0.1:8080/smartphone"
                with (
                    patch(
                        game_class.__module__ + ".connect",
                        side_effect=ConnectionRefusedError("Game is closed"),
                    ),
                    patch.object(game, "disconnect", new=AsyncMock()),
                    patch(game_class.__module__ + ".logger"),
                ):
                    await game.connect_ws()
                self.assertEqual(
                    game._state["state"], PairingState.ERROR_CONSOLE_CONNECTION.value
                )

    async def test_queued_connections_share_success_or_stop_safely(self):
        for action in ("connect", "cancel_queued", "close", "reconnect"):
            with self.subTest(action=action):
                joycon = JoyCon(
                    JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller"
                )
                setup_started = threading.Event()
                setup_release = threading.Event()

                def setup(generation, cancel):
                    setup_started.set()
                    if not setup_release.wait(2):
                        raise TimeoutError("Test did not release HID setup")
                    joycon._ensure_connection_attempt_is_current(generation, cancel)

                with (
                    patch.object(
                        joycon, "_connect_hid", side_effect=setup
                    ) as connect_hid,
                    patch.object(
                        joycon,
                        "_update_input_report",
                        side_effect=joycon.is_connected.set,
                    ),
                ):
                    first = asyncio.create_task(joycon.connect())
                    queued = None
                    try:
                        self.assertTrue(await asyncio.to_thread(setup_started.wait, 1))
                        queued = asyncio.create_task(joycon.connect())
                        await asyncio.sleep(0)
                        self.assertFalse(queued.done())
                        if action == "cancel_queued":
                            queued.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await queued
                            self.assertFalse(joycon._active_connection_cancel.is_set())
                            self.assertFalse(first.done())
                        elif action == "close":
                            joycon.close()

                        setup_release.set()
                        results = await asyncio.wait_for(
                            asyncio.gather(first, queued, return_exceptions=True), 1
                        )
                        if action == "close":
                            for result in results:
                                self.assertIsInstance(result, ControllerConnectionError)
                            self.assertTrue(joycon._closed)
                            self.assertFalse(joycon.is_connected.is_set())
                        else:
                            self.assertIs(results[0], True)
                            if action in ("connect", "reconnect"):
                                self.assertIs(results[1], True)
                        connect_hid.assert_called_once()
                        if action == "reconnect":
                            self.assertTrue(
                                await asyncio.wait_for(joycon.reconnect(), 1)
                            )
                            self.assertEqual(connect_hid.call_count, 2)
                        self.assertFalse(joycon._connect_lock.locked())
                    finally:
                        setup_release.set()
                        joycon.close()
                        tasks = [task for task in (first, queued) if task is not None]
                        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_controller_loss_finishes_disconnect_cleanup(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                controller = SimpleNamespace(
                    serial="test-controller",
                    is_connected=Mock(return_value=False),
                    set_player_led=AsyncMock(),
                )
                on_state_changed = AsyncMock()
                game = game_class(controller, on_state_changed=on_state_changed)
                game.is_connected = True
                close_started = asyncio.Event()
                close_finished = asyncio.Event()

                async def close():
                    close_started.set()
                    await asyncio.sleep(0.05)
                    close_finished.set()

                async def set_led(player_id):
                    await asyncio.sleep(0.05)

                controller.set_player_led.side_effect = set_led

                game.ws = SimpleNamespace(state=State.OPEN, close=close)
                with (
                    patch.object(game, "send_hello", new=AsyncMock()),
                    patch.object(
                        game, "_message_receive_loop", new=asyncio.Event().wait
                    ),
                    patch.object(game, "attempt_reconnect", new=AsyncMock()),
                ):
                    task = asyncio.create_task(game.run_connection_tasks())
                    try:
                        await asyncio.wait_for(close_started.wait(), 1)
                        # The command loop exits while socket close is still pending.
                        await asyncio.wait_for(asyncio.shield(task), 1)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        await game.stop_reconnection()

                controller.set_player_led.assert_awaited_once_with(0)
                self.assertTrue(close_finished.is_set())
                on_state_changed.assert_awaited_once_with(
                    controller.serial,
                    {"state": PairingState.DISCONNECTED.value, "connection_requested": True},
                )
                self.assertIsNone(game.ws)
                self.assertFalse(game.is_connected)

    async def test_manual_disconnect_stops_pairing_without_reconnecting(self):
        controller = SimpleNamespace(
            serial="test-controller",
            is_connected=Mock(return_value=True),
            set_player_led=AsyncMock(),
        )
        on_state_changed = AsyncMock()
        game = JustDanceGameV2(controller, on_state_changed=on_state_changed)
        game.is_connected = True
        close = AsyncMock()
        game.ws = SimpleNamespace(state=State.OPEN, close=close)
        receive_started = asyncio.Event()

        async def receive():
            receive_started.set()
            await asyncio.Event().wait()

        with (
            patch.object(game, "send_hello", new=AsyncMock()),
            patch.object(game, "_message_receive_loop", new=receive),
            patch.object(game, "pair", new=game.run_connection_tasks),
            patch.object(game, "attempt_reconnect", new=AsyncMock()) as reconnect,
        ):
            task = await game.start_pair()
            try:
                await asyncio.wait_for(receive_started.wait(), 1)
                await asyncio.wait_for(game.disconnect(should_reconnect=False), 1)
                reconnect.assert_not_awaited()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(task.cancelled())
        self.assertFalse(game.should_reconnect)
        self.assertIsNone(game.reconnection_task)
        close.assert_awaited_once()
        controller.set_player_led.assert_awaited_once_with(0)
        on_state_changed.assert_awaited_once_with(
            controller.serial,
            {"state": PairingState.DISCONNECTED.value, "connection_requested": False},
        )

    async def test_repeated_cancellation_does_not_interrupt_disconnect_cleanup(self):
        controller = SimpleNamespace(
            serial="test-controller",
            is_connected=Mock(return_value=True),
            set_player_led=AsyncMock(),
        )
        on_state_changed = AsyncMock()
        game = JustDanceGameV2(controller, on_state_changed=on_state_changed)
        game.is_connected = True
        close_started = asyncio.Event()
        close_release = asyncio.Event()
        close_finished = asyncio.Event()

        async def close():
            close_started.set()
            await close_release.wait()
            close_finished.set()

        game.ws = SimpleNamespace(state=State.OPEN, close=close)
        task = asyncio.create_task(game.disconnect(should_reconnect=False))
        try:
            await asyncio.wait_for(close_started.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            close_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 1)
        finally:
            close_release.set()
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(close_finished.is_set())
        controller.set_player_led.assert_awaited_once_with(0)
        on_state_changed.assert_awaited_once_with(
            controller.serial,
            {"state": PairingState.DISCONNECTED.value, "connection_requested": False},
        )

    async def test_cancelled_hid_setup_stops_worker_and_closes_device(self):
        joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
        joycon._first_connection_timeout = 30
        opened = threading.Event()
        device = Mock()

        def open_device(*args):
            opened.set()
            return device

        with patch.object(joycon, "_open", side_effect=open_device):
            task = asyncio.create_task(joycon.connect())
            try:
                self.assertTrue(await asyncio.to_thread(opened.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 1)
                device.close.assert_called_once()
                self.assertFalse(joycon.is_connected.is_set())
                self.assertIsNone(joycon._joycon_device)
                self.assertFalse(joycon._connect_lock.locked())
            finally:
                if joycon._active_connection_cancel is not None:
                    joycon._active_connection_cancel.set()
                await asyncio.gather(task, return_exceptions=True)
                joycon.close()


class JoyConReportTests(unittest.TestCase):
    def test_valid_reply_does_not_trigger_resend(self):
        joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
        response = b"\x80\x10current"
        with (
            patch.object(joycon, "_write_output_report") as write,
            patch.object(
                joycon,
                "_read_input_report",
                return_value=b"\x21" + bytes(12) + response,
            ),
            patch(
                "joydance.controllers.joycon.pycon.joycon.time.monotonic",
                side_effect=[0, 0, 0.5],
            ),
        ):
            self.assertEqual(
                joycon._send_subcmd_get_response(b"\x10", b"request", timeout=1),
                (0x80, response),
            )
        write.assert_called_once_with(b"\x01", b"\x10", b"request")

    def test_subcommand_retries_all_ignored_reports(self):
        response = b"\x80\x10current"
        valid_report = b"\x21" + bytes(12) + response
        for ignored_report in (
            b"",
            b"\x30",
            b"\x21" + bytes(12),
            b"\x21" + bytes(12) + b"\x80\x11",
            b"\x21" + bytes(12) + b"\x80\x10stale",
        ):
            with self.subTest(report=ignored_report):
                joycon = JoyCon(
                    JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller"
                )
                joycon._subcommand_resend_interval = 0.25
                with (
                    patch.object(joycon, "_write_output_report") as write,
                    patch.object(
                        joycon,
                        "_read_input_report",
                        side_effect=[ignored_report, valid_report],
                    ),
                    patch(
                        "joydance.controllers.joycon.pycon.joycon.time.monotonic",
                        side_effect=[0, 0, 0.25, 0.26],
                    ),
                ):
                    result = joycon._send_subcmd_get_response(
                        b"\x10",
                        b"request",
                        timeout=1,
                        response_validator=lambda reply: reply.endswith(b"current"),
                    )
                self.assertEqual(result, (0x80, response))
                self.assertEqual(write.call_count, 2)
                write.assert_called_with(b"\x01", b"\x10", b"request")


if __name__ == "__main__":
    unittest.main()
