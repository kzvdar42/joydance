import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from websockets.protocol import State

from joydance.controllers.joycon.joycon_wrapper import JoyConWrapper
from joydance.controllers.joycon.rumble_patterns import RUMBLE_PATTERNS
from joydance.controllers.joycon.pycon.constants import (
    JOYCON_VENDOR_ID,
    JOYCON_R_PRODUCT_ID,
)
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2


PLAY_SOUND = json.dumps({"__class": "JD_PlaySound_ConsoleCommandData", "soundIndex": 40})


class RumbleTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_effects_use_tuned_bands_and_absolute_deadlines_for_all_phone_variants(self):
        events = {index: index for index in RUMBLE_PATTERNS}
        events.update({index: 51 for index in range(52, 57)})
        events.update({index: 61 for index in range(62, 67)})
        for event, canonical in events.items():
            controller = JoyConWrapper(JOYCON_VENDOR_ID, JOYCON_R_PRODUCT_ID, 'test')
            self.addCleanup(controller.close)
            now, writes = 0, []

            def write(report):
                nonlocal now
                writes.append((now, report))
                now += .001  # HID writes must not add drift to every step.
                return len(report)

            async def sleep(duration):
                nonlocal now
                now += duration

            device = Mock(write=Mock(side_effect=write))
            controller.joycon._joycon_device = device
            with (
                self.subTest(event=event),
                patch('joydance.controllers.joycon.joycon_wrapper.asyncio.get_running_loop',
                      return_value=SimpleNamespace(time=lambda: now)),
                patch('joydance.controllers.joycon.joycon_wrapper.asyncio.sleep', side_effect=sleep),
            ):
                await controller.handle_rumble_on_sound_index(event)
            steps = RUMBLE_PATTERNS[canonical]
            self.assertEqual(len(writes), len(steps) + 1)
            deadline = 0
            for (actual, report), (lf, la, hf, ha, duration) in zip(writes, steps):
                low = controller.joycon.encode_rumble_data(lf, la)
                high = controller.joycon.encode_rumble_data(hf, ha)
                self.assertEqual(report[2:], (high[:2] + low[2:4]) * 2)
                self.assertEqual(report[0], 0x10)
                self.assertAlmostEqual(actual, deadline)
                deadline += duration
            self.assertAlmostEqual(writes[-1][0], deadline)
            self.assertEqual(writes[-1][1][2:], controller.joycon._RUMBLE_DATA)

    async def test_unknown_events_and_disabled_rumble_do_not_write(self):
        controller = JoyConWrapper(JOYCON_VENDOR_ID, JOYCON_R_PRODUCT_ID, 'test')
        self.addCleanup(controller.close)
        controller.joycon._joycon_device = device = Mock(write=Mock(side_effect=len))
        for event in (6, 9, 25, 41, 57, 67, -1, None, '1', [], {}):
            await controller.handle_rumble_on_sound_index(event)
        device.write.assert_not_called()
        controller.set_rumble(False)
        device.write.reset_mock()
        await controller.handle_rumble_on_sound_index(40)
        device.write.assert_not_called()

    async def test_disabling_rumble_during_effect_prevents_further_active_reports(self):
        controller = JoyConWrapper(JOYCON_VENDOR_ID, JOYCON_R_PRODUCT_ID, 'test')
        self.addCleanup(controller.close)
        controller.joycon._joycon_device = device = Mock(write=Mock(side_effect=len))

        async def disable(_):
            controller.set_rumble(False)

        with patch('joydance.controllers.joycon.joycon_wrapper.asyncio.sleep', side_effect=disable):
            await controller.handle_rumble_on_sound_index(40)
        reports = [call.args[0][2:] for call in device.write.call_args_list]
        self.assertNotEqual(reports[0], controller.joycon._RUMBLE_DATA)
        self.assertTrue(all(report == controller.joycon._RUMBLE_DATA for report in reports[1:]))

    def make_game(self, game_class):
        controller = JoyConWrapper(JOYCON_VENDOR_ID, JOYCON_R_PRODUCT_ID, "test-controller")
        controller.joycon.rumble_two_band = Mock()
        controller.stop_rumble = Mock()
        controller.is_connected = Mock(return_value=True)
        controller.set_accel_collection = Mock()
        controller.set_player_led = AsyncMock()
        game = game_class(controller, on_state_changed=AsyncMock(), on_game_message=AsyncMock())
        game.ws = SimpleNamespace(state=State.OPEN, close=AsyncMock(), send=AsyncMock())
        game.is_connected = True
        return game

    async def test_receive_loop_handles_scoring_stop_while_rumbling(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                game.should_start_accelerometer = True
                rumble_started = asyncio.Event()
                game.controller.joycon.rumble_two_band.side_effect = lambda *args: rumble_started.set()
                processed = asyncio.Event()

                async def messages():
                    yield PLAY_SOUND
                    await rumble_started.wait()
                    yield json.dumps({"__class": "JD_DisableAccelValuesSending_ConsoleCommandData"})
                    processed.set()
                    await asyncio.Event().wait()

                class Socket:
                    def __aiter__(self):
                        return messages()

                game.ws = Socket()
                receive = asyncio.create_task(game._message_receive_loop())
                try:
                    await asyncio.wait_for(processed.wait(), 1)
                    self.assertFalse(game.should_start_accelerometer)
                    self.assertFalse(game._rumble_task.done())
                    game.controller.stop_rumble.assert_not_called()
                finally:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    await game._stop_rumble()

    async def test_new_feedback_replaces_pattern_after_stopping_old_rumble(self):
        game = self.make_game(JustDanceGameV2)
        started = asyncio.Event()
        game.controller.joycon.rumble_two_band.side_effect = lambda *args: started.set()
        try:
            await game.on_message(PLAY_SOUND)
            await asyncio.wait_for(started.wait(), 1)
            first = game._rumble_task
            started.clear()
            await game.on_message(PLAY_SOUND)
            self.assertTrue(first.cancelled())
            game.controller.stop_rumble.assert_called_once_with()
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(game.controller.joycon.rumble_two_band.call_count, 2)
        finally:
            await game._stop_rumble()

    async def test_disconnect_cancels_rumble_and_stops_motor(self):
        game = self.make_game(JustDanceGameV2)
        started = asyncio.Event()
        game.controller.joycon.rumble_two_band.side_effect = lambda *args: started.set()
        try:
            await game.on_message(PLAY_SOUND)
            await asyncio.wait_for(started.wait(), 1)
            rumble = game._rumble_task
            await asyncio.wait_for(game.disconnect(should_reconnect=False), 1)
            self.assertTrue(rumble.cancelled())
            self.assertIsNone(game._rumble_task)
            game.controller.stop_rumble.assert_called_once_with()
        finally:
            await game._stop_rumble()

    async def test_connection_task_cancellation_stops_rumble(self):
        game = self.make_game(JustDanceGameV2)
        started = asyncio.Event()
        game.controller.joycon.rumble_two_band.side_effect = lambda *args: started.set()

        async def receive():
            await game.on_message(PLAY_SOUND)
            await asyncio.Event().wait()

        game._message_receive_loop = receive
        connection = asyncio.create_task(game.run_connection_tasks())
        try:
            await asyncio.wait_for(started.wait(), 1)
            rumble = game._rumble_task
            connection.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(connection, 1)
            self.assertTrue(rumble.cancelled())
            self.assertIsNone(game._rumble_task)
            game.controller.stop_rumble.assert_called_once_with()
        finally:
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
            await game._stop_rumble()

    async def test_rumble_failure_is_logged_without_stopping_game_messages(self):
        game = self.make_game(JustDanceGameV2)
        game.controller.joycon.rumble_two_band.side_effect = OSError("Controller unavailable")
        with self.assertLogs("joydance", level="ERROR") as logs:
            await game.on_message(PLAY_SOUND)
            await asyncio.wait_for(game._rumble_task, 1)
        self.assertIn("Error playing rumble", logs.output[0])
        game.controller.stop_rumble.assert_called_once_with()
        await game.on_message(json.dumps({"__class": "InputSetup_ConsoleCommandData", "isEnabled": 1}))
        self.assertTrue(game.is_input_allowed)
        await game._stop_rumble()


if __name__ == "__main__":
    unittest.main()
