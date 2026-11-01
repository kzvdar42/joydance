import { Component, render } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import SearchInputField from '/js/components/SearchInputField.js';
import WebSocketStatusIndicator from '/js/components/WebSocketStatusIndicator.js';
import Connections from '/js/components/Connections.js';
import { Controllers } from '/js/components/controller.js';
import { WsCommand, isPrivateIPv4, isValidIPv4, usesHostAddress, usesPairingCode } from '/js/consts.js';

window.mitty = mitt()

class App extends Component {
    constructor(props) {
        super()

        this.state = {
            connection: null,
            controllers: [],
        }
        // Websocket settings
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 100;
        this.reconnectDelay = 2000;
        this.reconnectTimer = null;
        this.socket = null;

        this.connectWs = this.connectWs.bind(this)
        this.sendRequest = this.sendRequest.bind(this)
        this.requestGetControllerList = this.requestGetControllerList.bind(this)
        this.requestConnectController = this.requestConnectController.bind(this)
        this.requestDisconnectController = this.requestDisconnectController.bind(this)
        this.handleConnectionSelect = this.handleConnectionSelect.bind(this)
        this.handleSearchInput = this.handleSearchInput.bind(this)
        this.handleToggleRumble = this.handleToggleRumble.bind(this)

        window.mitty.on('req_' + WsCommand.GET_CONTROLLER_LIST, this.requestGetControllerList)
        window.mitty.on('req_' + WsCommand.CONNECT_CONTROLLER, this.requestConnectController)
        window.mitty.on('req_' + WsCommand.DISCONNECT_CONTROLLER, this.requestDisconnectController)
        window.mitty.on('req_' + WsCommand.SEARCH_INPUT, this.handleSearchInput)
        window.mitty.on('req_' + WsCommand.TOGGLE_RUMBLE, this.handleToggleRumble)
    }

    sendRequest(cmd, data = {}) {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
            console.error('WebSocket is not connected');
            return false;
        }
        
