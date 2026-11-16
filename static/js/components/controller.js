import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { WsCommand } from '/js/consts.js';
import ControllerIcon from '/js/components/ControllerIcon.js';
import ActionIcon from '/js/components/ActionIcon.js';
import ControllerSettings from '/js/components/ControllerSettings.js';

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
        this.onPlayerRenamed = this.onPlayerRenamed.bind(this)
        this.toggleRumble = this.toggleRumble.bind(this)

        this.state = {
            controller: props.controller,
            guestChoice: null,
            playerName: null,
            songQuery: '',
            showSongResults: false,
            signingIn: false,
            signInFeedback: null,
        }
    }

    componentDidMount() {
        window.mitty.on('resp_' + WsCommand.UPDATE_CONTROLLER_STATE, this.onStateUpdated)
        window.mitty.on('resp_' + WsCommand.RENAME_PLAYER, this.onPlayerRenamed)
    }

    componentDidUpdate(prevProps) {
        if (prevProps.controller !== this.props.controller) {
            this.setState({ controller: this.props.controller })
        }
    }

    componentWillUnmount() {
        window.mitty.off('resp_' + WsCommand.UPDATE_CONTROLLER_STATE, this.onStateUpdated)
        window.mitty.off('resp_' + WsCommand.RENAME_PLAYER, this.onPlayerRenamed)
    }

    selectedGuest() {
        const { controller, guestChoice } = this.state
        if (guestChoice === 'new' || controller.jdnow_guests?.some(guest => guest.key === guestChoice)) {
            return guestChoice
        }
        return controller.jdnow_guest ?? 'new'
    }

    connect() {
        const serial = this.props.controller.serial
        window.mitty.emit('req_' + WsCommand.CONNECT_CONTROLLER,
            this.props.jdnow ? {serial, guest: this.selectedGuest()} : serial)
        this.setState({guestChoice: null, playerName: null, showSongResults: false})
    }

    disconnect() {
        window.mitty.emit('req_' + WsCommand.DISCONNECT_CONTROLLER, this.props.controller.serial)
    }

    async signIn() {
        this.setState({signingIn: true, signInFeedback: null})
        try {
            const response = await fetch('/jdnow/sign-in', {method: 'POST'})
            const data = await response.json()
            this.setState({signInFeedback: response.ok
                ? {text: 'Profile imported. Choose it from the picker.'}
                : {text: data.error, error: true}})
        } catch {
            this.setState({signInFeedback: {text: 'Could not complete Ubisoft sign-in.', error: true}})
        } finally {
            this.setState({signingIn: false})
        }
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

    onPlayerRenamed({serial, submitted_name}) {
        if (serial === this.props.controller.serial) {
            this.setState(({playerName}) => playerName === submitted_name ? {playerName: null} : null)
        }
    }

    render(props, { controller, playerName, songQuery, showSongResults, signingIn, signInFeedback }) {
        const { name, state, connection_requested, pairing_code, battery_level, rumble_enabled, player_name, player_id, player_color, player_image, skin_image, additional_message, is_main_player, error_details } = controller
        const controllerState = state
        const stateMessage = controllerState >= PairingState.ERROR_CONTROLLER && error_details
            ? error_details : PairingStateMessage[controllerState]

        const batteryLevel = BATTERY_LEVEL[battery_level]
        const songs = showSongResults ? controller.song_search_results || [] : []
        const total = showSongResults ? controller.song_search_total : null
        const searchFeedback = showSongResults && controller.song_search_feedback
        const renameFeedback = controller.player_rename_feedback
        const canRename = controllerState == PairingState.CONNECTED && controller.supports_player_rename
        let searchStatus = ''
        if (total === 0) {
            searchStatus = 'No songs match. Try another title or artist.'
        } else if (total > songs.length) {
            searchStatus = `Showing ${songs.length} of ${total} songs`
        } else if (total > 0) {
            searchStatus = `${total} ${total === 1 ? 'song' : 'songs'} found`
        }

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
                                ${additional_message && html`<span class="controller-message">${additional_message}</span>`}
                            </div>
                        `}
                    </div>
                    <div class="pure-u-4-24 flex">
                        ${pairing_code && html`
                            <span class="pairing-code">${pairing_code}</span>
                        `}
                    </div>
                    <div class="controller-actions">
                        ${controllerState == PairingState.CONNECTED && html`
                            <div class="button-group">
                                <button type="button" onClick=${this.disconnect} class="pure-button pure-button-error">Disconnect</button>
                            </div>
                        `}
                        ${connection_requested && controllerState != PairingState.CONNECTED && html`
                            <button type="button" onClick=${this.disconnect} class="pure-button pure-button-error">Stop</button>
                        `}
                        ${!connection_requested && controllerState != PairingState.CONNECTED && html`
                            <button type="button" onClick=${this.connect} disabled=${signingIn} class="pure-button pure-button-primary">Connect</button>
                        `}
                        <div class="controller-utility-actions">
                            <button type="button" onClick=${this.toggleRumble}
                                class="pure-button icon-button controller-rumble-button ${rumble_enabled ? 'pure-button-primary' : 'pure-button-secondary'}"
                                aria-label=${`${name} rumble`} aria-pressed=${Boolean(rumble_enabled)} title=${rumble_enabled ? 'Rumble on' : 'Rumble off'}>
                                <${ActionIcon} name=${rumble_enabled ? 'rumble' : 'rumble-off'} />
                            </button>
                            ${controller.motion_settings && html`
                                <button type="button" class="pure-button icon-button controller-settings-button" aria-haspopup="dialog"
                                    aria-label=${`${name} settings`} title="Settings"
                                    onClick=${() => this.settings.open()}><${ActionIcon} name="settings" /></button>
                            `}
                        </div>
                    </div>
                </div>
                ${controller.motion_settings && html`
                    <${ControllerSettings} controller=${controller} ref=${settings => this.settings = settings} />
                `}
                ${props.jdnow && !connection_requested && controllerState !== PairingState.CONNECTED && html`
                    <div class="pure-form controller-tools">
                        <label>JDNow profile
                            <select value=${this.selectedGuest()} disabled=${signingIn} aria-busy=${signingIn}
                                onChange=${event => {
                                    if (event.target.value === 'sign-in') {
                                        event.target.value = this.selectedGuest()
                                        return this.signIn()
                                    }
                                    this.setState({guestChoice: event.target.value})
                                }}>
                                ${(controller.jdnow_guests || []).map(guest => {
                                    const others = guest.controllers.filter(serial => serial !== controller.serial)
                                    const label = others.length ? `${guest.name} · Saved for ${others.join(', ')}` : guest.name
                                    return html`<option value=${guest.key}>${label}</option>`
                                })}
                                <option value="new">New guest</option>
                                ${['localhost', '127.0.0.1'].includes(window.location.hostname) && html`
                                    <option value="sign-in">Sign in with Ubisoft…</option>
                                `}
                            </select>
                        </label>
                    </div>
                    ${signingIn && html`<p class="controller-field-status" role="status">Finish signing in in the Ubisoft window.</p>`}
                    ${signInFeedback && html`
                        <p class="controller-field-status" role=${signInFeedback.error ? 'alert' : 'status'}>${signInFeedback.text}</p>
                    `}
                `}
                ${(canRename || renameFeedback?.error) && html`
                    <section aria-label="Player rename">
                        ${canRename && html`<form class="pure-form controller-tools" onSubmit=${event => {
                            event.preventDefault()
                            window.mitty.emit('req_' + WsCommand.RENAME_PLAYER, {
                                controller_serial: controller.serial, name: playerName ?? player_name ?? ''
                            })
                        }}>
                            <label>Player name
                                <input type="text" maxlength="20" required value=${playerName ?? player_name ?? ''}
                                    onInput=${event => this.setState({playerName: event.target.value})} />
                            </label>
                            <button type="submit" class="pure-button">Rename</button>
                        </form>`}
                        ${renameFeedback && html`
                            <p class="controller-field-status" role=${renameFeedback.error ? 'alert' : 'status'}>${renameFeedback.text}</p>
                        `}
                    </section>
                `}
                ${controllerState == PairingState.CONNECTED && controller.supports_song_search && html`
                    <section aria-label="Song search">
                        <form class="pure-form controller-tools" onSubmit=${event => {
                            event.preventDefault()
                            if (!controller.song_search_enabled) return
                            const text = songQuery.trim()
                            if (!text) {
                                const field = event.currentTarget.elements.songQuery
                                field.setCustomValidity('Enter a song title or artist.')
                                field.reportValidity()
                                return
                            }
                            this.setState({showSongResults: true})
                            if (this.songResults) this.songResults.scrollTop = 0
                            window.mitty.emit('req_' + WsCommand.SEARCH_INPUT, {
                                controller_serial: controller.serial, text
                            })
                        }}>
                            <label>Song title or artist
                                <input type="search" name="songQuery" maxlength="100" aria-required="true" value=${songQuery}
                                    disabled=${!controller.song_search_enabled}
                                    onInput=${event => {
                                        event.target.setCustomValidity('')
                                        const text = event.target.value
                                        const cleared = !text.trim()
                                        this.setState({songQuery: text, showSongResults: showSongResults && !cleared})
                                        if (cleared && showSongResults && controller.song_search_enabled) {
                                            window.mitty.emit('req_' + WsCommand.SEARCH_INPUT, {
                                                controller_serial: controller.serial, text: ''
                                            })
                                        }
                                    }} />
                            </label>
                            <button type="submit" class="pure-button pure-button-primary"
                                disabled=${!controller.song_search_enabled}>Search</button>
                        </form>
                        ${searchFeedback && html`
                            <p class="controller-field-status" role=${searchFeedback.error ? 'alert' : 'status'}>${searchFeedback.text}</p>
                        `}
                        <p class="controller-field-status" role="status">${searchStatus}</p>
                        ${songs.length > 0 && html`
                            <div class="controller-song-results-panel">
                                <ul class="controller-song-results" aria-label="Song search results"
                                    ref=${element => this.songResults = element}>
                                    ${songs.map(song => html`
                                        <li key=${song.id}>
                                            <button type="button" class="controller-song-result" disabled=${!controller.song_search_enabled}
                                                title="Show in All Songs"
                                                onClick=${() => window.mitty.emit('req_' + WsCommand.SELECT_SONG, {
                                                    controller_serial: controller.serial, song: song.id
                                                })}>
                                                <span class="controller-song-details">
                                                    <span class="controller-song-title">${song.name}</span>
                                                    ${song.artist && html`<span class="controller-song-artist">${song.artist}</span>`}
                                                </span>
                                                <span class="controller-song-arrow" aria-hidden="true">›</span>
                                            </button>
                                        </li>
                                    `)}
                                </ul>
                            </div>
                        `}
                    </section>
                `}
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
                <button type="button" disabled=${state.isRefreshing} aria-busy=${state.isRefreshing}
                    class="pure-button icon-button btn-refresh" aria-label="Refresh controllers"
                    title=${state.isRefreshing ? 'Refreshing controllers…' : 'Refresh controllers'}
                    onClick=${this.refreshControllerList}><${ActionIcon} name=${state.isRefreshing ? 'loading' : 'refresh'} /></button>
            </div>
            <div class="controllers-wrapper">
                ${props.controllers.length == 0 && html`
                    <p class="empty">No controllers found!</p>
                `}


                ${props.controllers.length > 0 && html`
                    <ul class="controllers-list">
                        ${props.controllers.map(item => (
                            html`<${Controller} controller=${item} jdnow=${props.jdnow} key=${item.serial} />`
                        ))}
                    </ul>
                `}
            </div>
        `
    }
}
