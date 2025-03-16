export const PairingMethod = {
    DEFAULT: 'default',
    FAST: 'fast',
    STADIA: 'stadia',
    OLD: 'old',
}

export const WsCommand = {
    GET_JOYCON_LIST: 'get_joycon_list',
    CONNECT_JOYCON: 'connect_joycon',
    DISCONNECT_JOYCON: 'disconnect_joycon',
    UPDATE_JOYCON_STATE: 'update_joycon_state',
    SEARCH_INPUT: 'search_input',
    SHOW_SEARCH: 'show_search',
    HIDE_SEARCH: 'hide_search',
    TOGGLE_RUMBLE: 'toggle_rumble',
}

export const WebSocketState = {
    CONNECTED: 0,
    DISCONNECTED: 1,
    RECONNECTING: 2,
}