        const msg = {
            cmd: cmd,
            data: data
        };
        console.log('Sending WebSocket message:', cmd === WsCommand.SEARCH_INPUT ? {cmd} : msg);
        this.socket.send(JSON.stringify(msg));
        return true;
    }

    requestGetControllerList() {
        if (!this.sendRequest(WsCommand.GET_CONTROLLER_LIST)) {
            window.mitty.emit('ws_disconnected');
        }
    }

    handleSearchInput(data) {
        this.sendRequest(WsCommand.SEARCH_INPUT, data);
    }

    handleToggleRumble(data) {
        this.sendRequest(WsCommand.TOGGLE_RUMBLE, data);
    }

    requestConnectController(serial) {
        const state = this.state.connection
        if (!state) {
            alert('Select a connection or add a new one first.')
            return
        }
        const pairing_method = state.pairing_method
        const addr = usesHostAddress(pairing_method) ? state.host_ip_addr : state.console_ip_addr
        if (!isValidIPv4(addr)) {
            alert('ERROR: Invalid IP address!')
            return
        }

        if (usesPairingCode(pairing_method)) {
            const pairing_code = state.pairing_code
            if (!pairing_code.match(/^\d{6}$/)) {
                alert('ERROR: Invalid pairing code!')
                return
            }
        }

        this.sendRequest(WsCommand.CONNECT_CONTROLLER, {
            pairing_method: state.pairing_method,
            host_ip_addr: state.host_ip_addr,
            console_ip_addr: state.console_ip_addr,
            pairing_code: state.pairing_code,
            controller_serial: serial,
            ...(state.console_url ? {console_url: state.console_url} : {}),
        })
    }

    requestDisconnectController(serial) {
        this.sendRequest(WsCommand.DISCONNECT_CONTROLLER, {
            controller_serial: serial,
        })
    }

    connectWs() {
        if (this.socket && this.socket.readyState !== WebSocket.CLOSED) {
            return;
        }
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
        window.mitty.emit('ws_reconnecting');
        const that = this
        const socket = new WebSocket('ws://' + window.location.host + '/ws')
        this.socket = socket

        socket.onopen = (event) => {
            if (this.socket !== socket) return;
            console.log('[open] Connection established')
            that.requestGetControllerList()
            that.sendRequest(WsCommand.GET_CONNECTIONS)
            window.mitty.emit('ws_connected');
            this.reconnectAttempts = 0;
        }

        socket.onmessage = (event) => {
            if (this.socket !== socket) return;
            const msg = JSON.parse(event.data)
            console.log('Received WebSocket message:', msg)
            const cmd = msg['cmd']
            const shortCmd = msg['cmd'].slice(5)  // Remove "resp_" prefix

            switch (shortCmd) {
                case WsCommand.GET_CONTROLLER_LIST:
                    that.setState({
                        controllers: msg['data'],
                    })
                    break
                case WsCommand.SHOW_SEARCH:
                    console.log('SHOW_SEARCH', msg['data'])
                    window.mitty.emit('show_search', msg['data'])
                    break
                case WsCommand.HIDE_SEARCH:
                    console.log('HIDE_SEARCH', msg['data'])
                    window.mitty.emit('hide_search', msg['data'])
                    break
                default:
                    window.mitty.emit(cmd, msg['data'])
            }
        }

        socket.onclose = (event) => {
            if (this.socket !== socket) return;
            this.socket = null;
            if (event.wasClean) {
                console.log(`[close] Connection closed cleanly, code=${event.code} reason=${event.reason}`);
            } else {
                console.log('[close] Connection died');
            }
            window.mitty.emit('ws_disconnected');
            this.scheduleReconnect();
        }

        socket.onerror = (error) => {
            if (this.socket !== socket) return;
            console.log(`[error] ${error.message}`);
            window.mitty.emit('ws_disconnected');
        }
    }

    scheduleReconnect() {
        if (this.reconnectTimer !== null) return;
        if (this.reconnectAttempts < this.maxReconnectAttempts) {
            let delay = this.reconnectDelay;
            // let delay = this.reconnectDelay * (2 ** this.reconnectAttempts); // Exponential backoff
            console.log(`Reconnecting in ${delay / 1000} seconds...`);

            this.reconnectTimer = setTimeout(() => {
                this.reconnectTimer = null;
                this.reconnectAttempts++;
                this.connectWs();
            }, delay);
        } else {
            console.warn('Max reconnect attempts reached. Stopping.');
        }
    }

    handleConnectionSelect(connection) {
        this.setState({connection: connection && {
            ...connection,
            host_ip_addr: isPrivateIPv4(window.location.hostname) ? window.location.hostname : connection.host_ip_addr,
        }})
    }

    componentDidMount() {
        this.connectWs()
    }

    componentWillUnmount() {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
        const socket = this.socket;
        this.socket = null;
        if (socket) socket.close();
    }

    render(props, state) {
        return html`
            <div class="container">
                <div class="ascii">
                    <pre>     ░░  ░░░░░░  ░░    ░░ ░░░░░░   ░░░░░  ░░░    ░░  ░░░░░░ ░░░░░░░
     ▒▒ ▒▒    ▒▒  ▒▒  ▒▒  ▒▒   ▒▒ ▒▒   ▒▒ ▒▒▒▒   ▒▒ ▒▒      ▒▒
     ▒▒ ▒▒    ▒▒   ▒▒▒▒   ▒▒   ▒▒ ▒▒▒▒▒▒▒ ▒▒ ▒▒  ▒▒ ▒▒      ▒▒▒▒▒
▓▓   ▓▓ ▓▓    ▓▓    ▓▓    ▓▓   ▓▓ ▓▓   ▓▓ ▓▓  ▓▓ ▓▓ ▓▓      ▓▓
 █████   ██████     ██    ██████  ██   ██ ██   ████  ██████ ███████
                    </pre>
                </div>

                <div onClick=${this.connectWs}>
                    <${WebSocketStatusIndicator} />
                </div>

                <${Connections} config=${window.CONFIG} pairing_method=${state.connection?.pairing_method || window.CONFIG.pairing_method} onSelect=${this.handleConnectionSelect} />
                <${SearchInputField} />

                <div class="pure-u-1 controllers">
                    <${Controllers} 
                        controllers=${state.controllers}
                    />
                </div>
            </div>
            <div class="footer">
                <a href="https://github.com/kzvdar42/joydance" target="_blank">${window.VERSION}</a>
            </div>
        `
    }
}

render(html`<${App} />`, document.body)
