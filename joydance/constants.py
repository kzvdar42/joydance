from enum import Enum

JOYDANCE_VERSION = '0.6.0-beta'
UBI_APP_ID = '210da0fb-d6a5-4ed1-9808-01e86f0de7fb'
UBI_SKU_ID = 'jdcompanion-android'


class WsSubprotocolVersion(Enum):
    V1 = 'v1',
    V2 = 'v2',


WS_SUBPROTOCOLS = {
    WsSubprotocolVersion.V1.value: 'v1.phonescoring.jd.ubisoft.com',
    WsSubprotocolVersion.V2.value: 'v2.phonescoring.jd.ubisoft.com',
}


class WsCommand(Enum):
    GET_JOYCON_LIST = 'get_joycon_list'
    CONNECT_JOYCON = 'connect_joycon'
    DISCONNECT_JOYCON = 'disconnect_joycon'
    UPDATE_JOYCON_STATE = 'update_joycon_state'
    SEARCH_INPUT = 'search_input'
    SHOW_SEARCH = 'show_search'
    HIDE_SEARCH = 'hide_search'
    TOGGLE_RUMBLE = 'toggle_rumble'


class PairingMethod(Enum):
    DEFAULT = 'default'
    FAST = 'fast'
    STADIA = 'stadia'
    OLD = 'old'


FRAME_DURATION = 0.015
SEND_FREQ_MS = 0.05
ACCEL_ACQUISITION_FREQ_HZ = 200  # Hz
ACCEL_ACQUISITION_LATENCY = 0  # ms
ACCEL_MAX_RANGE = 8  # ±G

DEFAULT_CONFIG = {
    'pairing_method': 'default',
    'host_ip_addr': '',
    'console_ip_addr': '',
    'pairing_code': '',
}


class Command(Enum):
    UP = 3690595578
    RIGHT = 1099935642
    DOWN = 2467711647
    LEFT = 3652315484
    ACCEPT = 1084313942

    V1_KEYBOARD_ERROR_OK = 185785632
    V1_FAVORITE = 2424896653

    PAUSE = 'PAUSE'

    BACK = 'SHORTCUT_BACK'
    CHANGE_DANCERCARD = 'SHORTCUT_CHANGE_DANCERCARD'
    DONT_SHOW_ANYMORE = 'SHORTCUT_DONT_SHOW_ANYMORE'
    FAVORITE = 'SHORTCUT_FAVORITE'
    GOTO_SONGSTAB = 'SHORTCUT_GOTO_SONGSTAB'
    SKIP = 'SHORTCUT_SKIP'
    SORTING = 'SHORTCUT_SORTING'
    SWAP_GENDER = 'SHORTCUT_SWAP_GENDER'
    SWEAT_ACTIVATION = 'SHORTCUT_SWEAT_ACTIVATION'
    TOGGLE_COOP = 'SHORTCUT_TOGGLE_COOP'
    UPLAY = 'SHORTCUT_UPLAY'

    ACTIVATE_DANCERCARD = 'SHORTCUT_ACTIVATE_DANCERCARD'
    DELETE_DANCERCARD = 'SHORTCUT_DELETE_DANCERCARD'

    DELETE_PLAYLIST = 'SHORTCUT_DELETE_PLAYLIST'
    SAVE_PLAYLIST = 'SHORTCUT_SAVE_PLAYLIST'
    PLAYLIST_RENAME = 'SHORTCUT_PLAYLIST_RENAME'
    PLAYLIST_DELETE_SONG = 'SHORTCUT_PLAYLIST_DELETE_SONG'
    PLAYLIST_MOVE_SONG_LEFT = 'SHORTCUT_PLAYLIST_MOVE_SONG_LEFT'
    PLAYLIST_MOVE_SONG_RIGHT = 'SHORTCUT_PLAYLIST_MOVE_SONG_RIGHT'

    TIPS_NEXT = 'SHORTCUT_TIPS_NEXT'
    TIPS_PREVIOUS = 'SHORTCUT_TIPS_PREVIOUS'

    SHORTCUT_SEARCH = 'SHORTCUT_SEARCH'
    SHORTCUT_EXTRA = 'SHORTCUT_EXTRA'


class JoyConButton(Enum):
    # Joy-Con (L)
    UP = 'up'
    RIGHT = 'right'
    DOWN = 'down'
    LEFT = 'left'
    L = 'l'
    ZL = 'zl'
    MINUS = 'minus'
    CAPTURE = 'capture'
    LEFT_STICK = 'stick_l_btn'
    LEFT_SR = 'left_sr'
    LEFT_SL = 'left_sl'

    # Joy-Con (R)
    A = 'a'
    B = 'b'
    X = 'x'
    Y = 'y'
    R = 'r'
    ZR = 'zr'
    PLUS = 'plus'
    HOME = 'home'
    RIGHT_STICK = 'stick_r_btn'
    RIGHT_SL = 'right_sl'
    RIGHT_SR = 'right_sr'
    CHARGING_GRIP = 'charging-grip'


# Assign buttons on Joy-Con (R) with commands
SHORTCUT_MAPPING = {
    JoyConButton.X: [
        Command.DELETE_DANCERCARD,
        Command.DELETE_PLAYLIST,
        Command.GOTO_SONGSTAB,
        Command.PLAYLIST_DELETE_SONG,
        Command.SKIP,
        Command.SORTING,
        Command.SWAP_GENDER,
        Command.TOGGLE_COOP,
        Command.SHORTCUT_EXTRA,
        Command.V1_FAVORITE,
    ],
    JoyConButton.Y: [
        Command.ACTIVATE_DANCERCARD,
        Command.CHANGE_DANCERCARD,
        Command.SWEAT_ACTIVATION,
        Command.UPLAY,
        Command.SHORTCUT_SEARCH,
    ],
    JoyConButton.PLUS: [
        Command.DONT_SHOW_ANYMORE,
        Command.FAVORITE,
        Command.PAUSE,
        Command.PLAYLIST_RENAME,
        Command.SAVE_PLAYLIST,
    ],
    JoyConButton.R: [
        Command.PLAYLIST_MOVE_SONG_LEFT,
        Command.TIPS_PREVIOUS,
    ],
    JoyConButton.ZR: [
        Command.PLAYLIST_MOVE_SONG_RIGHT,
        Command.TIPS_NEXT,
    ],
}

# Same with Joy-Con (L)
SHORTCUT_MAPPING[JoyConButton.UP] = SHORTCUT_MAPPING[JoyConButton.X]
SHORTCUT_MAPPING[JoyConButton.LEFT] = SHORTCUT_MAPPING[JoyConButton.Y]
SHORTCUT_MAPPING[JoyConButton.MINUS] = SHORTCUT_MAPPING[JoyConButton.PLUS]
SHORTCUT_MAPPING[JoyConButton.L] = SHORTCUT_MAPPING[JoyConButton.R]
SHORTCUT_MAPPING[JoyConButton.ZL] = SHORTCUT_MAPPING[JoyConButton.ZR]
