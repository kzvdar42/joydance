import asyncio
import json
import struct
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from joydance.constants import Command
from joydance.controllers.joycon.joycon_wrapper import JoyConWrapper
from joydance.controllers.joycon.pycon.constants import (
    JOYCON_L_PRODUCT_ID,
    JOYCON_R_PRODUCT_ID,
    JOYCON_PRO_PRODUCT_ID,
    JOYCON_VENDOR_ID,
)
from joydance.games.v1.game import JustDanceGameV1
from joydance.games.v2.game import JustDanceGameV2


def motion_report(start):
    report = bytearray(49)
    report[0] = 0x30
    for index in range(3):
        value = start + index
        struct.pack_into("<hhh", report, 13 + index * 12, value, -value, value)
    return bytes(report)


class AccelerometerPipelineTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, product_id=JOYCON_L_PRODUCT_ID):
        controller = JoyConWrapper(JOYCON_VENDOR_ID, product_id, "motion-test")
        joycon = controller.joycon
        joycon.set_accel_calibration((0, 0, 0), (16384, 16384, 16384))
        joycon._should_run.set()
        joycon._imu_enabled = joycon._imu_ready = True
        self.addCleanup(controller.close)
        return controller

    def test_reports_are_drained_once_in_order_for_every_controller_type(self):
        for product_id in (JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID):
            with self.subTest(product_id=product_id):
                controller = self.controller(product_id)
                controller.set_accel_collection(True)
                reports = (motion_report(1), motion_report(4), motion_report(4))
                for report in reports:
                    controller.joycon._publish_input_report(report)
                expected = [
                    sample for report in reports
                    for sample in controller.joycon.get_accels(report)
                ]
                self.assertEqual(controller.get_accel_events(), expected)
                self.assertEqual(controller.get_accel_events(), [])

    def test_reader_toggles_imu_and_queues_only_ready_motion(self):
        controller = self.controller()
        joycon = controller.joycon
        joycon._imu_enabled = joycon._imu_ready = False
        joycon._input_reader_gate.set()
        idle = b"\x30" + bytes(4) + b"\x04" + bytes(43)
        partial = motion_report(1)[:37] + bytes(12)
        fresh = motion_report(4)
        incoming = iter((idle, idle, partial, fresh, idle))

        def read(**kwargs):
            report = next(incoming, None)
            if report is None:
                joycon._should_run.clear()
                return b""
            return report

        commands = []
        samples = []

        def on_report(device):
            commands.append(controller.get_latest_command())
            if len(commands) == 1:
                controller.set_accel_collection(True)
            elif device._input_report == fresh:
                samples.extend(controller.get_accel_events())
                controller.set_accel_collection(False)

        device = Mock(read=Mock(side_effect=read), write=Mock(side_effect=len))
        joycon._joycon_device = device
        joycon.register_update_hook(on_report)
        with patch("joydance.controllers.joycon.pycon.joycon.time.sleep"):
            joycon._run_input_reader()

        self.assertEqual(commands[0], Command.ACCEPT)
        self.assertEqual(len(commands), 5)
        self.assertEqual(samples, joycon.get_accels(fresh))
        self.assertEqual(
            [call.args[0][10:12] for call in device.write.call_args_list],
            [b"\x40\x01", b"\x41\x03", b"\x40\x00"],
        )

    def test_motion_settings_state_tracks_requested_and_applied_settings(self):
        joycon = self.controller().joycon
        settings = {"idle_imu_shutdown": True, "gyro_shutdown": False}
        self.assertEqual(joycon.get_motion_settings(), settings)
        snapshot = joycon.get_motion_settings()
        snapshot["idle_imu_shutdown"] = False
        self.assertEqual(joycon.get_motion_settings(), settings)

        joycon._imu_enabled = False
        self.assertTrue(joycon.motion_settings_applied)
        joycon.set_accel_collection(True)
        self.assertFalse(joycon.motion_settings_applied)
        joycon._imu_enabled = True
        self.assertTrue(joycon.motion_settings_applied)
        joycon.set_accel_collection(False)
        self.assertFalse(joycon.motion_settings_applied)
        joycon.configure_motion(idle_imu_shutdown=False, gyro_shutdown=True)
        self.assertEqual(joycon.get_motion_settings(), {"idle_imu_shutdown": False, "gyro_shutdown": True})
        self.assertFalse(joycon.motion_settings_applied)
        joycon._gyro_shutdown_checked = True
        self.assertTrue(joycon.motion_settings_applied)
        self.assertFalse(joycon.gyro_shutdown_available)
        joycon._gyro_shutdown_supported = True
        self.assertTrue(joycon.gyro_shutdown_available)

    def test_idle_shutdown_can_be_disabled_without_buffering_unused_motion(self):
        controller = self.controller()
        controller.configure_motion(idle_imu_shutdown=False, gyro_shutdown=False)
        joycon = controller.joycon
        joycon._imu_enabled = False
        joycon._input_reader_gate.set()
        device = Mock(read=Mock(return_value=motion_report(1)), write=Mock(side_effect=len))
        joycon._joycon_device = device
        joycon.register_update_hook(lambda device: device._should_run.clear())
        with patch("joydance.controllers.joycon.pycon.joycon.time.sleep"):
            joycon._run_input_reader()
        self.assertTrue(joycon._imu_enabled)
        self.assertEqual(controller.get_accel_events(), [])
        self.assertIsNone(joycon._accel_reports)

    async def test_live_gyro_changes_preserve_motion_and_keep_imu_running(self):
        controller = self.controller()
        joycon = controller.joycon
        controller.set_accel_collection(True)
        controller.configure_motion(idle_imu_shutdown=False, gyro_shutdown=True)
        joycon._input_reader_gate.set()
        reports = (motion_report(1), motion_report(4), motion_report(7))
        identity = b"\x21" + bytes(12) + b"\xc0\x43\x0f\x01\x69" + bytes(31)
        device = Mock(read=Mock(side_effect=[reports[0], identity, *reports[1:]]), write=Mock(side_effect=len))
        joycon._joycon_device = device

        def on_report(device):
            if device._input_report == reports[1]:
                controller.configure_motion(idle_imu_shutdown=True, gyro_shutdown=False)
            elif device._input_report == reports[2]:
                device._should_run.clear()

        joycon.register_update_hook(on_report)
        joycon._run_input_reader()
        expected_samples = [sample for report in reports for sample in joycon.get_accels(report)]
        self.assertEqual(controller.get_accel_events(), expected_samples)
        self.assertEqual([request.args[0][10:14] for request in device.write.call_args_list], [
            b"\x43\x0f\x01\x00", b"\x42\x11\x01\x0c", b"\x42\x11\x01\x5c",
        ])
        self.assertTrue(joycon._imu_ready)
        self.assertTrue(controller.is_connected())
        await controller.apply_motion_settings(idle_imu_shutdown=True, gyro_shutdown=False)

    def test_live_imu_query_timeout_preserves_scoring_samples(self):
        controller = self.controller()
        joycon = controller.joycon
        controller.set_accel_collection(True)
        controller.configure_motion(idle_imu_shutdown=True, gyro_shutdown=True)
        joycon._input_reader_gate.set()
        joycon._subcommand_resend_interval = 0.025
        reports = []
        clock = 0

        def read(**kwargs):
            nonlocal clock
            clock += 0.01
            reports.append(motion_report(len(reports) * 3 + 1))
            return reports[-1]

        def on_report(device):
            if len(reports) == 4:
                device._should_run.clear()

        device = Mock(read=Mock(side_effect=read), write=Mock(side_effect=len))
        joycon._joycon_device = device
        joycon.register_update_hook(on_report)
        with patch("joydance.controllers.joycon.pycon.joycon.time.monotonic", side_effect=lambda: clock):
            joycon._run_input_reader()
        self.assertEqual(len(reports), 4)
        expected_samples = [sample for report in reports for sample in joycon.get_accels(report)]
        self.assertEqual(controller.get_accel_events(), expected_samples)
        self.assertFalse(joycon._gyro_shutdown_supported)
        self.assertTrue(controller.is_connected())
        self.assertEqual([request.args[0][10] for request in device.write.call_args_list], [0x43])

    def test_reader_restores_full_mode_and_recovers_buttons_and_motion(self):
        for product_id in (JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID):
            with self.subTest(product_id=product_id):
                controller = self.controller(product_id)
                joycon = controller.joycon
                joycon._input_reader_gate.set()
                joycon._publish_input_report(motion_report(100))
                controller.set_accel_collection(True)
                fresh = bytearray(motion_report(1))
                fresh[5 if controller.is_left() else 3] |= 4 if controller.is_left() else 8
                fresh = bytes(fresh)
                restored = False
                reads = 0

                def write(report):
                    nonlocal restored
                    if report[0] == 1 and report[10:12] == b"\x03\x30":
                        restored = True
                    return len(report)

                def read(**kwargs):
                    nonlocal reads
                    reads += 1
                    if restored:
                        return fresh
                    if reads >= 4:
                        joycon._should_run.clear()
                    return b"\x3f" + bytes(48)

                device = Mock(read=Mock(side_effect=read), write=Mock(side_effect=write))
                joycon._joycon_device = device
                joycon.register_update_hook(lambda device: device._should_run.clear())
                with patch("joydance.controllers.joycon.pycon.joycon.logger.warning") as warning:
                    joycon._run_input_reader()

                self.assertTrue(restored)
                warning.assert_called_once()
                self.assertEqual(reads, 2)
                self.assertEqual(
                    [call.args[0][10:15] for call in device.write.call_args_list],
                    [b"\x40\x01\x00\x00\x00", b"\x41\x03\x00\x01\x01",
                     b"\x48\x01\x00\x00\x00", b"\x03\x30\x00\x00\x00",
                     b"\x30\x08\x00\x00\x00"],
                )
                self.assertEqual(controller.get_latest_command(), Command.ACCEPT)
                self.assertEqual(controller.get_accel_events(), joycon.get_accels(fresh))

    def test_mode_restore_retries_are_throttled_and_reset_after_full_reports(self):
        controller = self.controller()
        joycon = controller.joycon
        joycon._imu_enabled = joycon._imu_ready = False
        joycon._input_reader_gate.set()
        joycon._joycon_device = Mock()
        clock = 0
        reports = iter([(0, 0x3F), (0.1, 0x3F), (0.3, 0x3F), (0.31, 0x30),
                        (0.32, 0x3F), (0.33, 0x30)])

        def read():
            nonlocal clock
            clock, report_id = next(reports)
            if clock == 0.33:
                joycon.register_update_hook(lambda device: device._should_run.clear())
            return bytes([report_id]) + bytes(48)

        with (
            patch.object(joycon, "_read_input_report", side_effect=read),
            patch.object(joycon, "_setup_sensors") as restore,
            patch("joydance.controllers.joycon.pycon.joycon.time.monotonic", side_effect=lambda: clock),
            self.assertLogs("joydance.controllers.joycon.pycon.joycon", level="WARNING") as logs,
        ):
            joycon._run_input_reader()
        self.assertEqual(restore.call_count, 3)
        self.assertEqual(len(logs.output), 2)

    def test_status_reads_do_not_consume_pending_motion(self):
        controller = self.controller()
        controller.set_accel_collection(True)
        report = motion_report(1)
        controller.joycon._publish_input_report(report)
        snapshot = controller.joycon.get_status()["accel"]
        self.assertEqual(controller.joycon.get_status()["accel"], snapshot)
        self.assertEqual(controller.get_accel_events(), snapshot)
        self.assertEqual(controller.joycon.get_status()["accel"], snapshot)

    def test_inactive_and_closed_controllers_do_not_retain_motion(self):
        controller = self.controller()
        joycon = controller.joycon
        report = motion_report(1)
        joycon._publish_input_report(report)
        controller.set_accel_collection(True)
        self.assertEqual(controller.get_accel_events(), [])
        joycon._publish_input_report(report)
        controller.set_accel_collection(False)
        joycon._publish_input_report(report)
        controller.set_accel_collection(True)
        self.assertEqual(controller.get_accel_events(), [])
        for close in (joycon._close, controller.close):
            controller.set_accel_collection(True)
            joycon._publish_input_report(report)
            close()
            self.assertEqual(controller.get_accel_events(), [])

    async def test_reader_can_publish_while_collected_reports_are_decoded(self):
        controller = self.controller()
        controller.set_accel_collection(True)
        joycon = controller.joycon
        first, second = motion_report(1), motion_report(4)
        joycon._publish_input_report(first)
        decoding = threading.Event()
        published = threading.Event()
        decode = joycon.get_accels

        def slow_decode(report):
            decoding.set()
            if not published.wait(1):
                raise TimeoutError("Reader blocked while draining motion")
            return decode(report)

        joycon.get_accels = slow_decode
        drain = asyncio.create_task(asyncio.to_thread(controller.get_accel_events))
        try:
            self.assertTrue(await asyncio.to_thread(decoding.wait, 1))
            await asyncio.wait_for(asyncio.to_thread(joycon._publish_input_report, second), 1)
            published.set()
            self.assertEqual(await asyncio.wait_for(drain, 1), decode(first))
        finally:
            published.set()
            await asyncio.gather(drain, return_exceptions=True)
            joycon.get_accels = decode
        self.assertEqual(controller.get_accel_events(), decode(second))

    async def test_game_collects_all_received_samples_without_repeating_them(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                controller = self.controller()
                controller.set_player_led = AsyncMock()
                game = game_class(controller, on_state_changed=AsyncMock(), on_game_message=AsyncMock())
                game.is_connected = True
                game.send_message = AsyncMock()
                self.addAsyncCleanup(game.disconnect, should_reconnect=False)
                enable = json.dumps({"__class": "JD_EnableAccelValuesSending_ConsoleCommandData"})
                disable = json.dumps({"__class": "JD_DisableAccelValuesSending_ConsoleCommandData"})
                controller.joycon._publish_input_report(motion_report(100))
                await game.on_message(enable)
                for start in (1, 4, 7, 10):
                    controller.joycon._publish_input_report(motion_report(start))
                await game.collect_accelerometer_data()
                await game.collect_accelerometer_data()
                await game.send_accelerometer_data(3)
                messages = [call.args[1] for call in game.send_message.await_args_list]
                samples = [sample for message in messages for sample in message["accelData"]]
                self.assertEqual([sample[0] for sample in samples], [value / 4096 for value in range(1, 13)])
                self.assertEqual([message["timeStamp"] for message in messages], [0, 10])
                await game.on_message(disable)
                controller.joycon._publish_input_report(motion_report(100))
                await game.on_message(enable)
                self.assertEqual(controller.get_accel_events(), [])
                controller.joycon._publish_input_report(motion_report(1))
                await game.disconnect(should_reconnect=False)
                self.assertEqual(controller.get_accel_events(), [])

    async def test_scoring_boundaries_discard_old_samples(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                controller = self.controller()
                game = game_class(controller, on_game_message=AsyncMock())
                game.is_connected = True
                game.send_message = AsyncMock()
                enable = json.dumps({"__class": "JD_EnableAccelValuesSending_ConsoleCommandData"})
                disable = json.dumps({"__class": "JD_DisableAccelValuesSending_ConsoleCommandData"})
                await game.on_message(enable)
                controller.joycon._publish_input_report(motion_report(1))
                await game.collect_accelerometer_data()
                controller.joycon._publish_input_report(motion_report(4))
                await game.on_message(disable)
                self.assertEqual(game.accel_data, [])
                self.assertEqual(controller.get_accel_events(), [])
                controller.joycon._publish_input_report(motion_report(7))
                await game.on_message(enable)
                controller.joycon._publish_input_report(motion_report(10))
                await game.collect_accelerometer_data()
                # A new enable also resets samples if no disable preceded it.
                await game.on_message(enable)
                self.assertEqual(game.accel_data, [])
                controller.joycon._publish_input_report(motion_report(13))
                await game.collect_accelerometer_data()
                await game.send_accelerometer_data(3)
                game.send_message.assert_awaited_once_with(
                    "JD_PhoneScoringData",
                    {"accelData": controller.joycon.get_accels(), "timeStamp": 0},
                )

    async def test_pending_send_stops_old_batches_without_changing_restart_timestamp(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for restart in (False, True):
                with self.subTest(protocol=game_class.__name__, restart=restart):
                    controller = self.controller()
                    game = game_class(controller, on_game_message=AsyncMock())
                    enable = json.dumps({"__class": "JD_EnableAccelValuesSending_ConsoleCommandData"})
                    disable = json.dumps({"__class": "JD_DisableAccelValuesSending_ConsoleCommandData"})
                    await game.on_message(enable)
                    game.number_of_accels_sent = 7
                    game.accel_data = [(i, 0, 0) for i in range(21)]
                    started, release = asyncio.Event(), asyncio.Event()

                    async def send(command, data):
                        started.set()
                        await release.wait()

                    game.send_message = AsyncMock(side_effect=send)
                    sender = asyncio.create_task(game.send_accelerometer_data(3))
                    try:
                        await asyncio.wait_for(started.wait(), 1)
                        await game.on_message(disable)
                        if restart:
                            await game.on_message(enable)
                    finally:
                        release.set()
                        await asyncio.wait_for(sender, 1)
                    self.assertEqual(game.send_message.await_count, 1)
                    self.assertEqual(game.number_of_accels_sent, 0 if restart else 7)
