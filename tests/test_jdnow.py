import asyncio
from collections import deque
from concurrent.futures.process import BrokenProcessPool
import json
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import zlib

from websockets.protocol import State

from joydance.config_handler import ConfigHandler, is_valid_pairing_code
from joydance.constants import Command, PairingState
from joydance.games.jdnow.api import JDNowAPI, parse_json, read_limited, service_url
from joydance.games.jdnow.assets import JDNowAssets
from joydance.games.jdnow.connection import JDNowConnection, decode_payload, encode_frame
from joydance.games.jdnow.dictionary import DICTIONARY
from joydance.games.jdnow.scoring import float32, ratio_score, read_classifier, resample, score_adaptive_move, score_move
from joydance.games.jdnow.signin import recover_profile
from joydance.games.jdnow.workers import JDNowScoringWorkers
from joydance.games.jdnow.game import JustDanceGameNow
from joydance.utils import GameConnectionError


REFERENCE = json.loads((Path(__file__).parent / "fixtures/jdnow_scoring_reference.json").read_text())


def classifier_bytes(endian=">"):
    data = bytearray(244)
    struct.pack_into(endian + "II", data, 0, 1, 7)
    struct.pack_into(endian + "fffff", data, 200, .841, .2, 5, .7, .1)
    struct.pack_into(endian + "QIiI", data, 220, 0x211C000000000000, 0, 50, 2)
    values = REFERENCE["means"] + REFERENCE["weights"] + REFERENCE["energy"]
    data.extend(struct.pack(endian + "f" * len(values), *values))
    return bytes(data)


def game():
    controller = SimpleNamespace(serial="test-jdn", available_shortcuts=set(), rumble_enabled=False,
        is_connected=Mock(return_value=True), set_accel_collection=Mock(), set_player_led=AsyncMock(),
        get_latest_command=Mock(return_value=None), get_joystick_command=Mock(return_value=None),
        get_accel_events=Mock(return_value=[]), animate_connecting_led=AsyncMock())
    instance = JustDanceGameNow(controller, pairing_code="22965", pairing_method="jdnow",
                               on_state_changed=AsyncMock(), on_game_message=AsyncMock())
    instance.ws = SimpleNamespace(state=State.OPEN, send=AsyncMock(), close=AsyncMock())
    instance.api = SimpleNamespace(profile={"publicID": "public-profile-123456", "pName": "Test dancer"})
    return instance


def guest_state(config, serial, in_use=()):
    return config.get_jdnow_guest_states([serial], in_use)[serial]


class JDNowConfigTests(unittest.TestCase):
    def test_imported_profile_is_shared_without_overwriting_the_current_guest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "config.cfg")
            config = ConfigHandler(path)
            config.save_jdnow_player_id("left", "private-guest-profile-1234", "Guest Dancer")
            linked_id = "private-linked-profile-1234"
            config.save_jdnow_linked_profile(linked_id, "Linked Dancer")
            config = ConfigHandler(path)
            state = guest_state(config, "right")
            linked = state["jdnow_guests"][1]
            self.assertEqual(linked["name"], "Ubisoft profile 2 · Linked Dancer")
            self.assertEqual(linked["controllers"], [])
            self.assertEqual(config.get_jdnow_player_id("right", linked["key"]), linked_id)
            self.assertEqual(config.get_jdnow_player_id("left"), "private-guest-profile-1234")
            self.assertNotIn(linked_id, json.dumps(state))

    def test_legacy_guest_and_names_survive_switching_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.cfg"
            original = "private-profile-left-1234"
            fresh = "private-profile-new-1234"
            path.write_text("[jdnow]\nplayers = " + json.dumps({"left": original}), encoding="utf-8")
            config = ConfigHandler(str(path))
            self.assertEqual(config.get_jdnow_player_id("left"), original)
            legacy = guest_state(config, "left")
            original_key = legacy["jdnow_guest"]
            self.assertEqual(legacy["jdnow_guests"], [
                {"key": original_key, "name": "Guest 1", "controllers": ["left"]}])
            self.assertEqual(guest_state(config, "right")["jdnow_guests"], legacy["jdnow_guests"])
            self.assertIsNone(config.get_jdnow_player_id("left", "new"))
            self.assertEqual(config.get_jdnow_player_id("left"), original)
            config.save_jdnow_player_id("left", original, "First dancer")
            config.save_jdnow_player_id("left", fresh, "Second dancer")
            config.save_jdnow_player_id("left", fresh, "Renamed dancer")
            reloaded = ConfigHandler(str(path))
            state = guest_state(reloaded, "left")
            fresh_key = state["jdnow_guest"]
            self.assertEqual([entry["name"] for entry in state["jdnow_guests"]],
                             ["Guest 1 · First dancer", "Guest 2 · Renamed dancer"])
            for invalid in (False, -1, 2, 0.0, "0", original):
                with self.assertRaises(ValueError):
                    reloaded.get_jdnow_player_id("left", invalid)
            self.assertEqual(reloaded.get_jdnow_player_id("left", original_key), original)
            self.assertEqual(reloaded.get_jdnow_player_id("right", original_key), original)
            reloaded.save_jdnow_player_id("right", original, "Borrowed dancer")
            reloaded.save_jdnow_player_id("left", original)
            restored = ConfigHandler(str(path))
            self.assertEqual(restored.get_jdnow_player_id("left"), original)
            self.assertEqual(restored.get_jdnow_player_id("left", fresh_key), fresh)
            self.assertEqual(guest_state(restored, "left"), {"jdnow_guests": [
                {"key": original_key, "name": "Guest 1 · Borrowed dancer", "controllers": ["left", "right"]},
                {"key": fresh_key, "name": "Guest 2 · Renamed dancer", "controllers": ["left"]}],
                "jdnow_guest": original_key})
            self.assertEqual(guest_state(restored, "right", {original}), {"jdnow_guests": [
                {"key": fresh_key, "name": "Guest 2 · Renamed dancer", "controllers": ["left"]}], "jdnow_guest": None})

    def test_room_and_guest_profile_survive_reload_without_leaking_into_ui_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "config.cfg")
            config = ConfigHandler(path)
            entry = config.save_connection({"name": "JDN", "pairing_method": "jdnow", "pairing_code": "22965"})
            self.assertIsNone(config.get_jdnow_player_id("left"))
            config.save_jdnow_player_id("left", "private-profile-left-1234")
            config.save_jdnow_player_id("right", "private-profile-right-1234")
            other = ConfigHandler(path)
            self.assertEqual(other.connections, [entry])
            self.assertEqual(other.get_jdnow_player_id("left"), "private-profile-left-1234")
            self.assertEqual(other.get_jdnow_player_id("right"), "private-profile-right-1234")
            self.assertNotIn("private-profile", json.dumps(other.data))
            for value in ("22965\n", "abcde", "123", "１２３４５", "1234567890", None, 22965):
                self.assertFalse(is_valid_pairing_code(value, "jdnow"))
            self.assertFalse(is_valid_pairing_code("22965", "default"))

    def test_failed_profile_save_rolls_back_existing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigHandler(str(Path(directory) / "config.cfg"))
            config.save_jdnow_player_id("left", "private-profile-left-1234", "Original")
            before = guest_state(config, "left")
            with patch.object(config, "save_data", return_value=False), self.assertRaises(OSError):
                config.save_jdnow_player_id("left", "private-profile-new-1234", "Unsaved")
            self.assertEqual(config.get_jdnow_player_id("left"), "private-profile-left-1234")
            self.assertEqual(guest_state(config, "left"), before)


