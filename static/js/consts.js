export const PairingMethod = {
    DEFAULT: 'default',
    FAST: 'fast',
    STADIA: 'stadia',
    OLD: 'old',
    JDMO: 'jdmo',
}

export const usesHostAddress = method => [PairingMethod.DEFAULT, PairingMethod.JDMO].includes(method)
export const usesPairingCode = method => [PairingMethod.DEFAULT, PairingMethod.STADIA, PairingMethod.JDMO].includes(method)

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

export const isPrivateIPv4 = address => {
    const octets = address.split('.')
    if (!IpAddressRegex.JS_PATTERN.test(address) || octets.some(octet => String(Number(octet)) !== octet)) {
        return false
    }
    const [first, second] = octets.map(Number)
    return first === 10 || (first === 172 && second >= 16 && second <= 31) || (first === 192 && second === 168)
}
