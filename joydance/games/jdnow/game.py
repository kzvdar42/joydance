"""Just Dance Now browser rooms with local Joy-Con motion scoring."""
import asyncio
from collections import deque
import json
import logging
import math
import re
import time

import aiohttp

from joydance.constants import Command, FRAME_DURATION, JD_SERVICE_TIMEOUT, PairingState
from joydance.utils import GameConnectionError
from .api import ASSET_NAME, JDNowAPI, parse_json
from .connection import JDNowConnection
from .scoring import MOTION_EDGE_TOLERANCE_MS, SCORING_WINDOW_MS, float32, read_classifier, score_adaptive_move
from ..justdance_abstract import JustDanceGameAbstract


logger = logging.getLogger(__name__)
RATINGS = ("bad", "ok", "good", "perfect", "yeah")
DIRECTIONS = {Command.UP, Command.DOWN, Command.LEFT, Command.RIGHT}
EMPTY_SONG_SEARCH = {"song_search_results": [], "song_search_total": None, "song_search_feedback": None}
CONFIRMATION_SECONDS = 4


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid JDN numeric field")
    return value


class JustDanceGameNow(JustDanceGameAbstract):
    def __init__(self, controller, *args, player_id=None, on_guest=None, scoring_workers=None, **kwargs):
        self._confirmation_tasks = {}
        super().__init__(controller, *args, **kwargs)
        self._player_id = player_id
        self._on_guest = on_guest
        self.api = None
        self.catalog = {}
        self._song_order, self._playlists = [], {}
        self._playlist_order = []
        self._load_tasks = set()
        self._score_lock = asyncio.Lock()
        self._score_pointer = 0.5
        self._scoring_workers = scoring_workers

    def _reset_input_state(self):
        super()._reset_input_state()
        self._generation = getattr(self, "_generation", 0) + 1
        task = getattr(self, "_load_task", None)
        if task is not None:
            task.cancel()
        for task in getattr(self, "_load_tasks", ()):
            task.cancel()
        self._load_task = self._load_key = None
        self._join_sent = False
        self._phase = "browsing"
        self._is_main_player = False
        self._device_as_controller = False
        self._current_song = ""
        self._category = ""
        self._tab = None
        self._playlist_picker = False
        self._pending_navigation = deque()
        self._pending_tabs = deque()
        self._coach = 0
        self._play_request = None
        self._purchase_pending = False
        self._moves, self._classifiers = [], {}
        self._motion = deque()
        self._song_start = self._initial_song_start = 0
        self._server_anchor = time.time() * 1000
        self._monotonic_anchor = time.monotonic()
        self._last_received = time.monotonic()
        self._last_sample = None
        self._stalled = False
        self._move_index = self._song_number = self._gauge = self._score = 0
        self._score_offset = self._missing_motion_count = 0
        self._total_raw = self._max_raw = 0.0
        for task in self._confirmation_tasks.values():
            task.cancel()
        self._confirmation_tasks.clear()
        self._state["additional_message"] = None
        if not (self._state.get("player_rename_feedback") or {}).get("error"):
            self._state["player_rename_feedback"] = None

    def server_time(self):
        return self._server_anchor + (time.monotonic() - self._monotonic_anchor) * 1000

    @property
    def reserved_guest_id(self):
        # Pairing state remains active until the room socket has closed.
        state = self._state.get("state", PairingState.IDLE.value)
        if (self.is_connected or self._state.get("connection_requested")
                or PairingState.GETTING_TOKEN.value <= state <= PairingState.CONNECTED.value):
            return self._player_id
        return None

    @property
    def _can_browse_songs(self):
        return (self.is_connected and self._is_main_player and self._phase == "browsing"
                and not self.should_start_accelerometer)

    @property
    def _navigation_busy(self):
        return len(self._pending_navigation) >= 32 or len(self._pending_tabs) >= 32

    async def on_state_changed(self, state):
        search_enabled = self._can_browse_songs
        if search_enabled:
            self.available_shortcuts = {Command.GOTO_SONGSTAB if self._tab == "playlist" else Command.GOTO_PLAYLISTSTAB}
        elif self.is_connected and self._is_main_player and self._phase == "results":
            self.available_shortcuts = {Command.VIDEO_RESTART}
        else:
            self.available_shortcuts = set()
        self._sync_controller_shortcuts()
        await super().on_state_changed({**(EMPTY_SONG_SEARCH if not search_enabled else {}), **state,
                                       "supports_song_search": True, "supports_player_rename": True,
                                       "song_search_enabled": search_enabled})

    async def search_songs(self, query):
        if isinstance(query, str) and not query.strip():
            await self.on_state_changed(EMPTY_SONG_SEARCH)
            return
        if not self._can_browse_songs:
            raise ValueError("Return to song selection with the main controller to search.")
        if not isinstance(query, str) or len(query.strip()) > 100:
            raise ValueError("Enter a song title or artist (up to 100 characters).")
        query = query.strip().casefold()
        matches = [song for song in self._song_order if query in
                   f"{self.catalog[song].get('name', song)} {self.catalog[song].get('artist', '')}".casefold()]
        results = [{"id": song, "name": self.catalog[song].get("name", song),
                    "artist": self.catalog[song].get("artist", "")} for song in matches[:20]]
        await self.on_state_changed({"song_search_results": results, "song_search_total": len(matches),
                                     "song_search_feedback": None})

    async def select_song(self, song):
        """Jump to a song in All Songs while staying in the browsing screen."""
        if not self._can_browse_songs:
            raise ValueError("Return to song selection with the main controller to jump to a song.")
        if not isinstance(song, str) or song not in self._song_order:
            raise ValueError("This song is unavailable in the current catalog.")
        if self._navigation_busy:
            raise ValueError("Navigation is still updating. Try again shortly.")
        if self._tab != "songlist":
            await self._send_tab("songlist")
        self._tab = "songlist"
        self._playlist_picker = False
        await self._send_navigation(song, "")
        name = self.catalog[song].get("name", song)
        await self._show_action_feedback("song_search_feedback", {"text": f"Jumped to {name} in All Songs. Press A to select."})

    async def rename_player(self, name):
        if not self.is_connected:
            raise ValueError("Connect to a Dance Room before renaming the player.")
        api, room = self.api, self.ws
        relays = await api.rename_player(name)
        name = api.profile["pName"]
        warnings = []
        if self._on_guest is not None:
            try:
                self._on_guest(api.player_id, name)
            except OSError as error:
                warnings.append(str(error))
                logger.warning("%s: %s", self.controller.serial, error)

        room_updated = room is self.ws and self._websocket_is_open(room)
        try:
            for relay in relays:
                if room is not self.ws or not await self.send_message(relay["func"], {"player": relay["player"]}):
                    room_updated = False
                    break
        except (GameConnectionError, OSError, ValueError) as error:
            room_updated = False
            logger.warning("%s: JDN rename room update failed (%s)", self.controller.serial, type(error).__name__)
        if not room_updated:
            warnings.append("Reconnect to update the Dance Room.")
        feedback = ({"text": " ".join(["Player name saved.", *warnings]), "error": True}
                    if warnings else {"text": "Player name updated."})
        await self._show_action_feedback("player_rename_feedback", feedback, player_name=name)

    async def _show_action_feedback(self, field, feedback, **state):
        task = self._confirmation_tasks.pop(field, None)
        if task is not None:
            task.cancel()
        if not feedback.get("error"):
            self._confirmation_tasks[field] = asyncio.create_task(self._expire_confirmation(field, feedback))
        await self.on_state_changed({**state, field: feedback})

    async def _expire_confirmation(self, field, feedback):
        await asyncio.sleep(CONFIRMATION_SECONDS)
        if self._state.get(field) is feedback:
            await self.on_state_changed({field: None})

    async def _update_player_color(self, color):
        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = [int(color[i:i + 2], 16) for i in (1, 3, 5)] + [255]
        elif isinstance(color, str) and len(color) <= 64:
            try:
                color = json.loads(color)
            except ValueError:
                return
        if (isinstance(color, list) and len(color) == 4
                and all(type(value) is int and 0 <= value <= 255 for value in color)):
            await self.on_state_changed({"player_color": color[:3] + [color[3] / 255]})

    async def pair(self):
        self.should_reconnect = True
        try:
            await self.on_state_changed({"state": PairingState.GETTING_TOKEN.value, "error_details": None})
            async with aiohttp.ClientSession() as session:
                self.api = JDNowAPI(session, self._player_id, self._on_guest)
                hello = await self.api.login()
                self._player_id = self.api.player_id
                self._server_anchor = integer(hello["serverTime"], 0, 2**53)
                self._monotonic_anchor = time.monotonic()
                await self.on_state_changed({"state": PairingState.PAIRING.value})
                endpoint = await self.api.room_endpoint(self.pairing_code)
                self.catalog, categories = await asyncio.gather(self.api.songs(), self.api.categories())
                self._configure_navigation(categories)
                await self.on_state_changed({"state": PairingState.CONNECTING.value})
                async with asyncio.timeout(JD_SERVICE_TIMEOUT):
                    reader, writer = await asyncio.open_connection(*endpoint)
                connection = JDNowConnection(reader, writer)
                self.ws = connection
                self._last_received = time.monotonic()
                try:
                    await self.run_connection_tasks()
                finally:
                    await connection.close()
        except (KeyError, TypeError, ValueError, RecursionError):
            logger.debug("%s: JDN validation failed phase=%s song=%s move=%s",
                         self.controller.serial, self._phase, self._current_song, self._move_index, exc_info=True)
            await self._handle_connection_error(GameConnectionError(
                "Unexpected Just Dance Now service response.", should_reconnect=False))
        except Exception as error:
            await self._handle_connection_error(error)

    async def send_hello(self):
        # The room server initiates clock synchronization before accepting joinRoom.
        pass

    def _configure_navigation(self, categories):
        uvs = {song for entry in categories if entry.get("trackingId") == "UVS" for song in entry["songs"]}
        lge = {song for entry in categories if entry.get("trackingId") == "LGE" for song in entry["songs"]}
        hidden = set() if self.api.profile.get("isUVSPlayer") else uvs | lge
        def title(song):
            name = self.catalog[song].get("name", song)
            name = name if isinstance(name, str) else song
            return name.lower(), name
        self._song_order = sorted((song for song in self.catalog if song not in hidden), key=title)
        # The web player preserves playlist order and removes UVS tracks from regular playlists.
        self._playlists = {
            entry["id"]: list(dict.fromkeys(song for song in entry["songs"] if song in self.catalog and song not in uvs))
            for entry in categories if entry.get("trackingId") not in {"UVS", "LGE"}
        }
        if self.api.profile.get("isUVSPlayer"):
            self._playlists.update({entry["id"]: [song for song in entry["songs"] if song in self.catalog]
                                    for entry in categories if entry.get("trackingId") == "UVS"})
        self._playlist_order = [entry["id"] for entry in categories if entry["id"] in self._playlists
            and entry.get("trackingId") != "FTUE" and not any(entry.get(flag) for flag in
                ("isShowOnlyForTvos", "isHiddenForAll", "isSongPack"))]
        self._set_favorites(self.api.profile.get("favorites", []))

    def _set_favorites(self, songs):
        if not isinstance(songs, list) or len(songs) > 10000 or any(not isinstance(song, str) for song in songs):
            raise ValueError("Invalid JDN favorites")
        self._playlists["FAVORITES"] = list(dict.fromkeys(song for song in songs if song in self.catalog))
        self._playlist_order = [key for key in self._playlist_order if key != "FAVORITES"]
        if self._playlists["FAVORITES"]:
            self._playlist_order.insert(0, "FAVORITES")

    async def _switch_tab(self, tab):
        if self._navigation_busy:
            return
        self._tab, self._category, self._current_song = tab, "", ""
        self._playlist_picker = tab == "playlist"
        await self._send_tab(tab)
        await self._navigate(Command.RIGHT)
        await self.on_state_changed({})

    async def _send_tab(self, tab):
        self._pending_tabs.append(tab)
        await self.send_message("tabRest", {"tabName": tab})

    async def _send_navigation(self, song, category):
        self._current_song, self._category = song or "", category
        self._pending_navigation.append((song, category))
        if song is None:
            await self.send_message("playlistSelected", {"category": category})
        else:
            await self.send_message("navRest", {"song": song, "category": category})

    async def _navigate_playlists(self, direction):
        playlists = [key for key in self._playlist_order if self._playlists.get(key)]
        if not playlists or self._navigation_busy:
            return None
        step = 1 if direction in {Command.RIGHT, Command.DOWN} else -1
        index = playlists.index(self._category) if self._category in playlists else -step
        index = min(len(playlists) - 1, max(0, index + step))
        category = playlists[index]
        if category == self._category:
            return False
        await self._send_navigation(None, category)
        logger.debug("%s: JDN playlist category=%s position=%s/%s", self.controller.serial, category, index + 1, len(playlists))
        return True

    async def _navigate(self, direction):
        """Return True on movement, False at an edge, or None when navigation is unavailable."""
        if self._playlist_picker:
            return await self._navigate_playlists(direction)
        songs = self._playlists.get(self._category, []) if self._category else self._song_order
        if not songs or self._navigation_busy:
            return None
        step = 1 if direction in {Command.RIGHT, Command.DOWN} else -1
        vertical = direction in {Command.UP, Command.DOWN}
        # The browser uses three columns in All Songs and a single column inside playlists.
        if vertical and not self._category:
            step *= 3
        index = songs.index(self._current_song) if self._current_song in songs else -step
        if vertical and not 0 <= index + step < len(songs):
            return False
        index = min(len(songs) - 1, max(0, index + step))
        song = songs[index]
        if song == self._current_song:
            return False
        if not self._category and self._tab != "songlist":
            self._tab = "songlist"
            await self._send_tab("songlist")
        await self._send_navigation(song, self._category)
        logger.debug("%s: JDN navigation song=%s category=%s position=%s/%s",
                     self.controller.serial, song, self._category, index + 1, len(songs))
        return True

    async def send_message(self, name, data=None):
        if not self._websocket_is_open(self.ws):
            return False
        await self.ws.send(json.dumps({"func": name, **(data or {})}, separators=(",", ":"), allow_nan=False))
        if name not in {"pong", "sync", "playerScoredPhone"}:
            logger.debug("%s: JDN >>> command=%s", self.controller.serial, name)
        return True

    async def on_message(self, message_str):
        try:
            message = parse_json(message_str.encode("utf-8"))
            name = message["func"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,80}", name):
                raise ValueError("Invalid command")
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise GameConnectionError("Invalid JDN command.", should_reconnect=False) from error
        self._last_received = time.monotonic()
        if name not in {"ping", "sync"}:
            logger.debug("%s: JDN <<< command=%s fields=%s", self.controller.serial, name, sorted(message))
        if name == "ping":
            await self.send_message("pong")
        elif name == "sync":
            origin = integer(message["sync"]["o"], 0, 2**53)
            now = round(time.time() * 1000)
            await self.send_message("sync", {"r": now, "t": now, "d": 0, "o": origin})
        elif name == "clientSyncCompleted":
            self._server_anchor = integer(message["serverTime"], 0, 2**53)
            self._monotonic_anchor = time.monotonic()
            latency = message.get("latency", 0)
            if isinstance(latency, (int, float)) and math.isfinite(latency) and 0 <= latency <= 60000:
                self._server_anchor += latency / 2
            if not self._join_sent:
                self._join_sent = True
                await self.send_message("joinRoom", {"roomID": int(self.pairing_code), "dancercardID": self.api.profile["publicID"]})
        elif name in {"joinRoom", "roomController"} and message.get("error"):
            status = re.search(r"\b([45][0-9]{2})\b", str(message["error"]))
            details = f" (server error {status[1]})" if status else ""
            raise GameConnectionError(f"JDN rejected the Dance Room join{details}. Check the room number and try again.",
                                      PairingState.ERROR_CONSOLE_CONNECTION, should_reconnect=False)
        elif name == "playerJoined":
            self.is_connected = True
            self._handshake_deadline = None
            self._is_main_player = bool(message.get("isController"))
            self._device_as_controller = bool(message.get("isDeviceAsController"))
            song = message.get("song")
            if isinstance(song, str) and song in self.catalog:
                self._current_song = song
            player = min(6, max(1, integer(message.get("numberOfPlayersInRoom", 1), 1, 1000)))
            self.controller.get_latest_command(commands_to_check=set())
            await self._stop_connecting_led()
            await self.controller.set_player_led(player, flash=True)
            await self.on_state_changed({"state": PairingState.CONNECTED.value, "error_details": None,
                "player_name": self.api.profile.get("pName", "JoyDance"), "player_id": player,
                "is_main_player": self._is_main_player, "player_rename_feedback": None})
            await self._feedback(61)
        elif name == "racetrackColorUpdated":
            await self._update_player_color(message.get("colorHexa", message.get("color")))
        elif name == "controllerChanged":
            self._is_main_player = message.get("isController") is True
            self._device_as_controller = message.get("isDeviceAsController") is True
            if not self._is_main_player:
                self._play_request = None
            await self.on_state_changed({"is_main_player": self._is_main_player})
        elif name in {"roomClosed", "playerKicked"}:
            raise GameConnectionError("JDN closed the room or removed this controller.", should_reconnect=False)
        elif name == "tabRest" and message.get("tabName") in {"songlist", "playlist"}:
            if message.get("sender") != "web" and message["tabName"] in self._pending_tabs:
                self._pending_tabs.remove(message["tabName"])
            elif message.get("sender") == "web" or message["tabName"] != self._tab:
                self._tab = message["tabName"]
                self._category = self._current_song = ""
                self._playlist_picker = self._tab == "playlist"
                await self.on_state_changed({})
        elif name == "favoritesChanged" and message.get("playerID") == self.api.profile["publicID"]:
            self._set_favorites(message["favorites"])
        elif name in {"navRest", "playlistSelected"}:
            target = message.get("song"), message.get("category")
            if message.get("sender") != "web" and target in self._pending_navigation:
                # Replies to earlier moves must not undo a newer stick move or browser selection.
                self._pending_navigation.remove(target)
            else:
                if self._phase == "results":
                    self._phase = "browsing"
                category = message.get("category")
                if isinstance(category, str) and len(category) <= 128:
                    if category != self._category:
                        self._current_song = ""
                    self._category = category
                    self._tab = "playlist" if category else "songlist"
                    self._playlist_picker = False
                song = message.get("song")
                if isinstance(song, str) and song in self.catalog:
                    self._current_song = song
                if self._phase == "browsing":
                    await self.on_state_changed({})
        elif name == "songSelected":
            song = message["song"]
            if song not in self.catalog:
                raise GameConnectionError("JDN selected a song missing from the catalog.", should_reconnect=False)
            self._play_request = None
            self._current_song = song
            self._phase = "coach"
            self._coach = 0
            self._start_loading(song, self._coach)
            await self.on_state_changed({"additional_message": None})
        elif name == "coachSelected" and message.get("player") == self.api.profile["publicID"]:
            self._coach = integer(message["coach"], 0, 3)
            self._start_loading(self._current_song, self._coach)
        elif name == "virtualPayPerPlayPurchase" and self._purchase_pending:
            self._purchase_pending = False
            play_request, self._play_request = self._play_request, None
            if play_request is not None and self._phase == "coach" and self._is_main_player:
                token = message.get("token")
                if message.get("error") or not isinstance(token, str) or not 1 <= len(token) <= 8192:
                    await self.on_state_changed({"additional_message": "JDN could not start this song. Check your coins or VIP access."})
                else:
                    self._phase = "launching"
                    await self.on_state_changed({"additional_message": None})
                    await self.send_message("songLaunch", {"token": token})
        elif name == "songStart":
            await self._start_song(message)
        elif name == "videoDelay":
            self._apply_video_delay(message["delay"])
        elif name in {"bufferStart", "bufferEnd"}:
            self._stalled = name == "bufferStart"
        elif name == "songEnd":
            await self._score_pending(flush=True)
            await self._finish_song()
        elif name in {"songAborted", "songDeselected", "songSelection"}:
            self._stop_scoring()
            self._phase = "browsing"
            await self.on_state_changed({"additional_message": None})
        await self.on_game_message(message)

    def _apply_video_delay(self, delay):
        # The app reads a signed Int32; browser corrections can move the clock backwards.
        if type(delay) not in (int, float) or not -(2**31) <= delay < 2**31:
            raise ValueError("Invalid JDN video timing correction")
        self._song_start = self._initial_song_start + int(delay)
        logger.debug("%s: JDN video_delay_ms=%s song_start=%s", self.controller.serial, delay, self._song_start)

    async def _load_song(self, song_id, coach):
        song = self.catalog[song_id]
        if not ASSET_NAME.fullmatch(song_id):
            raise ValueError("Invalid song identifier")
        moves = parse_json(await self.api.asset(song, f"moves/{song_id}_moves{coach}.json"), jsonp=True)
        if not isinstance(moves, list) or not 1 <= len(moves) <= 5000:
            raise ValueError("Invalid JDN move schedule")
        previous = -1
        for move in moves:
            if not isinstance(move, dict) or not isinstance(move.get("name"), str) or not ASSET_NAME.fullmatch(move["name"]):
                raise ValueError("Invalid JDN move name")
            previous = integer(move["time"], previous, 3600000)
            integer(move["duration"], 1, 60000)
            if "goldMove" in move and (type(move["goldMove"]) not in (bool, int) or move["goldMove"] not in (0, 1)):
                raise ValueError("Invalid JDN Gold Move flag")
        names = {move["name"] for move in moves}
        if len(names) > 500:
            raise ValueError("Too many JDN classifiers")
        slots = asyncio.Semaphore(6)
        async def load(name):
            async with slots:
                data = await self.api.asset(song, f"classifiers_WIIU/{name}.msm")
                try:
                    return name, read_classifier(data)
                except ValueError as error:
                    logger.warning("%s: JDN classifier=%s rejected: %s", self.controller.serial, name, error)
                    raise
        async with asyncio.TaskGroup() as tasks:
            pending = [tasks.create_task(load(name)) for name in sorted(names)]
        return moves, dict(task.result() for task in pending)

    def _start_loading(self, song, coach):
        key = song, coach
        task = self._load_task
        if self._load_key == key and task is not None:
            if not task.done() or (not task.cancelled() and task.exception() is None):
                return
        if task is not None:
            task.cancel()
        if not self._purchase_pending:
            self._play_request = None
        self._load_key = key
        self._load_task = asyncio.create_task(self._load_song(song, coach))
        self._load_tasks.add(self._load_task)
        self._load_task.add_done_callback(self._load_tasks.discard)
        self._load_task.add_done_callback(self._loaded)

    def _loaded(self, task):
        if not task.cancelled() and task.exception():
            logger.warning("%s: JDN song preparation failed (%s)", self.controller.serial, type(task.exception()).__name__)

    async def _start_song(self, message):
        self._stop_scoring()
        self._current_song = message["songName"]
        if self._current_song not in self.catalog:
            raise GameConnectionError("JDN started an unknown song.", should_reconnect=False)
        self._coach = integer(message.get("coachNbr", self._coach), 0, 3)
        self._song_number = integer(message["songNr"], 0, 2**31 - 1)
        self._song_start = self._initial_song_start = integer(message["songStart"], 0, 2**53)
        self._apply_video_delay(message.get("videoDelay", 0))
        await self._update_player_color(message.get("racetrackColorHexa", message.get("racetrackColor")))
        self._move_index = self._gauge = self._missing_motion_count = 0
        self._score = self._score_offset = integer(message.get("scoreWhenLeftRoom", 0), 0, 13333)
        self._total_raw = 0.0
        self._stalled = False
        self.should_start_accelerometer = bool(message.get("reportScore", True))
        self.controller.set_accel_collection(self.should_start_accelerometer)
        collection_start = self.server_time()
        generation = self._generation
        self._start_loading(self._current_song, self._coach)
        try:
            moves, classifiers = await self._load_task
        except Exception as error:
            raise GameConnectionError("Could not prepare JDN song classifiers. Check the debug log.", should_reconnect=False) from error
        if generation != self._generation:
            return
        self._moves, self._classifiers = moves, classifiers
        gold_count = sum(bool(move.get("goldMove")) for move in moves)
        self._max_raw = float32((len(moves) - gold_count - 5) * 1.5 + 5 + gold_count * 7.5)
        if self._max_raw <= 0:
            raise GameConnectionError("This JDN move schedule cannot be scored.", should_reconnect=False)
        while self._move_index < len(moves):
            move = moves[self._move_index]
            if self._song_start + move["time"] + move["duration"] + SCORING_WINDOW_MS >= collection_start:
                break
            self._move_index += 1
        self._phase = "dancing"
        logger.debug("%s: JDN scoring song=%s coach=%s song_nr=%s moves=%s gold=%s start=%s skipped_elapsed=%s restored_score=%s enabled=%s",
                     self.controller.serial, self._current_song, self._coach, self._song_number, len(moves), gold_count,
                     self._song_start, self._move_index, self._score, self.should_start_accelerometer)
        if self.should_start_accelerometer and self._move_index == 0:
            await self._feedback(51)
        await self.on_state_changed({"additional_message": None})

    def _stop_scoring(self):
        self._generation += 1
        self._play_request = None
        self.should_start_accelerometer = False
        self.controller.set_accel_collection(False)
        self._motion.clear()
        self._last_sample = None
        self._moves, self._classifiers = [], {}

    async def _finish_song(self):
        self._stop_scoring()
        self._phase = "results"
        await self.on_state_changed({})

    async def _score_pending(self, *, flush=False):
        async with self._score_lock:
            await self._score_moves(flush)

    async def _score_moves(self, flush):
        generation = self._generation
        padding = SCORING_WINDOW_MS + MOTION_EDGE_TOLERANCE_MS
        while self.should_start_accelerometer and self._move_index < len(self._moves):
            move = self._moves[self._move_index]
            start = self._song_start + move["time"]
            end = start + move["duration"]
            deadline = end + SCORING_WINDOW_MS
            now = self.server_time()
            if now <= deadline:
                return
            if (not flush and now <= deadline + MOTION_EDGE_TOLERANCE_MS
                    and (not self._motion or self._motion[-1][0] <= deadline)):
                return
            samples = [sample for sample in self._motion if start - padding <= sample[0] <= end + padding]
            args = self._classifiers[move["name"]], samples, start, move["duration"], self._score_pointer
            if self._scoring_workers is None:
                result, pointer, offset = await asyncio.to_thread(score_adaptive_move, *args)
            else:
                result, pointer, offset = await self._scoring_workers.score(*args)
            if generation != self._generation or not self.should_start_accelerometer:
                return
            # A video delay can shift the move while the worker is scoring it.
            if start != self._song_start + move["time"]:
                return
            if result is None:
                self._missing_motion_count += 1
                if self._missing_motion_count == 1:
                    logger.warning("%s: JDN missing motion move=%s samples=%s; further gaps summarized at scoring completion",
                                   self.controller.serial, self._move_index, len(samples))
                self._move_index += 1
                continue
            self._score_pointer = pointer
            raw, energy, factor, rating = result
            self._gauge = min(20, max(0, self._gauge + (-2, -2, 1, 2)[rating]))
            if move.get("goldMove"):
                raw, rating = (5.0, 4) if rating >= 2 else (0.0, 0)
            if self._gauge > 10:
                raw = float32(raw * 1.5)
            self._total_raw = float32(self._total_raw + raw)
            previous_score = self._score
            previous_stars = min(5, previous_score // 2000)
            self._score = min(13333, self._score_offset + max(0, round(float32(float32(self._total_raw / self._max_raw) * 13333))))
            await self.send_message("playerScoredPhone", {"totalScore": self._score, "moveNr": self._move_index,
                "rating": RATINGS[rating], "songNr": self._song_number, "isDeviceAsController": self._device_as_controller})
            if generation != self._generation:
                return
            stars = min(5, self._score // 2000)
            feedback = ([7] if rating == 4 else []) + list(range(previous_stars + 1, stars + 1))
            feedback += [effect for threshold, effect in ((11000, 26), (12000, 40))
                         if previous_score < threshold <= self._score]
            if feedback:
                logger.debug("%s: JDN gameplay feedback gold=%s stars=%s effects=%s total=%s rumble_enabled=%s",
                             self.controller.serial, rating == 4, stars, feedback, self._score, self.controller.rumble_enabled)
                await self._feedback(*feedback)
            if logger.isEnabledFor(logging.DEBUG):
                minimum = tuple(round(min(s[i] for s in samples), 4) for i in (1, 2, 3))
                maximum = tuple(round(max(s[i] for s in samples), 4) for i in (1, 2, 3))
                logger.debug("%s: JDN move=%s name=%s samples=%s accel_min=%s accel_max=%s raw=%.4f "
                             "energy=%.4f factor=%.4f rating=%s total=%s timing_offset_ms=%.1f timing_pointer=%.3f",
                             self.controller.serial, self._move_index, move["name"], len(samples), minimum, maximum,
                             raw, energy, factor, RATINGS[rating], self._score, offset, pointer)
            self._move_index += 1
        if self.should_start_accelerometer and self._moves and self._move_index == len(self._moves):
            # App 9.9.0 only sends playerScoredPhone; playerEndScore has no active callers.
            logger.debug("%s: JDN scoring complete total=%s missing_motion=%s; enabling results controls",
                         self.controller.serial, self._score, self._missing_motion_count)
            # Browser rooms can return to results without sending songEnd to the controller.
            await self._finish_song()

    async def tick(self):
        while True:
            if not self.controller.is_connected():
                await self.disconnect(should_reconnect=True)
                return
            now = time.monotonic()
            if self._handshake_deadline is not None and asyncio.get_running_loop().time() >= self._handshake_deadline:
                raise GameConnectionError("JDN did not finish room pairing in time.")
            if now - self._last_received > 45:
                raise GameConnectionError("JDN stopped responding to this controller.")
            if self.is_connected and self.should_start_accelerometer and not self._stalled:
                samples = self.controller.get_accel_events()
                server_now = self.server_time()
                if samples:
                    step = 1000 / self.accel_acquisition_freq_hz
                    first = server_now - (len(samples) - 1) * step
                    if self._last_sample is not None and abs(first - self._last_sample - step) < 100:
                        first = self._last_sample + step
                    for index, xyz in enumerate(samples):
                        timestamp = first + index * step
                        # A bunched report can re-anchor the batch inside the buffered motion.
                        if self._last_sample is not None and timestamp <= self._last_sample:
                            continue
                        if len(xyz) != 3 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in xyz):
                            raise ValueError("Invalid controller motion sample")
                        self._motion.append((timestamp, *(max(-3.4, min(3.4, v)) for v in xyz)))
                        self._last_sample = timestamp
                while self._motion and self._motion[0][0] < server_now - 30000:
                    self._motion.popleft()
                if self._phase == "dancing":
                    await self._score_pending()
            elif self.should_start_accelerometer:
                self.controller.get_accel_events()
            await asyncio.sleep(FRAME_DURATION)

    async def _feedback(self, *sound_indices):
        if sound_indices and self.controller.rumble_enabled:
            await self._stop_rumble()
            async def play():
                for sound_index in sound_indices:
                    await self._play_rumble(sound_index)
            self._rumble_task = asyncio.create_task(play())

    async def send_command(self):
        next_joystick_repeat_time = 0.0
        last_joystick_command = None
        stick_warned_at_edge = False
        allowed = DIRECTIONS | {Command.ACCEPT, Command.BACK, Command.GOTO_SONGSTAB,
                                Command.GOTO_PLAYLISTSTAB, Command.VIDEO_RESTART}
        while True:
            command = self.controller.get_latest_command(commands_to_check=allowed)
            joystick = self.controller.get_joystick_command() if self.is_connected and not self.should_start_accelerometer else None
            if joystick != last_joystick_command:
                next_joystick_repeat_time = 0.0
                stick_warned_at_edge = False
            last_joystick_command = joystick
            from_stick = command is None and joystick in DIRECTIONS and time.monotonic() >= next_joystick_repeat_time
            if from_stick:
                command = joystick
                next_joystick_repeat_time = time.monotonic() + FRAME_DURATION * 15
            if (self.is_connected and command is not None and not self.should_start_accelerometer
                    and self._phase in {"browsing", "coach", "results"}):
                if self._phase == "coach" and command in {Command.LEFT, Command.RIGHT}:
                    coaches = integer(self.catalog[self._current_song]["coaches"], 1, 4)
                    self._coach = (self._coach + (1 if command == Command.RIGHT else -1)) % coaches
                    await self.send_message("selectCoach", {"coach": self._coach})
                    self._start_loading(self._current_song, self._coach)
                elif self._is_main_player:
                    if self._phase == "results":
                        if command == Command.ACCEPT:
                            # The recap sends the song position before asking the screen to leave results.
                            await self.send_message("navContinue", {"song": self._current_song, "category": self._category})
                            await self.send_message("songSelection")
                        elif command == Command.VIDEO_RESTART and self._current_song:
                            await self.send_message("selectSong", {"song": self._current_song})
                    elif command in {Command.GOTO_SONGSTAB, Command.GOTO_PLAYLISTSTAB} and self._phase == "browsing":
                        await self._switch_tab("songlist" if self._tab == "playlist" else "playlist")
                    elif command in DIRECTIONS and self._phase == "browsing" and self.catalog:
                        moved = await self._navigate(command)
                        if moved:
                            stick_warned_at_edge = False
                        elif moved is False and (not from_stick or not stick_warned_at_edge):
                            await self._feedback(37)
                            stick_warned_at_edge = stick_warned_at_edge or from_stick
                    elif command == Command.ACCEPT and self._phase == "coach" and not self._purchase_pending:
                        self._start_loading(self._current_song, self._coach)
                        self._play_request = self._load_task
                    elif (command == Command.ACCEPT and self._phase == "browsing"
                          and (self._playlist_picker or (self._category and not self._current_song))):
                        if self._playlists.get(self._category):
                            self._playlist_picker = False
                            await self._navigate(Command.RIGHT)
                    elif command == Command.ACCEPT and self._phase == "browsing" and self._current_song:
                        await self.send_message("selectSong", {"song": self._current_song})
                    elif command == Command.BACK:
                        self._play_request = None
                        if self._phase == "browsing" and self._tab == "playlist":
                            if not self._navigation_busy:
                                self._playlist_picker = True
                                self._current_song = ""
                                if self._category in self._playlists:
                                    await self._send_navigation(None, self._category)
                        else:
                            await self.send_message("deselectSong" if self._phase == "coach" else "songSelection")
            await self._submit_ready_play_request()
            await asyncio.sleep(FRAME_DURATION)

    async def _submit_ready_play_request(self):
        load = self._play_request
        # Keep at most one purchase on the wire because the reply has no request ID.
        if load is None or not load.done() or self._purchase_pending:
            return
        if (not self.is_connected or not self._is_main_player or self._phase != "coach"
                or load is not self._load_task or load.cancelled()):
            self._play_request = None
        elif load.exception() is not None:
            self._play_request = None
            await self.on_state_changed({"additional_message": "Could not prepare this song. Press A to try again."})
        else:
            self._purchase_pending = True
            if not await self.send_message("virtualPayPerPlayPurchase", {"trade": self._current_song}):
                self._purchase_pending = False
                self._play_request = None

    async def disconnect(self, *args, **kwargs):
        tasks = tuple(self._load_tasks)
        for task in tasks:
            task.cancel()
        try:
            await super().disconnect(*args, **kwargs)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def connect_ws(self):
        raise NotImplementedError("JDN connects to its room server during pairing.")

    async def preprocess_command(self, cmd):
        return cmd
