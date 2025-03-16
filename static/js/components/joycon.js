import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { WsCommand } from '/js/consts.js';

const SVG_BATTERY_LEVEL = html`<svg style="enable-background:new 0 0 16 16" viewBox="0 0 16 16" xml:space="preserve" xmlns="http://www.w3.org/2000/svg"><path d="M15 4H0v8h15V9h1V7h-1V4zm-1 3v4H1V5h13v2z"/><rect class="battery-bar-4" height="4" width="2" x="11" y="6"/><rect class="battery-bar-3" height="4" width="2" x="8" y="6"/><rect class="battery-bar-2" height="4" width="2" x="5" y="6"/><rect class="battery-bar-1" height="4" width="2" x="2" y="6"/></svg>`

const BATTERY_LEVEL = {
    4: 'full',
    3: 'medium',
    2: 'low',
    1: 'critical',
    0: 'critical',
}

const PairingState = {
    IDLE: 0,
    GETTING_TOKEN: 1,
    PAIRING: 2,
    CONNECTING: 3,
    CONNECTED: 4,
    DISCONNECTING: 5,
    DISCONNECTED: 10,

    ERROR_JOYCON: 101,
    ERROR_CONNECTION: 102,
    ERROR_INVALID_PAIRING_CODE: 103,
    ERROR_PUNCH_PAIRING: 104,
    ERROR_HOLE_PUNCHING: 105,
    ERROR_CONSOLE_CONNECTION: 106,
}

const PairingStateMessage = {
    [PairingState.IDLE]: 'Idle',
    [PairingState.GETTING_TOKEN]: 'Getting auth token...',
    [PairingState.PAIRING]: 'Sending pairing code...',
    [PairingState.CONNECTING]: 'Connecting with console...',
    [PairingState.CONNECTED]: 'Connected!',
    [PairingState.DISCONNECTED]: 'Disconnected',

    [PairingState.ERROR_JOYCON]: 'Joy-Con problem!',
    [PairingState.ERROR_CONNECTION]: 'Couldn\'t get auth token!',
    [PairingState.ERROR_INVALID_PAIRING_CODE]: 'Invalid pairing code!',
    [PairingState.ERROR_PUNCH_PAIRING]: 'Couldn\'t punch pairing!',
    [PairingState.ERROR_HOLE_PUNCHING]: 'Couldn\'t connect with console!',
    [PairingState.ERROR_CONSOLE_CONNECTION]: 'Couldn\'t connect with console!',
}

class JoyCon extends Component {
    constructor(props) {
        super(props)

        this.connect = this.connect.bind(this)
        this.disconnect = this.disconnect.bind(this)
        this.onStateUpdated = this.onStateUpdated.bind(this)
        this.toggleRumble = this.toggleRumble.bind(this)

        this.state = {
            ...props.joycon,
        }

        window.mitty.on('resp_' + WsCommand.DISCONNECT_JOYCON, this.onDisconnected)
        window.mitty.on('resp_' + WsCommand.UPDATE_JOYCON_STATE, this.onStateUpdated)
    }

    connect() {
        window.mitty.emit('req_' + WsCommand.CONNECT_JOYCON, this.props.joycon.serial)
    }

    disconnect() {
        window.mitty.emit('req_' + WsCommand.DISCONNECT_JOYCON, this.props.joycon.serial)
    }

    toggleRumble() {
        const newState = !this.state.rumble_enabled
        window.mitty.emit('req_' + WsCommand.TOGGLE_RUMBLE, {
            joycon_serial: this.props.joycon.serial,
            enabled: newState
        })
    }

    onStateUpdated(data) {
        if (data['serial'] != this.props.joycon.serial) {
            return
        }

        const state = data['state']
        if (PairingStateMessage.hasOwnProperty(state)) {
            this.setState({
                ...data,
            })
        }
    }

