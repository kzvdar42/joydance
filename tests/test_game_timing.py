from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import asyncio

from joydance.constants import FRAME_DURATION
from joydance.games.v1.game import JustDanceGameV1


class GameTimingTests(unittest.IsolatedAsyncioTestCase):
    def game(self):
        controller = SimpleNamespace(serial="timing", is_connected=Mock(return_value=True))
        return JustDanceGameV1(controller)

    async def test_split_sleep_uses_elapsed_time_without_reading_the_system_clock(self):
        elapsed = 0

        async def sleep(delay):
            nonlocal elapsed
            elapsed += delay + .001

        clock = SimpleNamespace(monotonic=lambda: elapsed, time=Mock(side_effect=AssertionError("System clock read")))
        with patch("joydance.games.justdance_abstract.time", clock), \
                patch("joydance.games.justdance_abstract.asyncio.sleep", side_effect=sleep) as wait:
            await self.game().sleep_approx(FRAME_DURATION)
        self.assertEqual(wait.await_args_list[0].args[0], FRAME_DURATION * .3)
        self.assertGreaterEqual(elapsed, FRAME_DURATION)
        self.assertLess(elapsed, FRAME_DURATION + .002)
        clock.time.assert_not_called()

    async def test_tick_preserves_sleep_compensation_and_scoring_batch_cadence(self):
        instance = self.game()
        instance.is_connected = instance.should_start_accelerometer = True
        elapsed = 0
        frames = []

        async def sleep(delay):
            nonlocal elapsed
            elapsed += delay

        async def collect():
            nonlocal elapsed
            elapsed += .001

        async def send(frame):
            nonlocal elapsed
            elapsed += .002
            frames.append(frame)
            if len(frames) == 4:
                raise asyncio.CancelledError

        instance.sleep_approx = AsyncMock(side_effect=sleep)
        instance.collect_accelerometer_data = AsyncMock(side_effect=collect)
        instance.send_accelerometer_data = AsyncMock(side_effect=send)
        clock = SimpleNamespace(monotonic=lambda: elapsed, time=Mock(side_effect=AssertionError("System clock read")))
        with patch("joydance.games.justdance_abstract.time", clock):
            with self.assertRaises(asyncio.CancelledError):
                await instance.tick()
        self.assertEqual(frames, [1, 2, 3, 1])
        for call, expected in zip(instance.sleep_approx.await_args_list, [FRAME_DURATION] + [FRAME_DURATION - .003] * 3):
            self.assertAlmostEqual(call.args[0], expected)
        clock.time.assert_not_called()
