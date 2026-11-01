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
    GET_CONNECTIONS: 'get_connections',
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

const ipv4Octet = '(?:25[0-5]|2[0-4]\\d|1\\d{2}|[1-9]?\\d)'
const ipv4Regex = new RegExp(`^(?:${ipv4Octet}\\.){3}${ipv4Octet}$`)

export const IpAddressRegex = {
    JS_PATTERN: ipv4Regex,
    HTML_PATTERN: ipv4Regex.source,
}

// Require the whole string: JavaScript's $ anchor also allows a final line break.
export const isValidIPv4 = address =>
    typeof address === 'string' && IpAddressRegex.JS_PATTERN.exec(address)?.[0] === address

export const isPrivateIPv4 = address => {
    if (!isValidIPv4(address)) {
        return false
    }
    const [first, second] = address.split('.').map(Number)
    return first === 10 || (first === 172 && second >= 16 && second <= 31) || (first === 192 && second === 168)
}
