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
    JOYCON_R_PRODUCT_ID,
    JOYCON_PRO_PRODUCT_ID,
    JOYCON_VENDOR_ID,
)
from joydance.controllers.joycon.pycon.joycon import JoyCon
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2
from joydance.utils import ControllerConnectionError


class ConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_accelerometer_batches_preserve_samples_and_timestamps(self):
        game = JustDanceGameV2(SimpleNamespace(serial="test-controller"))
        game.should_start_accelerometer = True
        game.number_of_accels_sent = 7
        samples = [(i, i + 1, i + 2) for i in range(21)]
        game.accel_data = samples.copy()
        game.send_message = AsyncMock()

        await game.send_accelerometer_data(3)

        self.assertEqual(game.send_message.await_count, 3)
        for sent, start in zip(game.send_message.await_args_list, (0, 10, 20)):
            self.assertEqual(sent.args, (
                "JD_PhoneScoringData",
                {"accelData": samples[start:start + 10], "timeStamp": 7 + start},
            ))
        self.assertEqual(game.number_of_accels_sent, 28)
        self.assertEqual(game.accel_data, [])

    async def test_reconnect_from_song_accepts_pause_menu_input(self):
        controller = SimpleNamespace(
            serial="test-controller",
            is_connected=Mock(return_value=True),
            set_accel_collection=Mock(),
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
                        set_accel_collection=Mock(),
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
                    set_accel_collection=Mock(),
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
            set_accel_collection=Mock(),
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
            set_accel_collection=Mock(),
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

    async def test_failed_setup_write_closes_device_and_retries(self):
        for bytes_written in (-1, 0, 48):
            with self.subTest(bytes_written=bytes_written):
                joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
                joycon._first_connection_timeout = 0
                joycon._max_setup_attempts = 2
                joycon.reconnect_timeout = 0
                full_report = b"\x30" + bytes(48)

                def write(report):
                    return bytes_written if report[10] == 0x41 else len(report)

                failed_device = Mock(read=Mock(return_value=full_report), write=Mock(side_effect=write))
                device = Mock(read=Mock(return_value=full_report), write=Mock(side_effect=len))
                joycon.register_update_hook(lambda controller: controller._should_run.clear())
                with (
                    patch.object(joycon, "_open", side_effect=[failed_device, device]) as open_device,
                    patch.object(joycon, "_read_joycon_data"),
                    patch("joydance.controllers.joycon.pycon.joycon.time.sleep"),
                    patch("joydance.controllers.joycon.pycon.joycon.logger"),
                ):
                    try:
                        self.assertTrue(await asyncio.wait_for(joycon.connect(), 1))
                        self.assertEqual(open_device.call_count, 2)
                        failed_device.close.assert_called_once()
                        self.assertEqual(
                            [call.args[0][10] for call in failed_device.write.call_args_list],
                            [0x08, 0x03, 0x40, 0x41],
                        )
                        self.assertTrue(joycon.was_state_initialized)
                    finally:
                        joycon.close()

    async def test_stopping_connection_while_waiting_for_reader_does_not_open_device(self):
        for action in ("cancel", "close"):
            with self.subTest(action=action):
                joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
                device = Mock()
                joycon._joycon_device = device
                waiting = threading.Event()
                wait_for_reader = joycon._wait_for_input_reader

                def wait():
                    waiting.set()
                    wait_for_reader()

                joycon._input_reader_lock.acquire()
                lock_held = True
                with (
                    patch.object(joycon, "_wait_for_input_reader", side_effect=wait),
                    patch.object(joycon, "_open") as open_device,
                ):
                    connection = asyncio.create_task(joycon.connect())
                    try:
                        self.assertTrue(await asyncio.to_thread(waiting.wait, 1))
                        if action == "cancel":
                            connection.cancel()
                        else:
                            joycon.close()
                        joycon._input_reader_lock.release()
                        lock_held = False
                        expected = asyncio.CancelledError if action == "cancel" else ControllerConnectionError
                        with self.assertRaises(expected):
                            await asyncio.wait_for(connection, 1)
                        open_device.assert_not_called()
                        device.close.assert_called_once()
                        self.assertFalse(joycon._connect_lock.locked())
                        self.assertFalse(joycon.is_connected.is_set())
                        self.assertIsNone(joycon._joycon_device)
                        self.assertTrue(joycon._input_reader_gate.is_set())
                    finally:
                        if lock_held:
                            joycon._input_reader_lock.release()
                        joycon.close()
                        connection.cancel()
                        await asyncio.gather(connection, return_exceptions=True)

    async def test_reconnect_waits_for_reader_and_keeps_setup_replies(self):
        for pending_report in (b"", b"\x21" + bytes(48), b"\x3f" + bytes(48),
                               b"\x30" + bytes(48), OSError("old HID read failed")):
            with self.subTest(pending_report=pending_report):
                joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
                joycon._first_connection_timeout = 0
                joycon._max_setup_attempts = 1
                joycon.reconnect_timeout = 0
                joycon._should_run.set()
                joycon._input_reader_gate.set()
                joycon.is_connected.set()
                read_started = threading.Event()
                read_release = threading.Event()
                gate_closed = threading.Event()

                def read_old(**kwargs):
                    read_started.set()
                    if not read_release.wait(2):
                        raise TimeoutError("Test did not release the old HID read")
                    if isinstance(pending_report, Exception):
                        raise pending_report
                    return pending_report

                old_device = Mock(read=Mock(side_effect=read_old))
                reply = b"\x21" + bytes(12) + b"\x80\x10setup"
                new_device = Mock(
                    read=Mock(side_effect=[reply, b"\x30" + bytes(48)]),
                    write=Mock(side_effect=len),
                )
                joycon._joycon_device = old_device
                clear_gate = joycon._input_reader_gate.clear

                def pause():
                    clear_gate()
                    gate_closed.set()

                def setup():
                    self.assertEqual(
                        joycon._send_subcmd_get_response(b"\x10", b"request", timeout=1),
                        (0x80, b"\x80\x10setup"),
                    )

                joycon.register_update_hook(lambda controller: controller._should_run.clear())
                with (
                    patch.object(joycon._input_reader_gate, "clear", side_effect=pause),
                    patch.object(joycon, "_open", return_value=new_device) as open_device,
                    patch.object(joycon, "_prepare_input_reports"),
                    patch.object(joycon, "_wait_for_any_input_report"),
                    patch.object(joycon, "_read_joycon_data", side_effect=setup),
                    patch.object(joycon, "_setup_sensors"),
                    patch.object(joycon, "_schedule_reconnect_from_reader"),
                    patch("joydance.controllers.joycon.pycon.joycon.logger"),
                ):
                    reader = threading.Thread(target=joycon._update_input_report, daemon=True)
                    joycon._update_input_report_thread = reader
                    reader.start()
                    connection = None
                    try:
                        self.assertTrue(await asyncio.to_thread(read_started.wait, 1))
                        connection = asyncio.create_task(joycon.connect())
                        self.assertTrue(await asyncio.to_thread(gate_closed.wait, 1))
                        await asyncio.sleep(0.02)
                        old_device.close.assert_not_called()
                        open_device.assert_not_called()
                        read_release.set()
                        self.assertTrue(await asyncio.wait_for(connection, 2))
                        old_device.close.assert_called_once()
                        self.assertEqual(old_device.read.call_count, 1)
                        self.assertEqual(new_device.read.call_count, 2)
                    finally:
                        read_release.set()
                        joycon.close()
                        if connection is not None:
                            connection.cancel()
                            await asyncio.gather(connection, return_exceptions=True)
                        await asyncio.to_thread(reader.join, 1)
                        self.assertFalse(reader.is_alive())


class JoyConReportTests(unittest.TestCase):
    def test_truncated_full_reports_do_not_publish_or_refresh_connection(self):
        joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
        self.addCleanup(joycon.close)
        joycon._should_run.set()
        joycon.set_accel_collection(True)
        hook = Mock()
        joycon.register_update_hook(hook)
        original = joycon._input_report
        with patch("joydance.controllers.joycon.pycon.joycon.time.monotonic", return_value=10) as clock:
            for length in range(49):
                report = b"\x30" + bytes(length - 1) if length else b""
                joycon._publish_input_report(report)
                self.assertEqual(joycon._input_report, original)
                self.assertFalse(joycon.is_connected.is_set())
                self.assertIsNone(joycon._input_report_deadline)
            hook.assert_not_called()
            full_report = b"\x30" + bytes(48)
            joycon._publish_input_report(full_report)
            clock.return_value = 11
            joycon._publish_input_report(full_report[:-1])
            self.assertEqual(joycon._input_report, full_report)
            self.assertTrue(joycon.is_connected.is_set())
            self.assertEqual(joycon._input_report_deadline, 12)
            self.assertEqual(joycon._accel_reports, [full_report])
            hook.assert_called_once_with(joycon)

    def test_spi_read_retries_matching_replies_with_incomplete_payloads(self):
        for size in (2, 6, 24, 29):
            with self.subTest(size=size):
                joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
                self.addCleanup(joycon.close)
                address = 0x6050
                argument = address.to_bytes(4, "little") + bytes([size])
                payload = bytes(range(size))
                response = b"\x90\x10" + argument + payload
                with (
                    patch.object(joycon, "_write_output_report") as write,
                    patch.object(joycon, "_read_input_report", side_effect=[
                        b"\x21" + bytes(12) + data
                        for data in (response[:7], response[:-1], response)
                    ]),
                    patch("joydance.controllers.joycon.pycon.joycon.time.monotonic",
                          side_effect=[0, 0, 0.25, 0.5, 0.51]),
                ):
                    self.assertEqual(joycon._spi_flash_read(address, size), payload)
                self.assertEqual(write.call_count, 3)
                write.assert_called_with(b"\x01", b"\x10", argument)

    def test_manual_stop_during_read_does_not_trigger_stream_timeout(self):
        joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
        self.addCleanup(joycon.close)
        joycon._should_run.set()
        joycon._input_reader_gate.set()
        clock = 0

        def read(**kwargs):
            nonlocal clock
            clock = 3
            joycon.close()
            return b""

        device = Mock(read=Mock(side_effect=read))
        joycon._joycon_device = device
        with (
            patch("joydance.controllers.joycon.pycon.joycon.time.monotonic", side_effect=lambda: clock),
            patch("joydance.controllers.joycon.pycon.joycon.logger") as logger,
            patch.object(joycon, "_schedule_reconnect_from_reader") as reconnect,
        ):
            joycon._publish_input_report(b"\x30" + bytes(48))
            joycon._update_input_report()
        device.close.assert_called_once()
        self.assertFalse(joycon.is_connected.is_set())
        logger.exception.assert_not_called()
        reconnect.assert_not_called()

    def test_stalled_stream_disconnects_and_schedules_reconnect(self):
        for report in (b"", b"\x21" + bytes(48), b"\x3f" + bytes(48),
                       b"\x30", b"\x31" + bytes(48)):
            with self.subTest(report=report):
                joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
                self.addCleanup(joycon.close)
                joycon.reconnect_timeout = 0
                joycon._should_run.set()
                joycon._input_reader_gate.set()
                joycon.set_accel_collection(True)
                clock = 0

                def read(**kwargs):
                    nonlocal clock
                    clock += 1
                    if clock >= 3:
                        joycon._should_run.clear()
                    return report

                device = Mock(read=Mock(side_effect=read))
                joycon._joycon_device = device
                with (
                    patch("joydance.controllers.joycon.pycon.joycon.time.monotonic", side_effect=lambda: clock),
                    patch("joydance.controllers.joycon.pycon.joycon.logger"),
                    patch.object(joycon, "_setup_sensors"),
                    patch.object(joycon, "_schedule_reconnect_from_reader", side_effect=joycon._should_run.clear) as reconnect,
                ):
                    joycon._publish_input_report(b"\x30" + bytes(48))
                    joycon._update_input_report()
                self.assertFalse(joycon.is_connected.is_set())
                device.close.assert_called_once()
                reconnect.assert_called_once()
                self.assertEqual(device.read.call_count, 2)
                self.assertEqual(joycon.get_accel_events(), [])

    def test_full_reports_reset_timeout_after_short_gaps(self):
        for report in (b"", b"\x21" + bytes(48), b"\x3f" + bytes(48)):
            with self.subTest(report=report):
                joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
                self.addCleanup(joycon.close)
                joycon._should_run.set()
                joycon._input_reader_gate.set()
                full_report = b"\x30" + bytes(48)
                reports = iter([(1.9, report), (1.95, full_report), (3.8, report), (3.9, full_report)])
                clock = 0

                def read(**kwargs):
                    nonlocal clock
                    clock, data = next(reports)
                    return data

                def stop(controller):
                    if clock == 3.9:
                        controller._should_run.clear()

                device = Mock(read=Mock(side_effect=read))
                joycon._joycon_device = device
                with (
                    patch("joydance.controllers.joycon.pycon.joycon.time.monotonic", side_effect=lambda: clock),
                    patch("joydance.controllers.joycon.pycon.joycon.logger"),
                    patch.object(joycon, "_setup_sensors"),
                    patch.object(joycon, "_schedule_reconnect_from_reader") as reconnect,
                ):
                    joycon._publish_input_report(full_report)
                    joycon.register_update_hook(stop)
                    joycon._update_input_report()
                self.assertTrue(joycon.is_connected.is_set())
                self.assertEqual(device.read.call_count, 4)
                device.close.assert_not_called()
                reconnect.assert_not_called()

    def test_setup_pause_resets_stream_timeout_until_first_new_full_report(self):
        joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
        self.addCleanup(joycon.close)
        joycon._should_run.set()
        full_report = b"\x30" + bytes(48)
        device = Mock(read=Mock(side_effect=[b"", full_report]))
        joycon._joycon_device = device
        clock = 0
        with (
            patch("joydance.controllers.joycon.pycon.joycon.time.monotonic", side_effect=lambda: clock),
            patch.object(joycon, "_schedule_reconnect_from_reader") as reconnect,
        ):
            joycon._publish_input_report(full_report)
            joycon._wait_for_input_reader()
            clock = 10
            joycon._input_reader_gate.set()
            joycon.register_update_hook(lambda controller: controller._should_run.clear())
            joycon._update_input_report()
        self.assertTrue(joycon.is_connected.is_set())
        self.assertEqual(device.read.call_count, 2)
        device.close.assert_not_called()
        reconnect.assert_not_called()

    def test_reader_rechecks_gate_after_waiting_for_lock(self):
        joycon = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "test-controller")
        device = Mock()
        joycon._joycon_device = device
        joycon._should_run.set()
        joycon._input_reader_gate.set()
        first_wait = threading.Event()
        paused = threading.Event()
        wait_for_gate = joycon._input_reader_gate.wait

        def wait(timeout=None):
            if first_wait.is_set():
                paused.set()
            result = wait_for_gate(timeout)
            first_wait.set()
            return result

        with patch.object(joycon._input_reader_gate, "wait", side_effect=wait):
            reader = threading.Thread(target=joycon._update_input_report, daemon=True)
            try:
                with joycon._input_reader_lock:
                    reader.start()
                    self.assertTrue(first_wait.wait(1))
                    joycon._input_reader_gate.clear()
                self.assertTrue(paused.wait(1))
                device.read.assert_not_called()
            finally:
                joycon.close()
                reader.join(1)
                self.assertFalse(reader.is_alive())

    def test_sensor_setup_explicitly_restores_settings_on_every_call(self):
        # Wire values from bluetooth_hid_subcommands_notes.md, commands 0x40/0x41/0x48/0x03.
        expected = [
            b"\x40\x01\x00\x00\x00",
            b"\x41\x03\x00\x01\x01",
            b"\x48\x01\x00\x00\x00",
            b"\x03\x30\x00\x00\x00",
        ]
        for product_id in (JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID):
            for rumble_enabled in (False, True):
                with self.subTest(product_id=product_id, rumble_enabled=rumble_enabled):
                    joycon = JoyCon(JOYCON_VENDOR_ID, product_id, "test-controller")
                    self.addCleanup(joycon.close)
                    joycon.rumble_enabled = rumble_enabled
                    device = Mock()
                    device.write.side_effect = len
                    joycon._joycon_device = device
                    with patch("joydance.controllers.joycon.pycon.joycon.time.sleep"):
                        joycon._setup_sensors()
                        joycon._setup_sensors()
                    reports = [call.args[0] for call in device.write.call_args_list]
                    self.assertEqual([report[10:15] for report in reports], expected * 2)
                    self.assertTrue(all(report[0] == 1 and len(report) == 49 for report in reports))
                    self.assertEqual(joycon.rumble_enabled, rumble_enabled)
                    if not rumble_enabled:
                        joycon.rumble(320, 0.5)
                        self.assertEqual(device.write.call_count, len(reports))

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