    render(props, { name, state, pairing_code, is_left, color, battery_level, rumble_enabled, player_name, player_id, player_color, player_image, skin_image, additional_message }) {
        const joyconState = state
        const stateMessage = PairingStateMessage[joyconState]
        let showButton = true
        if ([PairingState.GETTING_TOKEN, PairingState.PAIRING, PairingState.CONNECTING].indexOf(joyconState) > -1) {
            showButton = false
        }

        let joyconSvg
        if (is_left) {
            joyconSvg = html`<svg class="joycon-color" viewBox="0 0 171 453" xmlns="http://www.w3.org/2000/svg" xml:space="preserve" style="fill-rule:evenodd;clip-rule:evenodd"><path d="M219.594 33.518v412.688c0 1.023-.506 1.797-1.797 1.797h-49.64c-51.68 0-85.075-45.698-85.075-85.075V114.987c0-57.885 56.764-84.719 84.719-84.719h48.79c2.486 0 3.003 1.368 3.003 3.25zm-32.123 105.087c0 17.589-14.474 32.062-32.063 32.062-17.589 0-32.062-14.473-32.062-32.062s14.473-32.063 32.062-32.063 32.063 14.474 32.063 32.063z" style="fill:${color};stroke:#000;stroke-width:8.33px" transform="translate(-65.902 -13.089)"/></svg>`
        } else {
            joyconSvg = html`<svg class="joycon-color" viewBox="0 0 171 453" xmlns="http://www.w3.org/2000/svg" xml:space="preserve" style="fill-rule:evenodd;clip-rule:evenodd"><path d="M324.763 40.363v412.688c0 1.023.506 1.797 1.797 1.797h49.64c51.68 0 85.075-45.698 85.075-85.075V121.832c0-6.774-.777-13.123-2.195-19.054-10.696-44.744-57.841-65.665-82.524-65.665h-48.79c-2.486 0-3.003 1.368-3.003 3.25zm96 218.094c0 17.589-14.473 32.063-32.062 32.063s-32.063-14.474-32.063-32.063c0-17.589 14.474-32.062 32.063-32.062 17.589 0 32.062 14.473 32.062 32.062z" style="fill:${color};fill-rule:nonzero;stroke:#000;stroke-width:8.33px" transform="translate(-307.583 -19.934)"/></svg>`
        }

        const batteryLevel = BATTERY_LEVEL[battery_level]

        return html`
            <li>
                <div class="pure-g">

                    <div class="pure-u-2-24 flex">${joyconSvg}</div>
                    <div class="pure-u-12-24 joycon-info">
                        <div class="flex">
                            <span class="joycon-name">${name}</span>
                            <span class="battery-level ${batteryLevel}">${SVG_BATTERY_LEVEL}</span>
                        </div>
                        <span class="joycon-state">${stateMessage}</span>
                        ${player_name && html`
                            <div class="player-info">
                                <span class="player-color" style="background-color: rgba(${player_color[0]}, ${player_color[1]}, ${player_color[2]}, ${player_color[3]})"></span>
                                <span>P${player_id}: ${player_name}</span>
                                ${additional_message && html`<span>${additional_message}</span>`}
                            </div>
                        `}
                    </div>
                    <div class="pure-u-4-24 flex">
                        ${pairing_code && html`
                            <span class="pairing-code">${pairing_code}</span>
                        `}
                    </div>
                    <div class="pure-u-6-24">
                        ${showButton && joyconState == PairingState.CONNECTED && html`
                            <div class="button-group">
                                <button type="button" onClick=${this.disconnect} class="pure-button pure-button-error">Disconnect</button>
                                <button type="button" onClick=${this.toggleRumble} class="pure-button ${rumble_enabled ? 'pure-button-primary' : 'pure-button-secondary'}">
                                    Rumble ${rumble_enabled ? 'On' : 'Off'}
                                </button>
                            </div>
                        `}
                        ${showButton && joyconState != PairingState.CONNECTED && html`
                            <button type="button" onClick=${this.connect} class="pure-button pure-button-primary">Connect</button>
                        `}
                    </div>
                </div>
            </li>
        `
    }
}

export class JoyCons extends Component {
    constructor() {
        super()
        this.state = {
            isRefreshing: false,
        }

        this.refreshJoyconList = this.refreshJoyconList.bind(this)
    }

    refreshJoyconList() {
        this.setState({
            isRefreshing: false,
        })
        window.mitty.emit('req_' + WsCommand.GET_JOYCON_LIST)
    }

    componentDidMount() {
    }

    render(props, state) {
        return html`
            <div class="pure-g">
                <h2 class="pure-u-18-24">Joy-Cons</h2>
                    ${state.isRefreshing && html`
                        <button type="button" disabled class="pure-button btn-refresh pure-u-6-24">Refresh</a>
                    `}
                    ${!state.isRefreshing && html`
                        <button type="button" class="pure-button btn-refresh pure-u-6-24" onClick=${this.refreshJoyconList}>Refresh</button>
                    `}
            </div>
            <div class="joycons-wrapper">
                ${props.joycons.length == 0 && html`
                    <p class="empty">No Joy-Cons found!</p>
                `}


                ${props.joycons.length > 0 && html`
                    <ul class="joycons-list">
                        ${props.joycons.map(item => (
                            html`<${JoyCon} joycon=${item} key=${item.serial} />`
                        ))}
                    </ul>
                `}
            </div>
        `
    }
}
