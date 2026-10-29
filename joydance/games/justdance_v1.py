from .justdance_abstract import JustDanceGameAbstract
from collections import defaultdict
import json
import logging
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from joydance.constants import (
    WS_SUBPROTOCOLS,
    Command,
    PairingState,
    WsSubprotocolVersion,
    UiLayer,
)

logger = logging.getLogger("joydance")


class JustDanceGameV1(JustDanceGameAbstract):
    """
    Game protocol handler for Just Dance protocol version v1.
    Handles all v1-specific communication, message parsing, and command preprocessing.
    """

    def __init__(self, controller, *args, **kwargs):
        super().__init__(controller, *args, **kwargs)
        self.is_main_player = False
        self._carousel_setup = defaultdict(
            lambda: {
                "row_num": 0,
                "col_num_per_row_id": defaultdict(int),
                "num_columns_per_row_id": {},
                "item_actions": {},
                "disabled_items": set(),
                "action_id": 0,
            }
        )
        self._screen_carousel_type = "main"
        self.coach_id = 0
        self.num_coaches = 0
        self._coach_actions = []

    def _reset_input_state(self):
        super()._reset_input_state()
        self.is_main_player = False
        self._state["is_main_player"] = False

    @property
    def current_carousel_type(self):
        return "popup" if self._active_ui_layer is UiLayer.POPUP else self._screen_carousel_type

    @property
    def carousel_setup(self):
        return self._carousel_setup[self.current_carousel_type]

    @property
    def row_num(self):
        return self._carousel_setup[self.current_carousel_type]["row_num"]

    @row_num.setter
    def row_num(self, value):
        self._carousel_setup[self.current_carousel_type]["row_num"] = value

    @property
    def col_num_per_row_id(self):
        return self._carousel_setup[self.current_carousel_type]["col_num_per_row_id"]

    @col_num_per_row_id.setter
    def col_num_per_row_id(self, value):
        self._carousel_setup[self.current_carousel_type]["col_num_per_row_id"] = value

    @property
    def num_columns_per_row_id(self):
        return self._carousel_setup[self.current_carousel_type]["num_columns_per_row_id"]

    @num_columns_per_row_id.setter
    def num_columns_per_row_id(self, value):
        self._carousel_setup[self.current_carousel_type]["num_columns_per_row_id"] = value

    @property
    def item_actions(self):
        return self._carousel_setup[self.current_carousel_type]["item_actions"]

    @item_actions.setter
    def item_actions(self, value):
        self._carousel_setup[self.current_carousel_type]["item_actions"] = value

    @property
    def action_id(self):
        return self._carousel_setup[self.current_carousel_type]["action_id"]

    @action_id.setter
    def action_id(self, value):
        self._carousel_setup[self.current_carousel_type]["action_id"] = value

    def is_command_allowed(self, cmd):
        if not super().is_command_allowed(cmd):
            return False
        if cmd == Command.ACCEPT and not self.is_in_lobby:
            position = (self.row_num, self.col_num_per_row_id[self.row_num])
            return position not in self.carousel_setup["disabled_items"]
        return True

    async def parse_input_setup_data(self, message):
        await super().parse_input_setup_data(message)
        carousel_pos_setup = message.get("carouselPosSetup")
        if carousel_pos_setup:
            carousel_type = "popup" if message.get("applyOnPopup") else self._screen_carousel_type
            carousel_setup = self._carousel_setup[carousel_type]
            carousel_setup["row_num"] = carousel_pos_setup["rowIndex"]
            carousel_setup["col_num_per_row_id"][carousel_setup["row_num"]] = carousel_pos_setup["itemIndex"]
            carousel_setup["action_id"] = carousel_pos_setup["actionIndex"]

    @staticmethod
    def parse_actions(raw_item_actions):
        item_actions = []
        for item_action in raw_item_actions:
            parsed_action = item_action.get("command", "")
            if parsed_action:
                try:
                    parsed_action = json.loads(parsed_action)["root"]
                except Exception:
                    logger.debug("V1 Failed to parse command: %s", item_action)
                    parsed_action = ""
            logger.debug("V1 parse_actions: %s, %s", item_action, parsed_action)
            item_actions.append(parsed_action)
        return item_actions

    async def parse_phone_setup_data(self, data):
        await super().parse_phone_setup_data(data)
        setup_data = data.get("setupData")
        if not isinstance(setup_data, dict):
            return
        is_popup = bool(data.get("isPopup"))
        if not is_popup:
            # Shared gameplay/recap screens do not establish navigation ownership.
            if setup_data.get("__class") == "JD_SimplePhoneUiData":
                self.is_main_player = False
            elif setup_data.get("lobbySetup"):
                self.is_main_player = self._has_lobby_start_button
            elif setup_data.get("mainCarousel") and "recapSetup" not in setup_data:
                self.is_main_player = True
            if setup_data.get("lobbySetup"):
                self._screen_carousel_type = "lobby"
            elif setup_data.get("recapSetup"):
                self._screen_carousel_type = "recap"
            else:
                self._screen_carousel_type = "main"

        disabled_items = set()
        self.carousel_setup["disabled_items"] = disabled_items
        main_carousel_rows = setup_data.get("mainCarousel", {}).get("rows", [])
        if main_carousel_rows:
            num_columns_per_row_id = {}
            all_item_actions = defaultdict(dict)
            for row_num, row in enumerate(main_carousel_rows):
                num_columns_per_row_id[row_num] = len(row["items"])
                for item_idx, item in enumerate(row.get("items", [])):
                    all_item_actions[row_num][item_idx] = self.parse_actions(item.get("actions", []))
                    # Default missing isEnabled to false
                    if not item.get("isEnabled", False):
                        disabled_items.add((row_num, item_idx))
            self.item_actions = all_item_actions
            self.num_columns_per_row_id = num_columns_per_row_id

        carousel_pos_setup = data.get("inputSetup", {}).get("carouselPosSetup", {})
        if carousel_pos_setup:
            self.row_num = carousel_pos_setup["rowIndex"]
            self.col_num_per_row_id[self.row_num] = carousel_pos_setup["itemIndex"]
            self.action_id = carousel_pos_setup["actionIndex"]

        lobby_setup = setup_data.get("lobbySetup", {})
        if lobby_setup:
            self.num_columns_per_row_id = {0: 1}
            self.item_actions = {
                0: {0: self.parse_actions(lobby_setup.get("startButton", {}).get("actions", []))}
            }
            if not is_popup:
                coaches = lobby_setup.get("coaches", [])
                self.num_coaches = len(coaches)
                self.coach_id = next(
                    (index for index, coach in enumerate(coaches) if coach.get("selectedOnEnter")), 0
                )
                self._coach_actions = [self.parse_actions(coach.get("actions", [])) for coach in coaches]

        shortcuts_data = setup_data.get("shortcuts") or []
        logger.debug("V1 shortcuts_data: %s", shortcuts_data)
        shortcuts = set()
        if setup_data.get("gameplaySetup", {}).get("pauseSlider", {}):
            shortcuts.add(Command.PAUSE)
        for shortcut in shortcuts_data:
            try:
                command_root = json.loads(shortcut["command"])["root"]
                if "identifier" in command_root:
                    shortcuts.add(Command(command_root["identifier"]))
                elif "input" in command_root:
                    shortcuts.add(Command(command_root["input"]))
            except Exception:
                logger.exception("V1: Failed to parse shortcut: %s", shortcut)
        if any(
            len(item.get("actions", [])) > 1
            for row in main_carousel_rows
            for item in row.get("items", [])
        ):
            shortcuts.add(Command.NEXT_ACTION)
        self.available_shortcuts = shortcuts
        self._sync_controller_shortcuts()
        if not is_popup and self._state.get("is_main_player") != self.is_main_player:
            await self.on_state_changed({"is_main_player": self.is_main_player})

    async def connect_ws(self):
        if not self.pairing_url.startswith("ws://"):
            logger.warning(
                f"{self.controller.serial}: V1 connect_ws expects ws:// pairing_url, got {self.pairing_url}"
            )

        subprotocol = WS_SUBPROTOCOLS[WsSubprotocolVersion.V1.value]
        try:
            logger.debug(
                f"{self.controller.serial}: V1 connecting to {self.pairing_url} with subprotocol {subprotocol}, sock: {'present' if self.console_conn else 'None'}"
            )
            async with connect(
                self.pairing_url,
                subprotocols=[subprotocol],
                sock=self.console_conn,
                ssl=None,
                ping_timeout=None,
                max_size=None,  # Disable message size limit to handle large JD_PhoneUiSetupData messages
                proxy=None,
            ) as websocket:
                self.ws = websocket
                # Set state only on JD_PhoneDataCmdHandshakeContinue response
                await self.run_connection_tasks()

        except ConnectionClosed as e:
            logger.warning("%s: V1 WebSocket connection closed: %s", self.controller.serial, e)
            await self.on_state_changed(
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect()
        except ConnectionRefusedError as e:
            logger.error("%s: V1 Connection refused for %s: %s", self.controller.serial, self.pairing_url, e)
            await self.on_state_changed(
                {"state": PairingState.ERROR_CONSOLE_CONNECTION.value, "error_details": str(e)},
            )
            await self.disconnect()
        except Exception as e:
            logger.exception(
                "%s: V1 An error occurred while connecting WebSocket to console.",
                self.controller.serial,
            )
            await self.on_state_changed(
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect()

    async def _message_receive_loop(self):
        try:
            async for message_str in self.ws:
                await self.on_message(message_str)
        except ConnectionClosed:
            logger.info(
                "%s: V1 WebSocket connection closed while receiving messages.",
                self.controller.serial,
            )
        except Exception:
            logger.exception("%s: V1 Error in message receive loop.", self.controller.serial)
            await self.disconnect()

    async def pair(self):
        try:
            self.is_connected = False
            self.should_reconnect = True

            if self.console_ip_addr:
                await self.on_state_changed(
                    {"state": PairingState.CONNECTING.value}
                )
                self.pairing_url = f"ws://{self.console_ip_addr}:8080/smartphone"

            else:
                await self.on_state_changed(
                    {"state": PairingState.GETTING_TOKEN.value}
                )
                logger.debug("%s: Getting V1 authorization token...", self.controller.serial)
                await self.get_access_token()

                await self.on_state_changed(
                    {"state": PairingState.PAIRING.value}
                )
                logger.debug("%s: V1 Sending pairing code...", self.controller.serial)
                await self.send_pairing_code()

                if self.pairing_url.startswith("wss://"):
                    self.pairing_url = self.pairing_url.replace("wss://", "ws://", 1)
                    logger.debug(
                        "%s: V1 Adjusted pairing URL to ws: %s",
                        self.controller.serial,
                        self.pairing_url,
                    )

                await self.on_state_changed(
                    {"state": PairingState.CONNECTING.value}
                )
                logger.debug("%s: V1 Connecting with console...", self.controller.serial)
                if self.requires_punch_pairing:
                    await self.send_initiate_punch_pairing()

            await self.connect_ws()
        except Exception:
            await self.disconnect()
            logger.exception("%s: V1 An error occurred while pairing.", self.controller.serial)

    async def preprocess_command(self, cmd):
        if not self.is_command_allowed(cmd):
            return None, None
        __class, data = None, {}

        if cmd == Command.PAUSE:
            __class = "JD_Pause_PhoneCommandData"
        elif cmd == Command.NEXT_ACTION:
            if self.is_in_lobby:
                return None, None
            row_idx, col_idx = self.row_num, self.col_num_per_row_id[self.row_num]
            actions = self.item_actions.get(row_idx, {}).get(col_idx, [])
            if len(actions) < 2:
                return None, None
            self.action_id = (self.action_id + 1) % len(actions)
            __class = "ChangeAction_PhoneCommandData"
            data = {"rowIndex": row_idx, "itemIndex": col_idx, "actionIndex": self.action_id}
        elif cmd == Command.BACK:
            if self.is_search_opened:
                __class = "JD_CancelKeyboard_PhoneCommandData"
            else:
                __class = "JD_Custom_PhoneCommandData"
                data["identifier"] = cmd.value
        elif type(cmd.value) == str:
            __class = "JD_Custom_PhoneCommandData"
            data["identifier"] = cmd.value
        elif cmd == Command.V1_FAVORITE:
            __class = "JD_Input_PhoneCommandData"
            data["input"] = cmd.value
        elif cmd == Command.ACCEPT:
            row_idx, col_idx, action_idx = (
                self.row_num,
                self.col_num_per_row_id[self.row_num],
                self.action_id,
            )
            try:
                selected_action = self.item_actions[row_idx][col_idx][action_idx]
                if not isinstance(selected_action, str):
                    selected_action = selected_action.copy()
            except:
                logger.error(
                    "Failed to get selected action %s, %s, %s, %s",
                    row_idx,
                    col_idx,
                    action_idx,
                    self.item_actions,
                    exc_info=True,
                )
                selected_action = ""
            if selected_action:
                if isinstance(selected_action, str):
                    return None, selected_action
                return selected_action.pop("__class", None), selected_action
            else:
                __class = "ValidateAction_PhoneCommandData"
                data["rowIndex"] = row_idx
                data["itemIndex"] = col_idx
                data["actionIndex"] = action_idx
        elif cmd == Command.UP:
            if self.is_in_lobby:
                return None, None
            self.action_id = 0
            __class = "ChangeRow_PhoneCommandData"
            self.row_num -= 1
            if self.row_num < 0:
                self.row_num = len(self.num_columns_per_row_id) - 1
            data["rowIndex"] = self.row_num
        elif cmd == Command.DOWN:
            if self.is_in_lobby:
                return None, None
            self.action_id = 0
            __class = "ChangeRow_PhoneCommandData"
            self.row_num += 1
            if self.row_num >= len(self.num_columns_per_row_id):
                self.row_num = 0
            data["rowIndex"] = self.row_num
        elif cmd in (Command.LEFT, Command.RIGHT) and self.is_in_lobby:
            coach_id = self.coach_id + (-1 if cmd == Command.LEFT else 1)
            if not 0 <= coach_id < self.num_coaches:
                return None, None
            self.coach_id = coach_id
            actions = self._coach_actions[coach_id]
            if actions and isinstance(actions[0], dict):
                data = actions[0].copy()
                return data.pop("__class", None), data
            if actions and isinstance(actions[0], str) and actions[0]:
                return None, actions[0]
            return None, None
        elif cmd == Command.LEFT:
            self.action_id = 0
            __class = "ChangeItem_PhoneCommandData"
            data["rowIndex"] = self.row_num
            self.col_num_per_row_id[self.row_num] -= 1
            if self.col_num_per_row_id[self.row_num] < 0:
                self.col_num_per_row_id[self.row_num] = self.num_columns_per_row_id[self.row_num] - 1
            data["itemIndex"] = self.col_num_per_row_id[self.row_num]
        elif cmd == Command.RIGHT:
            self.action_id = 0
            __class = "ChangeItem_PhoneCommandData"
            data["rowIndex"] = self.row_num
            self.col_num_per_row_id[self.row_num] += 1
            if self.col_num_per_row_id[self.row_num] >= self.num_columns_per_row_id[self.row_num]:
                self.col_num_per_row_id[self.row_num] = 0
            data["itemIndex"] = self.col_num_per_row_id[self.row_num]
        return __class, data