class JDNowScoringTests(unittest.TestCase):
    def test_periodic_motion_check_preserves_the_energy_cutoff_and_zero_score_feedback(self):
        classifier = {"means": (1, 3), "weights": (0, 0), "diagonal": True,
                      "low": .2, "high": 5, "energy": (1, 1)}
        for disabled, derivative, expected_rating, check_required in (
                (False, 2.99, 0, False), (False, 3, 3, True), (False, 3.01, 3, True),
                (True, 2.99, 0, False), (True, 3, 1, False), (True, 3.01, 1, False)):
            with self.subTest(disabled=disabled, derivative=derivative):
                model = {**classifier, **({"low": -1, "high": -1} if disabled else {})}
                features = [(56, 1, 1), (61, derivative, 1)]
                with patch("joydance.games.jdnow.scoring.motion_features", return_value=([], features)), \
                        patch("joydance.games.jdnow.scoring.periodic_motion", return_value=False) as periodic:
                    result = score_move(model, [], 0, 100)
                    self.assertEqual(result[3], expected_rating)
                    self.assertEqual(periodic.call_count, int(check_required))
                    periodic.reset_mock()
                    periodic.return_value = True
                    suppressed = score_move(model, [], 0, 100)
                    self.assertEqual(periodic.call_count, int(check_required))
                    if check_required:
                        self.assertEqual(suppressed[3], 1)
                    else:
                        self.assertEqual(suppressed, result)
                    if derivative == 3:
                        self.assertEqual(result[1], float32(.3))

    def test_disabled_classifier_thresholds_are_accepted_without_relaxing_other_validation(self):
        for endian in (">", "<"):
            for low, high in ((-1, -1), (-1, 5), (.2, -1)):
                with self.subTest(endian=endian, low=low, high=high):
                    data = bytearray(classifier_bytes(endian))
                    struct.pack_into(endian + "ff", data, 204, low, high)
                    classifier = read_classifier(data)
                    self.assertEqual((classifier["low"], classifier["high"]),
                                     struct.unpack_from(endian + "ff", data, 204))
                    for distance in (0, 1, 10):
                        self.assertEqual(ratio_score(distance, classifier["low"], classifier["high"]), 0)
                    samples = [(t, 0, -1, 0) for t in range(0, 871, 10)]
                    self.assertEqual(score_move(classifier, samples, 0, REFERENCE["duration"])[3], 0)
                    for offset, value in ((204, float("nan")), (208, float("inf")), (len(data) - 4, 0)):
                        invalid = bytearray(data)
                        struct.pack_into(endian + "f", invalid, offset, value)
                        with self.assertRaises(ValueError):
                            read_classifier(invalid)

    def test_complete_moves_match_independent_original_engine_results(self):
        for endian in (">", "<"):
            classifier = read_classifier(classifier_bytes(endian))
            for case in REFERENCE["cases"]:
                amplitude, frequency = case["amplitude"], case["frequency"]
                samples = [(t, amplitude * math.sin(t / 1000 * frequency * 2 * math.pi),
                    -1 + amplitude * .5 * math.cos(t / 1000 * frequency * 2 * math.pi),
                    amplitude * math.sin(t / 1000 * frequency * 2 * math.pi * .7)) for t in range(0, 871, 10)]
                with self.subTest(endian=endian, amplitude=amplitude, frequency=frequency):
                    result = score_move(classifier, samples, 0, REFERENCE["duration"])
                    self.assertEqual(result[3], case["result"][3])
                    for actual, expected in zip(result[:3], case["result"][:3]):
                        self.assertTrue(math.isclose(actual, expected, rel_tol=1e-5, abs_tol=1e-5), (actual, expected))

    def test_adaptive_timing_matches_native_windows_and_learns_for_the_next_move(self):
        classifier = read_classifier(classifier_bytes())
        samples = []
        for timestamp in range(895, 1950, 10):
            angle = (timestamp - 1000) / 1000 * 2 * 2 * math.pi
            samples.append((timestamp, math.sin(angle), -1 + .5 * math.cos(angle), math.sin(angle * .7)))

        # The JDNow 9.9.0 ARM64 scorer ranks window 5 highest. The app plays window 8 first.
        result, pointer, offset = score_adaptive_move(classifier, samples, 1000, 841, .5)
        self.assertEqual(offset, 4)
        self.assertEqual(result[3], 3)
        for actual, expected in zip(result[:3], (.94793028, 1.39808905, .92589396)):
            self.assertAlmostEqual(actual, expected, places=6)
        self.assertAlmostEqual(pointer, .46666667)
        result, pointer, offset = score_adaptive_move(classifier, samples, 1000, 841, pointer)
        self.assertEqual(offset, 14)
        self.assertEqual(result[3], 3)
        for actual, expected in zip(result[:3], (.95144248, 1.40557182, .92490840)):
            self.assertAlmostEqual(actual, expected, places=6)
        self.assertAlmostEqual(pointer, .44)

        still = [(timestamp, 0, -1, 0) for timestamp, *_ in samples]
        result, unchanged, offset = score_adaptive_move(classifier, still, 1000, 841, pointer)
        self.assertEqual(result[3], 0)
        self.assertEqual(unchanged, pointer)

    def test_adaptive_pruning_preserves_selected_window_first_best_and_flat_scores(self):
        samples = [(t, 0, -1, 0) for t in range(-100, 301, 25)]
        starts = (100, 75, 50, 25, 0, -25, -50)
        for scores, periodic_starts, expected_score, expected_pointer, expected_checks in (
                ((.75, .8, .75, .8, .5, .25, .1), {75, 25}, float32(.1), .4, [25, 75, 100]),
                ((.75,) * 7, {-50}, .75, .4, [25, 100, 75, 50, 0, -25, -50]),
                ((.75,) * 7, set(), .75, .5, [25, 100, 75, 50, 0, -25, -50])):
            with self.subTest(periodic_starts=periodic_starts):
                upper_by_start = dict(zip(starts, scores))
                def prepare(classifier, motion, start, duration):
                    score = upper_by_start[start]
                    return (score, 1.0, 1.0, 3), [start] if score >= .25 else None

                with patch("joydance.games.jdnow.scoring._score_before_periodicity", side_effect=prepare), \
                        patch("joydance.games.jdnow.scoring.periodic_motion",
                              side_effect=lambda classifier, magnitudes, duration: magnitudes[0] in periodic_starts) as check:
                    result, pointer, offset = score_adaptive_move({}, samples, 0, 100, .5)
                self.assertEqual(result[0], expected_score)
                self.assertEqual(offset, 25)
                self.assertAlmostEqual(pointer, expected_pointer)
                self.assertEqual([call.args[1][0] for call in check.call_args_list], expected_checks)

    def test_bad_classifier_data_and_bad_motion_are_rejected(self):
        for data in (b"", classifier_bytes()[:243], classifier_bytes()[:-4], classifier_bytes() + b"extra"):
            with self.assertRaises(ValueError):
                read_classifier(data)
        for offset, fmt, value in ((4, "I", 99), (232, "i", 100000), (200, "f", float("nan")), (208, "f", .1)):
            data = bytearray(classifier_bytes())
            struct.pack_into(">" + fmt, data, offset, value)
            with self.assertRaises(ValueError):
                read_classifier(data)
        for samples in ([], [(0, 0, 0, 0)], [(1, 0, 0, 0), (0, 0, 0, 0)],
                        [(0, float("nan"), 0, 0), (1, 0, 0, 0)],
                        [(0, 1001, 0, 0), (1, 0, 0, 0)], [(0, 0, 0), (1, 0, 0, 0)]):
            with self.assertRaises(ValueError):
                resample(samples, 0, 100)


class JDNowScoringWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_processes_return_the_same_scores_as_local_scoring(self):
        with patch("joydance.games.jdnow.workers.os.cpu_count", return_value=2):
            workers = JDNowScoringWorkers()
        self.addCleanup(workers.close)
        classifier = read_classifier(classifier_bytes())
        motion = [(t, math.sin(t / 100), -1, math.cos(t / 75)) for t in range(895, 1950, 5)]
        args = [(classifier, motion, 1000, 841, pointer) for pointer in (.4, .6)]
        expected = [score_adaptive_move(*move) for move in args]

        workers.prepare(1)
        self.assertIsNone(workers._executor)
        self.assertEqual(await workers.score(*args[0]), expected[0])
        workers.prepare(2)
        await asyncio.wait_for(asyncio.gather(*(asyncio.wrap_future(job) for job in workers._warmup)), 15)
        with patch("joydance.games.jdnow.workers.asyncio.to_thread", side_effect=AssertionError("local fallback")):
            scores = await asyncio.gather(*(workers.score(*move) for move in args))
        self.assertEqual(scores, expected)

    async def test_failed_worker_retries_the_same_move_locally(self):
        workers = JDNowScoringWorkers()
        self.addCleanup(workers.close)
        workers._executor = Mock()
        workers._executor.submit.side_effect = BrokenProcessPool("worker stopped")
        args = (read_classifier(classifier_bytes()), [(t, 0, -1, 0) for t in range(0, 960, 5)], 0, 841, .5)
        expected = score_adaptive_move(*args)
        with self.assertLogs("joydance.games.jdnow.workers", level="WARNING"):
            self.assertEqual(await workers.score(*args), expected)
        self.assertEqual(await workers.score(*args), expected)
        workers._executor.submit.assert_called_once()


class JDNowFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_dictionary_frames_unicode_and_fragmented_stream(self):
        message = json.dumps({"func": "navRest", "song": "Dance", "text": "💃"}, ensure_ascii=False)
        frame = encode_frame(message)
        self.assertEqual(int(frame[:4], 36) >> 17, 2)
        self.assertEqual(decode_payload(frame[:4], frame[4:]), message)
        reader = asyncio.StreamReader()
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
        connection = JDNowConnection(reader, writer)
        messages = connection.__aiter__()
        pending = asyncio.create_task(anext(messages))
        reader.feed_data(frame[:2])
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        reader.feed_data(frame[2:] + frame)
        self.assertEqual(await pending, message)
        self.assertEqual(await anext(messages), message)
        reader.feed_eof()
        with self.assertRaisesRegex(GameConnectionError, "server closed the TCP"):
            await anext(messages)
        await connection.close()
        self.assertIs(connection.state, State.CLOSED)

    def test_invalid_headers_truncated_deflate_and_inflation_limit(self):
        frame = encode_frame('{"func":"pong"}')
        for header, body in ((b"!!!!", b"abc"), (frame[:4], frame[4:-1]), (b"0001", b"\xff")):
            with self.assertRaises(GameConnectionError):
                decode_payload(header, body)
        encoder = zlib.compressobj(wbits=-15, zdict=DICTIONARY)
        body = encoder.compress(b"x" * (1024 * 1024)) + encoder.flush()
        value, header = len(body) | (2 << 17), ""
        while value:
            value, digit = divmod(value, 36)
            header = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + header
        with self.assertRaises(GameConnectionError):
            decode_payload(header.zfill(4).encode(), body)


