import asyncio
import json
import struct
import threading
import unittest
from unittest.mock import AsyncMock

from joydance.controllers.joycon.joycon_wrapper import JoyConWrapper
from joydance.controllers.joycon.pycon.constants import (
    JOYCON_L_PRODUCT_ID,
    JOYCON_R_PRODUCT_ID,
    JOYCON_PRO_PRODUCT_ID,
    JOYCON_VENDOR_ID,
)
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2


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
