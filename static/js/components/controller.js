import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { WsCommand } from '/js/consts.js';
import ControllerIcon from '/js/components/ControllerIcon.js';

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

    ERROR_CONTROLLER: 101,
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

    [PairingState.ERROR_CONTROLLER]: 'Controller problem!',
    [PairingState.ERROR_CONNECTION]: 'Couldn\'t get auth token!',
    [PairingState.ERROR_INVALID_PAIRING_CODE]: 'Invalid pairing code!',
    [PairingState.ERROR_PUNCH_PAIRING]: 'Couldn\'t punch pairing!',
    [PairingState.ERROR_HOLE_PUNCHING]: 'Couldn\'t connect with console!',
    [PairingState.ERROR_CONSOLE_CONNECTION]: 'Couldn\'t connect with console!',
}

class Controller extends Component {
    constructor(props) {
        super(props)

        this.connect = this.connect.bind(this)
        this.disconnect = this.disconnect.bind(this)
        this.onStateUpdated = this.onStateUpdated.bind(this)
        this.toggleRumble = this.toggleRumble.bind(this)

        this.state = {
            controller: props.controller,
        }
    }

    componentDidMount() {
        window.mitty.on('resp_' + WsCommand.UPDATE_CONTROLLER_STATE, this.onStateUpdated)
    }

    componentDidUpdate(prevProps) {
        if (prevProps.controller !== this.props.controller) {
            this.setState({ controller: this.props.controller })
        }
    }

    componentWillUnmount() {
        window.mitty.off('resp_' + WsCommand.UPDATE_CONTROLLER_STATE, this.onStateUpdated)
    }

    connect() {
        window.mitty.emit('req_' + WsCommand.CONNECT_CONTROLLER, this.props.controller.serial)
    }

    disconnect() {
        window.mitty.emit('req_' + WsCommand.DISCONNECT_CONTROLLER, this.props.controller.serial)
    }

    toggleRumble() {
        const newState = !this.state.controller.rumble_enabled
        window.mitty.emit('req_' + WsCommand.TOGGLE_RUMBLE, {
            controller_serial: this.props.controller.serial,
            enabled: newState
        })
    }

    onStateUpdated(data) {
        if (data['serial'] != this.props.controller.serial) {
            return
        }

        const state = data['state']
        if (PairingStateMessage.hasOwnProperty(state)) {
            this.setState(({ controller }) => ({
                controller: { ...controller, ...data },
            }))
        }
    }

    render(props, { controller }) {
        const { name, state, connection_requested, pairing_code, battery_level, rumble_enabled, player_name, player_id, player_color, player_image, skin_image, additional_message, is_main_player, error_details } = controller
        const controllerState = state
        const stateMessage = controllerState >= PairingState.ERROR_CONTROLLER && error_details
            ? error_details : PairingStateMessage[controllerState]

        const batteryLevel = BATTERY_LEVEL[battery_level]

        return html`
            <li>
                <div class="pure-g">

                    <div class="pure-u-2-24 flex"><${ControllerIcon} ...${controller} /></div>
                    <div class="pure-u-12-24 controller-info">
                        <div class="flex">
                            <span class="controller-name">${name}</span>
                            <span class="battery-level ${batteryLevel}">${SVG_BATTERY_LEVEL}</span>
                        </div>
                        <span class="controller-state">${stateMessage}</span>
                        ${player_name && html`
                            <div class="player-info">
                                ${Array.isArray(player_color) && player_color.length === 4 && html`
                                    <span class="player-color" style="background-color: rgba(${player_color[0]}, ${player_color[1]}, ${player_color[2]}, ${player_color[3]})"></span>
                                `}
                                <span>P${player_id}: ${player_name}</span>
                                ${controllerState == PairingState.CONNECTED && is_main_player && html`<span class="player-role">Main</span>`}
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
                        ${controllerState == PairingState.CONNECTED && html`
                            <div class="button-group">
                                <button type="button" onClick=${this.disconnect} class="pure-button pure-button-error">Disconnect</button>
                                <button type="button" onClick=${this.toggleRumble} class="pure-button ${rumble_enabled ? 'pure-button-primary' : 'pure-button-secondary'}">
                                    Rumble ${rumble_enabled ? 'On' : 'Off'}
                                </button>
                            </div>
                        `}
                        ${connection_requested && controllerState != PairingState.CONNECTED && html`
                            <button type="button" onClick=${this.disconnect} class="pure-button pure-button-error">Stop</button>
                        `}
                        ${!connection_requested && controllerState != PairingState.CONNECTED && html`
                            <button type="button" onClick=${this.connect} class="pure-button pure-button-primary">Connect</button>
                        `}
                    </div>
                </div>
            </li>
        `
    }
}

export class Controllers extends Component {
    constructor() {
        super()
        this.state = {
            isRefreshing: false,
        }

        this.refreshControllerList = this.refreshControllerList.bind(this)
        this.finishRefresh = this.finishRefresh.bind(this)
    }

    refreshControllerList() {
        this.setState({
            isRefreshing: true,
        })
        window.mitty.emit('req_' + WsCommand.GET_CONTROLLER_LIST)
    }

    finishRefresh() {
        this.setState({ isRefreshing: false })
    }

    componentDidMount() {
        window.mitty.on('ws_disconnected', this.finishRefresh)
    }

    componentDidUpdate(prevProps) {
        if (this.state.isRefreshing && prevProps.controllers !== this.props.controllers) {
            this.finishRefresh()
        }
    }

    componentWillUnmount() {
        window.mitty.off('ws_disconnected', this.finishRefresh)
    }

    render(props, state) {
        return html`
            <div class="pure-g">
                <h2 class="pure-u-18-24">Controllers</h2>
                <button type="button" disabled=${state.isRefreshing} aria-busy=${state.isRefreshing} class="pure-button btn-refresh pure-u-6-24" onClick=${this.refreshControllerList}>Refresh</button>
            </div>
            <div class="controllers-wrapper">
                ${props.controllers.length == 0 && html`
                    <p class="empty">No controllers found!</p>
                `}


                ${props.controllers.length > 0 && html`
                    <ul class="controllers-list">
                        ${props.controllers.map(item => (
                            html`<${Controller} controller=${item} key=${item.serial} />`
                        ))}
                    </ul>
                `}
            </div>
        `
    }
}