class JDNowAPITests(unittest.IsolatedAsyncioTestCase):
    def session_with_responses(self, responses):
        contexts = []
        for status, body in responses:
            async def chunks(size, body=body):
                yield json.dumps(body).encode()
            context = MagicMock()
            context.__aenter__.return_value = SimpleNamespace(status=status, content=SimpleNamespace(iter_chunked=chunks))
            contexts.append(context)
        return Mock(request=Mock(side_effect=contexts))

    async def test_expired_token_is_renewed_and_rename_retried_for_the_same_guest(self):
        profile = {"_id": "private-profile-123456", "publicID": "public-profile-123456", "pName": "Original"}
        hello = {"dancercard": profile, "authToken": "new-token", "api": "https://api.justdancenow.com/v1/"}
        relay = {"func": "updateDancercard", "player": {"name": "test"}}
        session = self.session_with_responses([(401, {}), (200, {"userToken": "guest-token"}),
            (200, hello), (200, {"updated": {"pName": "test"}, "passToDRS": [relay]})])
        saved = Mock()
        api = JDNowAPI(session, profile["_id"], saved)
        api.profile, api.auth_token = dict(profile), "expired-token"
        with self.assertLogs("joydance.games.jdnow.api", level="DEBUG") as logs:
            self.assertEqual(await api.rename_player("test"), [relay])
        self.assertEqual(api.profile["pName"], "test")
        self.assertEqual(api.auth_token, "new-token")
        self.assertEqual(api.player_id, profile["_id"])
        saved.assert_not_called()
        calls = session.request.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0].kwargs["headers"]["x-auth-token"], "Bearer expired-token")
        self.assertNotIn("x-auth-token", calls[1].kwargs["headers"])
        self.assertEqual(calls[2].kwargs["data"]["text"], profile["_id"])
        self.assertEqual(calls[3].kwargs["headers"]["x-auth-token"], "Bearer new-token")
        self.assertEqual(calls[3].kwargs["json"], calls[0].kwargs["json"])
        self.assertNotIn("expired-token", "\n".join(logs.output))
        self.assertNotIn("new-token", "\n".join(logs.output))

    async def test_authentication_retry_is_bounded_and_cannot_switch_guest_identity(self):
        original = {"_id": "private-profile-123456", "publicID": "public-profile-123456"}
        for changed_identity in (False, True):
            profile = {**original, **({"publicID": "different-profile-123456"} if changed_identity else {})}
            hello = {"dancercard": profile, "authToken": "new-token", "api": "https://api.justdancenow.com/v1/"}
            session = self.session_with_responses([(401, {}), (200, {"userToken": "guest-token"}), (200, hello), (401, {})])
            api = JDNowAPI(session, original["_id"])
            api.profile, api.auth_token = dict(original), "expired-token"
            with self.assertRaises(ValueError if changed_identity else GameConnectionError):
                await api.rename_player("test")
            self.assertEqual(session.request.call_count, 3 if changed_identity else 4)
            if changed_identity:
                self.assertEqual(api.auth_token, "expired-token")
                self.assertEqual(api.profile, original)

    async def test_concurrent_expired_requests_share_one_guest_renewal(self):
        api = JDNowAPI(None, "private-profile-123456")
        api.auth_token, api.profile = "expired-token", {"publicID": "public-profile-123456"}
        async def login(refreshed):
            await asyncio.sleep(0)
            refreshed.auth_token = "new-token"
            refreshed.profile = dict(api.profile)
        with patch.object(JDNowAPI, "login", autospec=True, side_effect=login) as renew:
            await asyncio.gather(api._refresh_auth("expired-token"), api._refresh_auth("expired-token"))
        self.assertEqual(renew.await_count, 1)

    async def test_rename_uses_the_confirmed_name_and_preserves_the_room_profile(self):
        api = JDNowAPI(None, "private-profile-123456")
        profile = {"favorites": ["TestSong"], "avatar": 4, "fullBodyAvatar": 1004, "country": "GB",
                   "ftueDatas": '{"states":{"PlaylistLimit":0}}'}
        api.profile = {"pName": "Original", **profile}
        relay = {"func": "updateDancercard", "player": {"name": "New Dancer", "avatar": 5}}
        api.request = AsyncMock(return_value=json.dumps({"updated": {"pName": "New Dancer"}, "passToDRS": [relay]}).encode())
        self.assertEqual(await api.rename_player("  New  Dancer  "), [
            {"func": "updateDancercard", "player": {"name": "New Dancer", "avatar": 5, "fullBodyAvatar": 1004,
                                                   "country": "GB", "ftueDatas": profile["ftueDatas"]}}])
        api.request.assert_awaited_once_with("POST", api.jdns + "updateDancercard", authenticated=True,
                                           json={"_id": "private-profile-123456", "pName": "New  Dancer"}, limit=1024 * 1024)
        self.assertEqual(api.profile, {"pName": "New Dancer", **profile})
        for value in (None, "", "   ", "a" * 21, "a" * 19 + "\U0001f600", "Name\nNext", "Name\x00", "\ud800"):
            with self.assertRaises(ValueError):
                await api.rename_player(value)
        self.assertEqual(api.request.await_count, 1)
        for updated in ({}, {"pName": ""}, {"pName": "a" * 21}, {"pName": "Name\nNext"}):
            api.request.return_value = json.dumps({"updated": updated}).encode()
            with self.assertRaisesRegex(ValueError, "did not confirm"):
                await api.rename_player("New Dancer")
        self.assertEqual(api.profile["pName"], "New Dancer")

    async def test_categories_preserve_the_service_order_and_reject_invalid_song_ids(self):
        api = JDNowAPI(None)
        entries = [{"id": "Playlist", "trackingId": "Test", "songs": ["Zulu", "Alpha"]}]
        api.request = AsyncMock(return_value=json.dumps(entries).encode())
        self.assertEqual(await api.categories(), entries)
        self.assertTrue(api.request.await_args.kwargs["authenticated"])
        for entries in ({}, [{"id": "../escape", "songs": []}], [{"id": "Test", "songs": "Alpha"}],
                        [{"id": "Test", "songs": ["../escape"]}], [{"id": "Test", "songs": [], "trackingId": {}}]):
            api.request = AsyncMock(return_value=json.dumps(entries).encode())
            with self.assertRaises(ValueError):
                await api.categories()

    async def test_guest_requests_identify_the_desktop_platform(self):
        async def chunks(size):
            yield b'{}'
        response = SimpleNamespace(status=200, content=SimpleNamespace(iter_chunked=chunks))
        context = MagicMock()
        context.__aenter__.return_value = response
        session = Mock(request=Mock(return_value=context))
        api = JDNowAPI(session, player_id="saved-private-profile-1234")
        await api.request("GET", "https://jdns.justdancenow.com/getUserToken/", params={"_id": api.player_id})
        self.assertEqual(session.request.call_args.kwargs["headers"], {"x-platform": "pcapp"})
        self.assertEqual(session.request.call_args.kwargs["params"], {"_id": "saved-private-profile-1234"})

    async def test_login_uses_guest_sentinel_and_saves_server_identity(self):
        saved = Mock()
        api = JDNowAPI(None, on_guest=saved)
        hello = {"dancercard": {"_id": "private-profile-123456", "publicID": "public-profile-123456", "pName": "Dancer"},
                 "authToken": "synthetic-token", "api": "https://api.justdancenow.com/v1/"}
        api.request = AsyncMock(side_effect=[json.dumps(value).encode() for value in (
            {"userToken": "synthetic-user-token"}, hello)])
        self.assertEqual(await api.login(), hello)
        self.assertEqual(api.request.await_args_list[0].args[1], "https://jdns.justdancenow.com/getUserToken/")
        self.assertEqual(api.request.await_args_list[0].kwargs["params"], {"_id": "Hello"})
        self.assertEqual(api.request.await_args_list[1].kwargs["data"]["userToken"], "synthetic-user-token")
        self.assertNotIn("json", api.request.await_args_list[1].kwargs)
        saved.assert_called_once_with("private-profile-123456", "Dancer")
        self.assertEqual(api.player_id, "private-profile-123456")

    async def test_ubisoft_sign_in_can_link_a_dancer_without_changing_its_id(self):
        token = (200, {"userToken": "synthetic-user-token"})
        guest = {"_id": "temporary-profile-1234", "publicID": "temporary-public-1234"}
        linked = {**guest, "pName": "Linked Dancer", "ubiConnectIdMapped": True,
                  "syncData": {"localAccountData": {"globalLevel": 1}, "serverAccountData": {"globalLevel": 10}}}

        def hello(profile):
            return 200, {"dancercard": profile, "authToken": "synthetic-token", "api": "https://api.justdancenow.com/v1/"}

        user_id = "12345678-1234-1234-1234-123456789abc"
        session = self.session_with_responses((token, hello(guest), token, hello(linked), token, hello(linked)))
        with patch("joydance.games.jdnow.signin.aiohttp.ClientSession") as client_session:
            client_session.return_value.__aenter__.return_value = session
            self.assertEqual(await recover_profile(user_id), (linked["_id"], "Linked Dancer"))
        requests = session.request.call_args_list
        self.assertEqual(requests[3].kwargs["data"]["text"], guest["_id"])
        self.assertEqual(requests[3].kwargs["data"]["ubiConnectId"], user_id)
        self.assertEqual(requests[5].kwargs["data"]["text"], linked["_id"])
        self.assertNotIn("ubiConnectId", requests[5].kwargs["data"])

    async def test_region_redirect_and_endpoint_validation(self):
        api = JDNowAPI(None)
        api.jdns = "https://first.justdancenow.com/"
        api.request = AsyncMock(side_effect=[b'{"jdns":"https://second.justdancenow.com/"}',
                                             b'{"drs":"drs.justdancenow.com","port":4001}'])
        self.assertEqual(await api.room_endpoint("22965"), ("drs.justdancenow.com", 4001))
        self.assertTrue(api.request.await_args_list[1].args[1].startswith("https://second."))
        for endpoint in ({"drs": "127.0.0.1", "port": 4001}, {"drs": "drs.justdancenow.com.evil.test", "port": 443},
                         {"drs": "drs.justdancenow.com", "port": True}):
            api.request = AsyncMock(return_value=json.dumps(endpoint).encode())
            with self.assertRaises((ValueError, GameConnectionError)):
                await api.room_endpoint("22965")

    async def test_credentials_cannot_be_sent_to_asset_host(self):
        api = JDNowAPI(Mock())
        api.jdns, api.auth_token = "https://jdns.justdancenow.com/", "private-token"
        with self.assertRaises(ValueError):
            await api.request("GET", "https://cdn.justdancenow.com/asset", authenticated=True)
        api.session.request.assert_not_called()
        for url in ("http://jdns.justdancenow.com/", "https://jdns.justdancenow.com.evil.test/",
                    "https://user:pass@jdns.justdancenow.com/", "https://127.0.0.1/"):
            with self.assertRaises(ValueError):
                service_url(url)
        self.assertEqual(parse_json(b'callback({"moves":[]});', jsonp=True), {"moves": []})
        with self.assertRaises(ValueError):
            parse_json(b'{"score":NaN}')

    async def test_chunked_responses_are_read_completely_and_bounded(self):
        async def chunks(size):
            for value in (b"first", b"second", b"third"):
                yield value
        response = SimpleNamespace(content=SimpleNamespace(iter_chunked=chunks))
        self.assertEqual(await read_limited(response, 32), b"firstsecondthird")
        with self.assertRaises(ValueError):
            await read_limited(response, 10)


class JDNowGameTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_color_uses_the_server_color_and_ignores_invalid_values(self):
        instance = game()
        await instance.on_message('{"func":"racetrackColorUpdated","color":"blue","colorHexa":"#00c8ff"}')
        self.assertEqual(instance._state["player_color"], [0, 200, 255, 1])
        for color in ("invalid", "#ff0000;display:none", [256, 0, 0, 255], [True, 0, 0, 255]):
            await instance.on_message(json.dumps({"func": "racetrackColorUpdated", "colorHexa": color}))
            self.assertEqual(instance._state["player_color"], [0, 200, 255, 1])

    async def test_search_matches_title_and_artist_and_jumps_to_the_song_in_all_songs(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        instance.catalog["IdZ"]["artist"] = "Some Singer"
        instance.catalog["Hidden"]["artist"] = "Some Singer"
        await instance.search_songs("SINGER")
        self.assertEqual([song["id"] for song in instance._state["song_search_results"]], ["IdZ"])
        await instance.search_songs("a")
        self.assertEqual([song["id"] for song in instance._state["song_search_results"]], ["IdZ", "IdB"])
        instance._tab, instance._category, instance._playlist_picker = "playlist", "Mix", True
        with patch("joydance.games.jdnow.game.CONFIRMATION_SECONDS", 0):
            await instance.select_song("IdB")
            self.assertIn("Banana", instance._state["song_search_feedback"]["text"])
            await instance._confirmation_tasks["song_search_feedback"]
        messages = [json.loads(call.args[0]) for call in instance.ws.send.await_args_list]
        self.assertEqual(messages, [{"func": "tabRest", "tabName": "songlist"},
                                    {"func": "navRest", "song": "IdB", "category": ""}])
        self.assertEqual((instance._tab, instance._category, instance._current_song), ("songlist", "", "IdB"))
        self.assertFalse(instance._playlist_picker)
        self.assertFalse(instance._purchase_pending)
        self.assertEqual(instance._phase, "browsing")
        self.assertTrue(instance._state["song_search_enabled"])
        self.assertEqual([song["id"] for song in instance._state["song_search_results"]], ["IdZ", "IdB"])
        self.assertEqual(instance._state["song_search_total"], 2)
        self.assertIsNone(instance._state["song_search_feedback"])
        self.assertIsNone(instance._state["additional_message"])
        await instance.search_songs(" ")
        self.assertEqual(instance._state["song_search_results"], [])
        self.assertIsNone(instance._state["song_search_total"])
        self.assertIsNone(instance._state["song_search_feedback"])
        self.assertIsNone(instance._load_task)
        self.assertTrue(await instance._navigate(Command.RIGHT))
        self.assertEqual(instance._current_song, "IdA")

    async def test_search_is_bounded_and_unavailable_during_dance_or_for_other_players(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        for value in ("a" * 101, None):
            with self.assertRaises(ValueError):
                await instance.search_songs(value)
        with self.assertRaises(ValueError):
            await instance.select_song("Hidden")
        instance.catalog = {f"Song{i}": {"name": f"Test {i}"} for i in range(30)}
        instance._song_order = list(instance.catalog)
        await instance.search_songs("test")
        self.assertEqual(len(instance._state["song_search_results"]), 20)
        self.assertEqual(instance._state["song_search_total"], 30)
        await instance.search_songs("no match")
        self.assertEqual(instance._state["song_search_results"], [])
        self.assertEqual(instance._state["song_search_total"], 0)
        for connected, main, phase, collecting in (
                (True, True, "dancing", True), (True, True, "coach", False),
                (True, False, "browsing", False), (False, True, "browsing", False),
                (True, True, "browsing", True)):
            instance.is_connected, instance._is_main_player = connected, main
            instance._phase, instance.should_start_accelerometer = phase, collecting
            await instance.on_state_changed({})
            self.assertFalse(instance._state["song_search_enabled"])
            self.assertIsNone(instance._state["song_search_total"])
            self.assertFalse(instance.controller.available_shortcuts)
            with self.assertRaises(ValueError):
                await instance.search_songs("Test")
            with self.assertRaises(ValueError):
                await instance.select_song("Song0")
        instance.ws.send.assert_not_awaited()

    async def test_rename_forwards_the_service_notification_and_updates_the_web_name(self):
        instance = game()
        instance.is_connected = True
        instance.api.player_id = "private-profile-123456"
        instance._on_guest = Mock()
        relay = {"func": "updateDancercard", "player": {"name": "New Dancer"}}
        async def rename(name):
            instance.api.profile["pName"] = name
            return [relay]
        instance.api.rename_player = AsyncMock(side_effect=rename)
        with patch("joydance.games.jdnow.game.CONFIRMATION_SECONDS", 0):
            await instance.rename_player("New Dancer")
            self.assertEqual(instance._state["player_rename_feedback"], {"text": "Player name updated."})
            await instance._confirmation_tasks["player_rename_feedback"]
        self.assertEqual(json.loads(instance.ws.send.await_args.args[0]), relay)
        self.assertEqual(instance._state["player_name"], "New Dancer")
        self.assertIsNone(instance._state["player_rename_feedback"])
        self.assertIsNone(instance._state["additional_message"])
        instance._on_guest.assert_called_once_with("private-profile-123456", "New Dancer")
        instance.is_connected = False
        with self.assertRaises(ValueError):
            await instance.rename_player("Other Dancer")
        instance.api.rename_player.assert_awaited_once_with("New Dancer")

    async def test_rename_confirmation_expiry_leaves_newer_error_visible(self):
        instance = game()
        instance.is_connected = True
        instance.api.rename_player = AsyncMock(return_value=[])
        error = {"text": "Rename failed.", "error": True}
        with patch("joydance.games.jdnow.game.CONFIRMATION_SECONDS", 0):
            await instance.rename_player("New Dancer")
            await instance.on_state_changed({"player_rename_feedback": error})
            await instance._confirmation_tasks["player_rename_feedback"]
        self.assertEqual(instance._state["player_rename_feedback"], error)

    async def test_rename_keeps_the_saved_name_when_the_room_update_fails(self):
        for failure in (None, OSError("Connection lost"), GameConnectionError("Room closed")):
            with self.subTest(failure=failure):
                instance = game()
                instance.is_connected = True
                instance.api.player_id = "private-profile-123456"
                instance._on_guest = Mock()
                instance.ws.send.side_effect = failure

                async def rename(_):
                    instance.api.profile["pName"] = "New Dancer"
                    if failure is None:
                        instance.ws.state = State.CLOSED
                    return [{"func": "updateDancercard", "player": {"name": "New Dancer"}}]

                instance.api.rename_player = AsyncMock(side_effect=rename)
                await instance.rename_player("New  Dancer")
                instance._on_guest.assert_called_once_with("private-profile-123456", "New Dancer")
                self.assertEqual(instance._state["player_name"], "New Dancer")
                instance._reset_input_state()
                self.assertEqual(instance._state["player_rename_feedback"], {
                    "text": "Player name saved. Reconnect to update the Dance Room.", "error": True,
                })
                self.assertFalse(instance._confirmation_tasks)

    async def test_search_and_rename_feedback_are_independent(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        search_error = {"text": "Search failed.", "error": True}
        rename_error = {"text": "Rename failed.", "error": True}
        with patch("joydance.games.jdnow.game.CONFIRMATION_SECONDS", 0):
            await instance.select_song("IdZ")
            await instance.on_state_changed({"player_rename_feedback": rename_error, "song_search_feedback": search_error})
            await instance._confirmation_tasks["song_search_feedback"]
        await instance.on_message('{"func":"navRest","category":"","song":"IdZ","sender":"web"}')
        self.assertEqual(instance._state["player_rename_feedback"], rename_error)
        self.assertEqual(instance._state["song_search_feedback"], search_error)
        await instance.search_songs("alpha")
        self.assertEqual(instance._state["player_rename_feedback"], rename_error)
        self.assertIsNone(instance._state["song_search_feedback"])

    def navigation_game(self):
        instance = game()
        instance.catalog = {"IdA": {"name": "Zulu"}, "IdB": {"name": "Banana"},
                            "IdZ": {"name": "alpha"}, "Hidden": {"name": "A hidden song"}}
        instance._configure_navigation([
            {"id": "Mix", "songs": ["IdA", "Missing", "IdZ", "IdA", "Hidden"]},
            {"id": "Exclusive", "trackingId": "UVS", "songs": ["Hidden"]},
        ])
        return instance

    async def test_all_songs_follow_display_titles_and_do_not_skip_the_first_song(self):
        instance = self.navigation_game()
        for expected in ("IdZ", "IdB", "IdA"):
            self.assertTrue(await instance._navigate(Command.RIGHT))
            self.assertEqual(instance._current_song, expected)
        calls = instance.ws.send.await_count
        self.assertFalse(await instance._navigate(Command.RIGHT))
        self.assertEqual(instance.ws.send.await_count, calls)
        messages = [json.loads(call.args[0]) for call in instance.ws.send.await_args_list]
        self.assertEqual(messages[0], {"func": "tabRest", "tabName": "songlist"})
        self.assertEqual([m["song"] for m in messages if m["func"] == "navRest"], ["IdZ", "IdB", "IdA"])

    async def test_vertical_navigation_uses_browser_rows_and_keeps_the_column_at_edges(self):
        instance = game()
        instance.catalog = {f"Song{i}": {"name": f"Song {i}"} for i in range(8)}
        instance._song_order = list(instance.catalog)
        instance._current_song = "Song1"
        self.assertFalse(await instance._navigate(Command.UP))
        self.assertTrue(await instance._navigate(Command.DOWN))
        self.assertEqual(instance._current_song, "Song4")
        self.assertTrue(await instance._navigate(Command.DOWN))
        self.assertEqual(instance._current_song, "Song7")
        self.assertFalse(await instance._navigate(Command.DOWN))
        self.assertTrue(await instance._navigate(Command.UP))
        self.assertEqual(instance._current_song, "Song4")
        instance._current_song = "Song5"
        self.assertFalse(await instance._navigate(Command.DOWN))
        self.assertEqual(instance._current_song, "Song5")
        instance._current_song = ""
        self.assertTrue(await instance._navigate(Command.DOWN))
        self.assertEqual(instance._current_song, "Song0")

    async def test_vertical_navigation_inside_a_playlist_moves_one_song(self):
        instance = game()
        instance.catalog = {f"Song{i}": {} for i in range(8)}
        instance._playlists = {"Mix": ["Song7", "Song2", "Song4", "Song1"]}
        instance._category, instance._current_song = "Mix", "Song2"
        self.assertTrue(await instance._navigate(Command.DOWN))
        self.assertEqual(instance._current_song, "Song4")
        self.assertTrue(await instance._navigate(Command.UP))
        self.assertEqual(instance._current_song, "Song2")

    async def test_playlist_picker_follows_visible_service_order_and_updates_favorites(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        categories = [{"id": key, "name": key, "songs": songs, **flags} for key, songs, flags in (
            ("Mix", ["IdZ", "IdA"], {}), ("Hidden", ["IdA"], {"isHiddenForAll": True}),
            ("TV", ["IdA"], {"isShowOnlyForTvos": True}), ("Pack", ["IdA"], {"isSongPack": True}),
            ("FTUE", ["IdA"], {"trackingId": "FTUE"}), ("Empty", [], {}),
            ("Featured", ["IdB"], {"isFeatured": True}),
        )]
        instance._configure_navigation(categories)
        await instance._switch_tab("playlist")
        self.assertEqual(instance.controller.available_shortcuts, {Command.GOTO_SONGSTAB})
        self.assertTrue(instance._playlist_picker)
        self.assertEqual(instance._category, "Mix")
        await instance._navigate(Command.DOWN)
        self.assertEqual(instance._category, "Featured")
        self.assertFalse(await instance._navigate(Command.RIGHT))
        self.assertEqual(instance._current_song, "")
        instance._set_favorites(["IdA"])
        self.assertEqual(instance._playlist_order[0], "FAVORITES")
        instance._set_favorites([])
        self.assertNotIn("FAVORITES", instance._playlist_order)
        self.assertIsNone(instance._state["additional_message"])

    async def test_joycon_switches_tabs_previews_playlists_and_enters_and_leaves_their_songs(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        instance._feedback = AsyncMock()
        instance._playlists["Other"] = ["IdZ", "IdB"]
        instance._playlist_order = ["Mix", "Other"]
        commands = deque([Command.GOTO_PLAYLISTSTAB, Command.RIGHT, Command.ACCEPT,
                          Command.DOWN, Command.BACK, Command.GOTO_SONGSTAB])
        instance.controller.get_latest_command.side_effect = lambda **kwargs: commands.popleft()
        async def advance(duration):
            if not commands:
                raise asyncio.CancelledError
        with patch("joydance.games.jdnow.game.asyncio.sleep", side_effect=advance):
            with self.assertRaises(asyncio.CancelledError):
                await instance.send_command()
        messages = [json.loads(call.args[0]) for call in instance.ws.send.await_args_list]
        self.assertEqual([m["func"] for m in messages], ["tabRest", "playlistSelected", "playlistSelected",
                                                        "navRest", "navRest", "playlistSelected", "tabRest", "navRest"])
        self.assertEqual([m["category"] for m in messages if m["func"] == "playlistSelected"], ["Mix", "Other", "Other"])
        self.assertEqual([m["song"] for m in messages if m["func"] == "navRest"], ["IdZ", "IdB", "IdZ"])
        self.assertEqual(instance._tab, "songlist")
        self.assertFalse(instance._playlist_picker)
        self.assertFalse(instance._purchase_pending)
        instance._feedback.assert_not_awaited()

    async def test_navigation_uses_warning_rumble_only_at_list_edges(self):
        for category, song, picker, direction, busy, expected in (
                ("", "IdZ", False, Command.UP, False, [37]),
                ("Mix", "IdZ", False, Command.DOWN, False, [37]),
                ("Mix", "", True, Command.RIGHT, False, [37]),
                ("Mix", "IdZ", False, Command.DOWN, True, []),
                ("Unavailable", "", False, Command.RIGHT, False, [])):
            with self.subTest(category=category, picker=picker, busy=busy):
                instance = self.navigation_game()
                instance.is_connected = instance._is_main_player = True
                instance._category, instance._current_song, instance._playlist_picker = category, song, picker
                if busy:
                    instance._pending_navigation.extend([(song, category)] * 32)
                instance._feedback = AsyncMock()
                instance.controller.get_latest_command.return_value = direction
                with patch("joydance.games.jdnow.game.asyncio.sleep", side_effect=asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await instance.send_command()
                self.assertEqual([call.args[0] for call in instance._feedback.await_args_list], expected)
                instance.ws.send.assert_not_awaited()

    async def test_held_stick_warns_once_at_the_edge_until_released(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        instance._tab, instance._current_song = "songlist", "IdB"
        sticks = [Command.RIGHT] * 4 + [None, Command.RIGHT, Command.RIGHT]
        frame = 0
        feedback = []
        instance.controller.get_joystick_command.side_effect = lambda: sticks[frame]
        instance._feedback = AsyncMock(side_effect=lambda effect: feedback.append((frame, effect)))

        async def advance(_):
            nonlocal frame
            frame += 1
            if frame == len(sticks):
                raise asyncio.CancelledError

        with patch("joydance.games.jdnow.game.asyncio.sleep", side_effect=advance), \
                patch("joydance.games.jdnow.game.time.monotonic", side_effect=lambda: frame * .5):
            with self.assertRaises(asyncio.CancelledError):
                await instance.send_command()
        self.assertEqual(feedback, [(1, 37), (5, 37)])
        self.assertEqual(instance._current_song, "IdA")

    async def test_old_tab_and_playlist_replies_cannot_undo_newer_navigation_or_mouse_selection(self):
        instance = self.navigation_game()
        await instance._switch_tab("playlist")
        await instance._switch_tab("songlist")
        await instance.on_message('{"func":"playlistSelected","category":"Mix","sender":"app"}')
        await instance.on_message('{"func":"tabRest","tabName":"playlist","sender":"app"}')
        self.assertEqual((instance._tab, instance._current_song), ("songlist", "IdZ"))
        await instance.on_message('{"func":"tabRest","tabName":"playlist","sender":"web"}')
        await instance.on_message('{"func":"playlistSelected","category":"Mix","sender":"web"}')
        await instance.on_message('{"func":"tabRest","tabName":"songlist","sender":"app"}')
        await instance.on_message('{"func":"navRest","song":"IdZ","category":"","sender":"app"}')
        self.assertEqual((instance._tab, instance._category, instance._current_song), ("playlist", "Mix", ""))
        self.assertFalse(instance._playlist_picker)
        self.assertFalse(instance._pending_tabs)
        self.assertFalse(instance._pending_navigation)

    async def test_playlist_order_and_delayed_replies_preserve_the_latest_selection(self):
        instance = self.navigation_game()
        await instance.on_message(json.dumps({"func": "playlistSelected", "category": "Mix", "sender": "web"}))
        await instance._navigate(Command.RIGHT)
        self.assertEqual(instance._current_song, "IdA")
        await instance._navigate(Command.RIGHT)
        self.assertEqual(instance._current_song, "IdZ")
        await instance.on_message(json.dumps({"func": "navRest", "song": "IdA", "category": "Mix", "sender": "app"}))
        self.assertEqual(instance._current_song, "IdZ")
        # A browser click wins even when an older controller move is still awaiting its reply.
        await instance.on_message(json.dumps({"func": "navRest", "song": "IdA", "category": "Mix", "sender": "web"}))
        await instance.on_message(json.dumps({"func": "navRest", "song": "IdZ", "category": "Mix", "sender": "app"}))
        self.assertEqual(instance._current_song, "IdA")
        self.assertFalse(instance._pending_navigation)
        messages = [json.loads(call.args[0]) for call in instance.ws.send.await_args_list]
        self.assertEqual([m["song"] for m in messages], ["IdA", "IdZ"])
        self.assertTrue(all(m["category"] == "Mix" for m in messages))

    async def test_browser_tab_change_leaves_the_playlist_and_old_echo_cannot_restore_it(self):
        instance = self.navigation_game()
        await instance.on_message(json.dumps({"func": "playlistSelected", "category": "Mix"}))
        await instance._navigate(Command.RIGHT)
        await instance.on_message(json.dumps({"func": "tabRest", "tabName": "songlist", "sender": "web"}))
        await instance._navigate(Command.RIGHT)
        await instance.on_message(json.dumps({"func": "navRest", "song": "IdA", "category": "Mix", "sender": "app"}))
        self.assertEqual(instance._category, "")
        self.assertEqual(instance._current_song, "IdZ")
        await instance.on_message(json.dumps({"func": "playlistSelected", "category": "Unavailable"}))
        self.assertFalse(await instance._navigate(Command.RIGHT))

    async def test_out_of_order_navigation_replies_cannot_jump_back(self):
        instance = self.navigation_game()
        await instance._navigate(Command.RIGHT)
        await instance._navigate(Command.RIGHT)
        for song in ("IdB", "IdZ"):
            await instance.on_message(json.dumps({"func": "navRest", "song": song, "category": "", "sender": "app"}))
            self.assertEqual(instance._current_song, "IdB")
        self.assertFalse(instance._pending_navigation)

    async def test_navigation_waits_for_replies_without_changing_selection_or_spending_coins(self):
        for queue, target, reply in (
                ("_pending_tabs", "songlist", {"func": "tabRest", "tabName": "songlist"}),
                ("_pending_navigation", ("IdA", ""), {"func": "navRest", "song": "IdA", "category": ""})):
            with self.subTest(queue=queue):
                instance = self.navigation_game()
                instance.is_connected = instance._is_main_player = True
                getattr(instance, queue).extend([target] * 32)
                self.assertFalse(await instance._navigate(Command.RIGHT))
                await instance._switch_tab("playlist")
                with self.assertRaisesRegex(ValueError, "Navigation is still updating"):
                    await instance.select_song("IdZ")
                self.assertEqual((instance._tab, instance._category, instance._current_song), (None, "", ""))
                self.assertEqual(instance._phase, "browsing")
                self.assertFalse(instance._purchase_pending)
                instance.ws.send.assert_not_awaited()
                await instance.on_message(json.dumps({**reply, "sender": "app"}))
                self.assertTrue(await instance._navigate(Command.RIGHT))
                self.assertEqual(instance._current_song, "IdZ")
                self.assertLessEqual(len(instance._pending_tabs), 32)
                self.assertLessEqual(len(instance._pending_navigation), 32)

    async def test_favorites_follow_the_profile_order_and_live_updates(self):
        instance = self.navigation_game()
        instance.api.profile["favorites"] = ["IdZ", "IdA"]
        instance._configure_navigation([])
        await instance.on_message(json.dumps({"func": "playlistSelected", "category": "FAVORITES"}))
        for expected in ("IdZ", "IdA"):
            await instance._navigate(Command.RIGHT)
            self.assertEqual(instance._current_song, expected)
        await instance.on_message(json.dumps({"func": "favoritesChanged", "playerID": "public-profile-123456",
                                              "favorites": ["IdB", "Missing", "IdZ", "IdB"]}))
        await instance._navigate(Command.RIGHT)
        self.assertEqual(instance._current_song, "IdB")
        with self.assertRaises(ValueError):
            instance._set_favorites("IdB")

    async def test_stick_neutral_resets_repeat_delay_without_repeating_each_frame(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        sticks = [Command.RIGHT, None, Command.RIGHT] + [Command.RIGHT] * 5
        frame = 0
        def joystick():
            return sticks[frame]
        async def advance(duration):
            nonlocal frame
            frame += 1
            if frame == len(sticks):
                raise asyncio.CancelledError
        instance.controller.get_joystick_command.side_effect = joystick
        with patch("joydance.games.jdnow.game.asyncio.sleep", side_effect=advance), \
                patch("joydance.games.jdnow.game.time.monotonic", side_effect=lambda: frame * .05):
            with self.assertRaises(asyncio.CancelledError):
                await instance.send_command()
        messages = [json.loads(call.args[0]) for call in instance.ws.send.await_args_list]
        self.assertEqual([m["song"] for m in messages if m["func"] == "navRest"], ["IdZ", "IdB", "IdA"])

    async def test_application_dispatch_creates_and_restores_guests_without_exposing_ids(self):
        import dance

        with tempfile.TemporaryDirectory() as directory:
            config = ConfigHandler(str(Path(directory) / "config.cfg"))
            original, fresh = "private-profile-123456", "private-profile-updated-1234"
            config.save_jdnow_player_id("test-jdn", original, "Original")
            original_key = guest_state(config, "test-jdn")["jdnow_guest"]
            controller = game().controller
            scoring_workers = Mock(spec=JDNowScoringWorkers)
            assets = Mock(spec=JDNowAssets)
            app = {"config_handler": config, "controllers": {controller.serial: controller},
                   "controllers_info": {controller.serial: {}}, "joydance_connections": {}, "websockets": set(),
                   "joydance_connection_locks": {controller.serial: asyncio.Lock()}, "jdnow_guest_lock": asyncio.Lock(),
                   "jdnow_scoring_workers": scoring_workers, "jdnow_assets": assets}
            data = {"controller_serial": controller.serial, "pairing_method": "jdnow", "pairing_code": "22965",
                    "host_ip_addr": "", "console_ip_addr": ""}
            with patch.object(JustDanceGameNow, "start_pair", new=AsyncMock(return_value=Mock())):
                await dance.connect_controller(app, Mock(), data)
                connection = app["joydance_connections"][controller.serial]
                self.assertIsInstance(connection, JustDanceGameNow)
                self.assertEqual(connection._player_id, original)
                self.assertIsNone(connection.console_ip_addr)
                self.assertIs(connection._scoring_workers, scoring_workers)
                self.assertIs(connection._assets, assets)
                await dance.connect_controller(app, Mock(), {**data, "jdnow_guest": "new"})
                new_connection = app["joydance_connections"][controller.serial]
                self.assertIsNone(new_connection._player_id)
                self.assertEqual(config.get_jdnow_player_id(controller.serial), original)
                new_connection._on_guest(fresh, "Fresh")
                connection._on_guest(original, "Stale callback")
                self.assertEqual(config.get_jdnow_player_id(controller.serial), fresh)
                await dance.connect_controller(app, Mock(), {**data, "jdnow_guest": original_key})
                restored = app["joydance_connections"][controller.serial]
                self.assertEqual(restored._player_id, original)
                restored._on_guest(original, "Original")
                await restored.on_state_changed({"state": PairingState.DISCONNECTED.value})
            self.assertEqual(ConfigHandler(config.current_cfg_path).get_jdnow_player_id(controller.serial),
                             original)
            info = app["controllers_info"][controller.serial]
            self.assertEqual([guest["name"] for guest in info["jdnow_guests"]], ["Guest 1 · Original", "Guest 2 · Fresh"])
            self.assertEqual(info["jdnow_guest"], original_key)
            self.assertNotIn("private-profile", json.dumps(info))
            restored.get_state = AsyncMock(return_value={"name": "Joy-Con", "color": "blue", "serial": controller.serial})
            with patch.object(dance, "update_controllers_list", new=AsyncMock()):
                listed = await dance.update_controllers_info(app)
            self.assertEqual(listed[0]["jdnow_guests"], info["jdnow_guests"])
            self.assertEqual(listed[0]["jdnow_guest"], original_key)
            self.assertNotIn("private-profile", json.dumps(listed))

    async def test_shared_guest_reservation_during_pairing_and_room_replacement(self):
        import dance

        with tempfile.TemporaryDirectory() as directory:
            config = ConfigHandler(str(Path(directory) / "config.cfg"))
            player_id = "private-profile-123456"
            config.save_jdnow_player_id("unplugged", player_id, "Borrowable")
            entry = guest_state(config, "left")["jdnow_guests"][0]
            self.assertEqual(entry["controllers"], ["unplugged"])
            controllers = {serial: game().controller for serial in ("left", "right")}
            for serial, controller in controllers.items():
                controller.serial = serial
            browser = Mock(closed=False, send_json=AsyncMock())
            app = {"config_handler": config, "controllers": controllers,
                   "controllers_info": {serial: {"serial": serial, "state": 0} for serial in controllers},
                   "joydance_connections": {}, "websockets": {browser}, "jdnow_guest_lock": asyncio.Lock(),
                   "joydance_connection_locks": {serial: asyncio.Lock() for serial in controllers}}

            async def start_pair(connection):
                await connection.on_state_changed({"connection_requested": True, "state": PairingState.PAIRING.value})
                return Mock()

            data = {"pairing_method": "jdnow", "pairing_code": "22965", "host_ip_addr": "",
                    "console_ip_addr": "", "jdnow_guest": entry["key"]}
            with patch.object(JustDanceGameNow, "start_pair", new=start_pair):
                results = await asyncio.gather(*(dance.connect_controller(app, browser, {**data, "controller_serial": serial})
                                                 for serial in controllers), return_exceptions=True)
                self.assertEqual(results.count(None), 1)
                self.assertTrue(any(isinstance(error, GameConnectionError) and "already in use" in str(error)
                                    for error in results))
                owner, connection = next(iter(app["joydance_connections"].items()))
                other = "right" if owner == "left" else "left"
                self.assertEqual(connection._player_id, player_id)
                self.assertEqual(app["controllers_info"][other]["jdnow_guests"], [])
                connection.is_connected = True
                await connection.on_state_changed({"state": PairingState.CONNECTED.value})
                self.assertEqual(app["controllers_info"][other]["jdnow_guests"], [])

                target_id = "private-target-profile-123456"
                config.save_jdnow_player_id("target", target_id, "Target")
                target_key = guest_state(config, "target")["jdnow_guest"]
                replacement = {**data, "jdnow_guest": target_key, "pairing_code": "12345"}
                close_started, finish_close = asyncio.Event(), asyncio.Event()

                async def close():
                    close_started.set()
                    await finish_close.wait()

                connection.ws = SimpleNamespace(state=State.OPEN, close=close)
                switching = asyncio.create_task(dance.connect_controller(app, browser, {
                    **replacement, "controller_serial": owner,
                }))
                try:
                    await asyncio.wait_for(close_started.wait(), 1)
                    with self.assertRaisesRegex(GameConnectionError, "already in use"):
                        await asyncio.wait_for(dance.connect_controller(app, browser, {
                            **data, "controller_serial": other,
                        }), 1)
                    await asyncio.wait_for(dance.connect_controller(app, browser, {
                        **replacement, "controller_serial": other, "pairing_code": "54321",
                    }), 1)
                finally:
                    finish_close.set()
                    result, = await asyncio.gather(switching, return_exceptions=True)

                self.assertIsInstance(result, GameConnectionError)
                self.assertIn("already in use", str(result))
                self.assertIsNone(app["joydance_connections"][owner].reserved_guest_id)
                self.assertEqual(app["joydance_connections"][other].reserved_guest_id, target_id)
                self.assertEqual(config.data["pairing_code"], "54321")
                self.assertEqual(app["controllers_info"][owner]["pairing_code"], "22965")
                self.assertEqual(app["controllers_info"][other]["jdnow_guests"], [entry])
                await dance.connect_controller(app, browser, {**data, "controller_serial": other})
                self.assertEqual(app["joydance_connections"][other]._player_id, player_id)
                await dance.disconnect_controller(app, browser, {"controller_serial": other})
                self.assertIn(entry, app["controllers_info"][owner]["jdnow_guests"])
            self.assertNotIn(player_id, str(browser.send_json.await_args_list))

    async def test_join_waits_for_sync_and_requires_player_acknowledgement(self):
        instance = game()
        instance.catalog = {"TestSong": {}}
        await instance.send_hello()
        instance.ws.send.assert_not_awaited()
        await instance.on_message(json.dumps({"func": "clientSyncCompleted", "serverTime": 1000000, "latency": 100}))
        sent = json.loads(instance.ws.send.await_args.args[0])
        self.assertEqual(sent, {"func": "joinRoom", "roomID": 22965, "dancercardID": "public-profile-123456"})
        self.assertFalse(instance.is_connected)
        await instance.on_message(json.dumps({"func": "playerJoined", "isController": False, "numberOfPlayersInRoom": 2,
                                              "song": "TestSong"}))
        self.assertTrue(instance.is_connected)
        self.assertFalse(instance._is_main_player)
        instance.controller.set_player_led.assert_awaited_with(2, flash=True)
        self.assertFalse(instance.should_start_accelerometer)
        self.assertEqual(instance._current_song, "TestSong")
        await instance.on_message(json.dumps({"func": "playlistSelected", "category": "TestPlaylist"}))
        self.assertEqual(instance._category, "TestPlaylist")

    async def test_room_controller_transfer_updates_main_controls(self):
        instance = self.navigation_game()
        instance.is_connected = True
        await instance.on_message('{"func":"controllerChanged","isController":true,'
                                  '"isDeviceAsController":true,"newController":{}}')
        self.assertTrue(instance._state["is_main_player"])
        self.assertTrue(instance._state["song_search_enabled"])
        self.assertTrue(instance._device_as_controller)
        await instance.on_message('{"func":"controllerChanged","isController":false,"newController":{}}')
        self.assertFalse(instance._state["is_main_player"])
        self.assertFalse(instance._state["song_search_enabled"])
        self.assertFalse(instance._device_as_controller)

    async def test_accept_selects_the_focused_song_without_purchasing_it(self):
        instance = self.navigation_game()
        instance.is_connected = instance._is_main_player = True
        await instance.select_song("IdB")
        instance.controller.get_latest_command.return_value = Command.ACCEPT
        sent = asyncio.Event()
        instance.ws.send.side_effect = lambda message: sent.set()
        task = asyncio.create_task(instance.send_command())
        try:
            await asyncio.wait_for(sent.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(json.loads(instance.ws.send.await_args.args[0]), {"func": "selectSong", "song": "IdB"})
        self.assertFalse(instance._purchase_pending)

    async def test_server_join_error_does_not_report_connected_or_log_secrets(self):
        instance = game()
        with self.assertRaises(GameConnectionError) as failure:
            await instance.on_message(json.dumps({"func": "joinRoom", "error": "Error joining room 22965 - Error: 500 - Internal Server Error"}))
        self.assertIn("500", str(failure.exception))
        self.assertEqual(failure.exception.state, PairingState.ERROR_CONSOLE_CONNECTION)
        self.assertFalse(instance.is_connected)

    async def test_perfect_run_and_gold_reach_the_original_score_ceiling(self):
        instance = game()
        instance.is_connected = instance.should_start_accelerometer = True
        instance._moves = [{"name": "synthetic", "time": i * 100, "duration": 100, "goldMove": i == 6} for i in range(7)]
        instance._classifiers = {"synthetic": object()}
        instance._max_raw = 14
        instance._song_start, instance._song_number = 1000, 7
        instance._motion = deque((t, 0, -1, 0) for t in range(995, 1801, 5))
        instance.server_time = Mock(return_value=2000)
        with patch("joydance.games.jdnow.scoring._score_before_periodicity", return_value=((1.0, 2.0, 1.0, 3), None)):
            await instance._score_pending()
        messages = [json.loads(call.args[0]) for call in instance.ws.send.await_args_list]
        self.assertEqual(len(messages), 7)
        self.assertEqual([m["moveNr"] for m in messages], list(range(7)))
        self.assertEqual(messages[-1]["rating"], "yeah")
        self.assertEqual(messages[-1]["func"], "playerScoredPhone")
        self.assertEqual(messages[-1]["totalScore"], 13333)
        self.assertEqual(instance._phase, "results")
        self.assertFalse(instance.should_start_accelerometer)
        instance.controller.set_accel_collection.assert_called_with(False)
        await instance._score_pending()
        await instance.on_message('{"func":"songEnd"}')
        self.assertEqual(instance.ws.send.await_count, 7)
        self.assertEqual(instance._phase, "results")
        self.assertFalse(instance.should_start_accelerometer)

    async def test_results_continue_and_browser_navigation_work_without_song_end(self):
        instance = game()
        instance.is_connected = instance._is_main_player = instance.should_start_accelerometer = True
        instance._phase = "dancing"
        instance._current_song = "TestSong"
        instance.catalog = {"TestSong": {}, "NextSong": {}}
        instance._song_order = ["TestSong", "NextSong"]
        instance._moves = [{"name": "move", "time": 0, "duration": 100}]
        instance._classifiers = {"move": object()}
        instance._max_raw = 10
        instance._motion = deque((t, 0, -1, 0) for t in range(0, 106, 5))
        instance.server_time = Mock(return_value=1000)
        with patch("joydance.games.jdnow.scoring._score_before_periodicity", return_value=((1.0, 2.0, 1.0, 3), None)):
            await instance._score_pending()
        self.assertTrue(instance.is_connected)
        self.assertEqual(instance._phase, "results")
        instance.ws.send.reset_mock()
        instance.controller.get_latest_command.return_value = Command.ACCEPT
        with patch("joydance.games.jdnow.game.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await instance.send_command()
        self.assertEqual([json.loads(call.args[0]) for call in instance.ws.send.await_args_list], [
            {"func": "navContinue", "song": "TestSong", "category": ""},
            {"func": "songSelection"},
        ])
        await instance.on_message('{"func":"navRest","song":"TestSong","category":"","sender":"web"}')
        self.assertEqual(instance._phase, "browsing")
        self.assertTrue(await instance._navigate(Command.RIGHT))
        self.assertEqual(instance._current_song, "NextSong")

    async def test_play_again_reopens_coach_selection_without_purchasing(self):
        instance = game()
        instance.is_connected = instance._is_main_player = True
        instance._phase = "results"
        instance._current_song = "TestSong"
        instance.catalog = {"TestSong": {}}
        instance.controller.get_latest_command.return_value = Command.VIDEO_RESTART
        with patch("joydance.games.jdnow.game.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await instance.send_command()
        self.assertIn(Command.VIDEO_RESTART, instance.controller.get_latest_command.call_args.kwargs["commands_to_check"])
        instance.ws.send.assert_awaited_once()
        self.assertEqual(json.loads(instance.ws.send.await_args.args[0]), {"func": "selectSong", "song": "TestSong"})
        instance._start_loading = Mock()
        await instance.on_message('{"func":"songSelected","song":"TestSong","sender":"app"}')
        self.assertEqual(instance._phase, "coach")
        instance._start_loading.assert_called_once_with("TestSong", 0)
        self.assertFalse(instance._purchase_pending)

    async def test_launching_ignores_repeated_accept_during_the_tutorial(self):
        instance = game()
        instance.is_connected = instance._is_main_player = instance._purchase_pending = True
        instance._phase = "coach"
        instance._play_request = Mock()
        await instance.on_message('{"func":"virtualPayPerPlayPurchase","error":true}')
        self.assertIn("could not start this song", instance._state["additional_message"])
        instance._purchase_pending = True
        instance._play_request = Mock()
        await instance.on_message('{"func":"virtualPayPerPlayPurchase","token":"test-launch-token"}')
        self.assertEqual(instance._phase, "launching")
        self.assertIsNone(instance._state["additional_message"])
        instance.controller.get_latest_command.return_value = Command.ACCEPT
        task = asyncio.create_task(instance.send_command())
        try:
            await asyncio.sleep(.04)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(instance.ws.send.await_count, 1)
        self.assertEqual(json.loads(instance.ws.send.await_args.args[0])["func"], "songLaunch")

    async def test_video_corrections_accept_signed_and_fractional_browser_timing(self):
        instance = game()
        instance.is_connected = True
        instance._initial_song_start = 100000
        for delay in (1200, -45, -45.75, 0):
            await instance.on_message(json.dumps({"func": "videoDelay", "delay": delay}))
            self.assertEqual(instance._song_start, 100000 + int(delay))
            self.assertTrue(instance.is_connected)
        for delay in (True, "10", None, 2**31, -(2**31)-1):
            with self.assertRaises(ValueError):
                await instance.on_message(json.dumps({"func": "videoDelay", "delay": delay}))

    async def test_rejoining_a_song_restores_score_and_skips_elapsed_moves(self):
        instance = game()
        instance.catalog = {"TestSong": {}}
        moves = [{"name": "move", "time": i * 1000, "duration": 100} for i in range(7)]
        instance._load_song = AsyncMock(return_value=(moves, {"move": object()}))
        instance.server_time = Mock(return_value=4250)
        await instance._start_song({"songName": "TestSong", "songNr": 1, "songStart": 1000,
                                    "videoDelay": -25, "scoreWhenLeftRoom": 4100})
        self.assertEqual(instance._move_index, 4)
        self.assertEqual(instance._score, 4100)
        self.assertEqual(instance._song_start, 975)
        instance._motion = deque((t, 0, -1, 0) for t in range(4970, 5081, 5))
        instance.server_time.return_value = 5160
        await instance._score_pending()
        instance.ws.send.assert_not_awaited()
        instance._motion.extend((t, 0, -1, 0) for t in range(5085, 5156, 5))
        with patch("joydance.games.jdnow.scoring._score_before_periodicity", return_value=((1.0, 2.0, 1.0, 3), None)):
            await instance._score_pending()
        sent = json.loads(instance.ws.send.await_args.args[0])
        self.assertEqual(sent["moveNr"], 4)
        self.assertGreater(sent["totalScore"], 4100)
        self.assertEqual(instance._missing_motion_count, 0)
        instance._stop_scoring()

    async def test_star_and_successful_gold_feedback_use_existing_rumble_patterns(self):
        instance = game()
        instance.controller.rumble_enabled = True
        instance.controller.handle_rumble_on_sound_index = AsyncMock()
        instance.is_connected = instance.should_start_accelerometer = True
        instance._moves = [{"name": "move", "time": 0, "duration": 100, "goldMove": True}]
        instance._classifiers = {"move": object()}
        instance._max_raw = 5
        instance._motion = deque((t, 0, -1, 0) for t in range(0, 106, 5))
        instance.server_time = Mock(return_value=1000)
        with patch("joydance.games.jdnow.scoring._score_before_periodicity", return_value=((1.0, 2.0, 1.0, 3), None)):
            await instance._score_pending()
        await instance._rumble_task
        self.assertEqual([call.args[0] for call in instance.controller.handle_rumble_on_sound_index.await_args_list],
                         [7, 1, 2, 3, 4, 5, 26, 40])
        await instance._score_pending()
        self.assertEqual(instance.controller.handle_rumble_on_sound_index.await_count, 8)
        instance.controller.rumble_enabled = False
        for effect in (7, 26, 40):
            await instance._feedback(effect)
        self.assertEqual(instance.controller.handle_rumble_on_sound_index.await_count, 8)

    async def test_superstar_and_megastar_feedback_only_fire_when_crossing_their_thresholds(self):
        for previous, gain, expected in (
                (10998, 1, []), (10999, 1, [26]), (11000, 1, []),
                (11998, 1, []), (11999, 1, [40]), (12000, 1, []),
                (10999, 1001, [26, 40]), (11500, 500, [40]), (12100, 100, [])):
            with self.subTest(previous=previous, gain=gain):
                instance = game()
                instance.is_connected = instance.should_start_accelerometer = True
                instance._score = instance._score_offset = previous
                instance._feedback = AsyncMock()
                instance._moves = [{"name": "move", "time": t, "duration": 100} for t in (0, 1000)]
                instance._classifiers = {"move": object()}
                instance._max_raw = 13333 / gain
                instance._motion = deque((t, 0, -1, 0) for t in range(0, 1106, 5))
                instance.server_time = Mock(return_value=2000)
                def score_window(classifier, samples, start, duration):
                    result = (1.0, 2.0, 1.0, 3) if start < 500 else (0.0, 0.1, 0.0, 0)
                    return result, None
                with patch("joydance.games.jdnow.scoring._score_before_periodicity", side_effect=score_window):
                    await instance._score_pending()
                self.assertEqual(instance._score, previous + gain)
                self.assertEqual([call.args for call in instance._feedback.await_args_list],
                                 [tuple(expected)] if expected else [])

    async def test_failed_gold_has_no_rumble_and_motion_gaps_are_summarized(self):
        instance = game()
        instance.is_connected = instance.should_start_accelerometer = True
        instance._feedback = AsyncMock()
        instance._moves = [{"name": "move", "time": i * 100, "duration": 100, "goldMove": True} for i in range(7)]
        instance._classifiers = {"move": object()}
        instance._max_raw = 50
        instance._motion = deque((t, 0, -1, 0) for t in range(0, 106, 5))
        instance.server_time = Mock(return_value=2000)
        with patch("joydance.games.jdnow.scoring._score_before_periodicity", return_value=((0.0, 0.1, 0.0, 0), None)):
            with self.assertLogs("joydance.games.jdnow.game", level="DEBUG") as logs:
                await instance._score_pending()
        instance._feedback.assert_not_awaited()
        self.assertEqual(instance._missing_motion_count, 6)
        self.assertEqual(sum("WARNING:" in line for line in logs.output), 1)
        self.assertTrue(any("missing_motion=6" in line for line in logs.output))

    async def test_scoring_result_cannot_escape_after_stop(self):
        instance = game()
        instance.should_start_accelerometer = instance.is_connected = True
        instance._moves = [{"name": "move", "time": 0, "duration": 100}]
        instance._classifiers = {"move": object()}
        instance._max_raw = 10
        instance._motion = deque((t, 0, -1, 0) for t in range(0, 106, 5))
        instance.server_time = Mock(return_value=1000)
        started, finish = asyncio.Event(), asyncio.Event()
        async def score(*args):
            started.set()
            await finish.wait()
            return (1.0, 2.0, 1.0, 3), 0.4, 10
        with patch("joydance.games.jdnow.game.asyncio.to_thread", side_effect=score):
            task = asyncio.create_task(instance._score_pending())
            await started.wait()
            instance._stop_scoring()
            finish.set()
            await task
        instance.ws.send.assert_not_awaited()
        self.assertEqual(instance._score_pointer, 0.5)

    async def test_video_delay_retries_an_in_flight_move_with_corrected_timing(self):
        instance = game()
        instance.should_start_accelerometer = instance.is_connected = True
        instance._song_start = instance._initial_song_start = 1000
        instance._moves = [{"name": "move", "time": 0, "duration": 100}]
        instance._classifiers = {"move": object()}
        instance._max_raw = 10
        instance._motion = deque([(1000, 0, -1, 0), (1900, 0, 1, 0)])
        instance.server_time = Mock(return_value=1600)
        started, finish = asyncio.Event(), asyncio.Event()

        async def score(classifier, samples, start, duration, pointer):
            if start == 1000:
                started.set()
                await finish.wait()
                return (1.0, 2.0, 1.0, 3), 0.4, 10
            return (0.5, 2.0, 1.0, 2), 0.6, 0

        worker = AsyncMock(side_effect=score)
        instance._scoring_workers = SimpleNamespace(score=worker)
        scoring = asyncio.create_task(instance._score_pending())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await instance.on_message('{"func":"videoDelay","delay":900}')
        finally:
            finish.set()
            await asyncio.wait_for(scoring, 1)

        instance.ws.send.assert_not_awaited()
        self.assertEqual((instance._move_index, instance._score, instance._score_pointer), (0, 0, 0.5))
        await instance._score_pending()
        self.assertEqual(worker.await_count, 1)

        instance.server_time.return_value = 2600
        await instance._score_pending()
        self.assertEqual(worker.await_count, 2)
        self.assertEqual(worker.await_args.args[1:], ([(1900, 0, 1, 0)], 1900, 100, 0.5))
        message = json.loads(instance.ws.send.await_args.args[0])
        self.assertEqual((message["func"], message["moveNr"], message["totalScore"]), ("playerScoredPhone", 0, 667))
        instance.ws.send.assert_awaited_once()
        self.assertEqual(instance._score_pointer, 0.6)

    async def test_motion_batch_overlap_does_not_abort_scoring(self):
        instance = game()
        instance.is_connected = instance.should_start_accelerometer = True
        instance._phase = "dancing"
        instance._moves = [{"name": "move", "time": 900, "duration": 140}]
        instance._classifiers = {"move": read_classifier(classifier_bytes())}
        instance._max_raw = 10
        instance.disconnect = AsyncMock()
        instance.controller.is_connected.side_effect = [True, True, True, False]
        instance.controller.get_accel_events.side_effect = [[(0, -1, 0)] * 24, [(0, -1, 0)] * 30, []]
        instance.server_time = Mock(side_effect=[1000, 1000, 1040, 1040, 1150, 1150])

        await instance.tick()

        messages = [json.loads(call.args[0])["func"] for call in instance.ws.send.await_args_list]
        self.assertEqual(messages, ["playerScoredPhone"])

    async def test_tick_and_song_end_cannot_score_the_same_move_twice(self):
        instance = game()
        instance.should_start_accelerometer = True
        instance._moves = [{"name": "move", "time": 0, "duration": 100}]
        instance._classifiers = {"move": object()}
        instance._max_raw = 10
        instance._motion = deque((t, 0, -1, 0) for t in range(0, 106, 5))
        instance.server_time = Mock(return_value=1000)
        with patch("joydance.games.jdnow.scoring._score_before_periodicity", return_value=((1.0, 2.0, 1.0, 3), None)) as scorer:
            await asyncio.gather(instance._score_pending(), instance._score_pending())
        scorer.assert_called_once()
        messages = [json.loads(call.args[0])["func"] for call in instance.ws.send.await_args_list]
        self.assertEqual(messages, ["playerScoredPhone"])

    async def test_six_controllers_share_song_preparation_without_changing_scoring(self):
        song = {"base": "https://cdn.justdancenow.com/TestSong-v1"}
        moves = [{"name": "move", "time": 0, "duration": 841}]

        async def download(song, path):
            return classifier_bytes() if path.endswith(".msm") else json.dumps(moves).encode()

        baseline = game()
        baseline.catalog = {"TestSong": song}
        baseline.api.asset = AsyncMock(side_effect=download)
        expected_moves, expected_classifiers = await baseline._load_song("TestSong", 0)
        motion = [(t, math.sin(t / 100), -1, math.cos(t / 75)) for t in range(0, 960, 5)]
        expected_score = score_adaptive_move(expected_classifiers["move"], motion, 0, 841, .5)
        assets = JDNowAssets()
        self.addAsyncCleanup(assets.close)
        instances = [game() for _ in range(6)]
        for instance in instances:
            instance._assets = assets
            instance.catalog = {"TestSong": song}
        with patch.object(JDNowAPI, "asset", new=AsyncMock(side_effect=download)) as fetch, \
                patch("joydance.games.jdnow.game.read_classifier", wraps=read_classifier) as parse:
            results = await asyncio.gather(*(instance._load_song("TestSong", 0) for instance in instances))
            self.assertEqual(fetch.await_count, 2)
            parse.assert_called_once()
            # Another coach has a distinct schedule but shares the same classifier file.
            await instances[0]._load_song("TestSong", 1)
            self.assertEqual(fetch.await_count, 3)
            parse.assert_called_once()
        for actual_moves, classifiers in results:
            self.assertEqual(actual_moves, expected_moves)
            self.assertEqual(classifiers, expected_classifiers)
            self.assertIs(classifiers["move"], results[0][1]["move"])
            self.assertEqual(score_adaptive_move(classifiers["move"], motion, 0, 841, .5), expected_score)

    async def test_public_move_schedules_accept_integer_gold_flags(self):
        instance = game()
        instance.catalog = {"TestSong": {"base": "https://cdn.justdancenow.com/TestSong"}}
        for flag in (0, 1, False, True):
            moves = [{"name": "move", "time": 0, "duration": 100, "goldMove": flag}]
            instance.api.asset = AsyncMock(side_effect=[json.dumps(moves).encode(), classifier_bytes()])
            actual, classifiers = await instance._load_song("TestSong", 0)
            self.assertEqual(actual, moves)
            self.assertIn("move", classifiers)
        for flag in (2, "1", 1.0):
            instance.api.asset = AsyncMock(return_value=json.dumps([
                {"name": "move", "time": 0, "duration": 100, "goldMove": flag}]).encode())
            with self.assertRaises(ValueError):
                await instance._load_song("TestSong", 0)

    async def test_back_or_a_coach_change_cancels_a_play_request_waiting_for_preload(self):
        for change in ("browser back", "Joy-Con back", "coach"):
            with self.subTest(change=change):
                instance = game()
                instance.is_connected = instance._is_main_player = True
                instance.catalog = {"TestSong": {"coaches": 2}}
                ready = asyncio.Event()

                async def load(*args):
                    await ready.wait()
                    return [], {}

                instance._load_song = load
                await instance.on_message('{"func":"songSelected","song":"TestSong"}')
                commands = deque([Command.ACCEPT, Command.BACK if change == "Joy-Con back" else None])
                instance.controller.get_latest_command.side_effect = lambda **kwargs: commands.popleft()

                async def advance(_):
                    if not commands:
                        raise asyncio.CancelledError
                    if change == "browser back":
                        await instance.on_message('{"func":"songDeselected"}')
                    elif change == "coach":
                        await instance.on_message('{"func":"coachSelected","player":"public-profile-123456","coach":1}')
                    ready.set()
                    await instance._load_task

                with patch("joydance.games.jdnow.game.asyncio.sleep", side_effect=advance):
                    with self.assertRaises(asyncio.CancelledError):
                        await instance.send_command()
                commands_sent = [json.loads(call.args[0])["func"] for call in instance.ws.send.await_args_list]
                self.assertEqual(commands_sent, ["deselectSong"] if change == "Joy-Con back" else [])
                self.assertFalse(instance._purchase_pending)

    async def test_late_purchase_reply_cannot_launch_a_song_after_backing_out(self):
        instance = game()
        instance.is_connected = instance._is_main_player = True
        instance.catalog = {"TestSong": {}}
        instance._load_song = AsyncMock(return_value=([], {}))
        await instance.on_message('{"func":"songSelected","song":"TestSong"}')
        await instance._load_task
        instance._play_request = instance._load_task
        await instance._submit_ready_play_request()
        await instance.on_message('{"func":"songDeselected"}')
        await instance.on_message('{"func":"songSelected","song":"TestSong"}')
        self.assertTrue(instance._purchase_pending)
        await instance.on_message('{"func":"virtualPayPerPlayPurchase","token":"late-token"}')
        instance.ws.send.assert_awaited_once()
        self.assertEqual(instance._phase, "coach")
        self.assertFalse(instance._purchase_pending)
        instance._play_request = instance._load_task
        await instance._submit_ready_play_request()
        await instance.on_message('{"func":"virtualPayPerPlayPurchase","token":"current-token"}')
        self.assertEqual(json.loads(instance.ws.send.await_args.args[0]), {"func": "songLaunch", "token": "current-token"})

    async def test_song_preload_reuses_running_and_successful_downloads(self):
        instance = game()
        ready = asyncio.Event()
        result = ([{"name": "move", "time": 0, "duration": 100}], {"move": object()})
        async def load(*args):
            await ready.wait()
            return result
        instance._load_song = AsyncMock(side_effect=load)
        instance._start_loading("TestSong", 0)
        first = instance._load_task
        instance._start_loading("TestSong", 0)
        self.assertIs(instance._load_task, first)
        ready.set()
        self.assertEqual(await first, result)
        instance._start_loading("TestSong", 0)
        self.assertIs(instance._load_task, first)
        instance._load_song.assert_awaited_once_with("TestSong", 0)

    async def test_song_preload_retries_after_failure_or_cancellation_on_reselection(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                instance = game()
                instance.catalog = {"TestSong": {"name": "Test song", "coaches": 1}}
                result = ([{"name": "move", "time": 0, "duration": 100}], {"move": object()})
                started = asyncio.Event()
                async def load(*args):
                    started.set()
                    if cancelled:
                        await asyncio.Event().wait()
                    raise GameConnectionError("Temporary song data failure.")
                instance._load_song = AsyncMock(side_effect=load)
                await instance.on_message('{"func":"songSelected","song":"TestSong"}')
                first = instance._load_task
                await started.wait()
                if cancelled:
                    first.cancel()
                with self.assertRaises(asyncio.CancelledError if cancelled else GameConnectionError):
                    await first
                instance._load_song.side_effect = None
                instance._load_song.return_value = result
                await instance.on_message('{"func":"songDeselected"}')
                await instance.on_message('{"func":"songSelected","song":"TestSong"}')
                self.assertIsNot(instance._load_task, first)
                self.assertEqual(await instance._load_task, result)
                self.assertEqual(instance._load_song.await_count, 2)
                self.assertFalse(instance._purchase_pending)

    async def test_superseded_song_downloads_are_cancelled_on_disconnect(self):
        instance = game()
        async def load(*args):
            await asyncio.Event().wait()
        instance._load_song = load
        instance._start_loading("first", 0)
        first = instance._load_task
        await asyncio.sleep(0)
        instance._start_loading("second", 0)
        second = instance._load_task
        await instance.disconnect(should_reconnect=False)
        self.assertTrue(first.cancelled())
        self.assertTrue(second.cancelled())
        self.assertFalse(instance._load_tasks)
