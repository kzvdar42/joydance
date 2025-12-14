export const PairingMethod = {
    DEFAULT: 'default',
    FAST: 'fast',
    STADIA: 'stadia',
    OLD: 'old',
}

export const WsCommand = {
    GET_CONTROLLER_LIST: 'get_controller_list',
    CONNECT_CONTROLLER: 'connect_controller',
    DISCONNECT_CONTROLLER: 'disconnect_controller',
    UPDATE_CONTROLLER_STATE: 'update_controller_state',
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

export const IpAddressRegex = {
    // IP address validation pattern for JavaScript regex
    JS_PATTERN: /^(\d{1,2}|1\d\d|2[0-4]\d|25[0-5])\.(\d{1,2}|1\d\d|2[0-4]\d|25[0-5])\.((\d{1,2}|1\d\d|2[0-4]\d|25[0-5])\.)(\d{1,2}|1\d\d|2[0-4]\d|25[0-5])$/,
    // IP address validation pattern for HTML pattern attribute (requires double escaping)
    HTML_PATTERN: "^(\\d{1,2}|1\\d\\d|2[0-4]\\d|25[0-5])\\.(\\d{1,2}|1\\d\\d|2[0-4]\\d|25[0-5])\\.((\\d{1,2}|1\\d\\d|2[0-4]\\d|25[0-5])\\.)(\\d{1,2}|1\\d\\d|2[0-4]\\d|25[0-5])$"
}