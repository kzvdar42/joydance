import { Component, render } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import SearchInputField from '/js/components/SearchInputField.js';
import WebSocketStatusIndicator from '/js/components/WebSocketStatusIndicator.js';
import PairingMethodPicker from '/js/components/PairingMethodPicker.js';
import IpAddressField from '/js/components/IpAddressField.js';
import PairingCodeField from '/js/components/PairingCodeField.js';
import { JoyCons } from '/js/components/joycon.js';
import { PairingMethod, WsCommand, IpAddressRegex } from '/js/consts.js';

window.mitty = mitt()

class App extends Component {
    constructor(props) {
        super()

        this.state = {
            pairing_method: window.CONFIG.pairing_method,
            host_ip_addr: window.CONFIG.host_ip_addr,
            console_ip_addr: window.CONFIG.console_ip_addr,
            pairing_code: window.CONFIG.pairing_code,
            joycons: [],
        }
        // Websocket settings
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 100;
        this.reconnectDelay = 2000;

        this.connectWs = this.connectWs.bind(this)
        this.sendRequest = this.sendRequest.bind(this)
        this.requestGetJoyconList = this.requestGetJoyconList.bind(this)
        this.requestConnectJoycon = this.requestConnectJoycon.bind(this)
        this.requestDisconnectJoycon = this.requestDisconnectJoycon.bind(this)
        this.handleMethodChange = this.handleMethodChange.bind(this)
        this.handleAddrChange = this.handleAddrChange.bind(this)
        this.handleCodeChange = this.handleCodeChange.bind(this)
        this.handleSearchInput = this.handleSearchInput.bind(this)
        this.handleToggleRumble = this.handleToggleRumble.bind(this)

        window.mitty.on('req_' + WsCommand.GET_JOYCON_LIST, this.requestGetJoyconList)
        window.mitty.on('req_' + WsCommand.CONNECT_JOYCON, this.requestConnectJoycon)
        window.mitty.on('req_' + WsCommand.DISCONNECT_JOYCON, this.requestDisconnectJoycon)
        window.mitty.on('req_' + WsCommand.SEARCH_INPUT, this.handleSearchInput)
        window.mitty.on('req_' + WsCommand.TOGGLE_RUMBLE, this.handleToggleRumble)
        window.mitty.on('update_method', this.handleMethodChange)
        window.mitty.on('update_addr', this.handleAddrChange)
        window.mitty.on('update_code', this.handleCodeChange)
    }

    sendRequest(cmd, data = {}) {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
            console.error('WebSocket is not connected');
            return;
        }
        
        const msg = {
            cmd: cmd,
            data: data
        };
        console.log('Sending WebSocket message:', msg);
        this.socket.send(JSON.stringify(msg));
    }

    requestGetJoyconList() {
        this.sendRequest(WsCommand.GET_JOYCON_LIST);
    }

    handleSearchInput(data) {
        this.sendRequest(WsCommand.SEARCH_INPUT, data);
    }

    handleToggleRumble(data) {
        this.sendRequest(WsCommand.TOGGLE_RUMBLE, data);
    }

    requestConnectJoycon(serial) {
        const state = this.state
        const pairing_method = state.pairing_method
        let addr = pairing_method == PairingMethod.DEFAULT ? state.host_ip_addr : state.console_ip_addr
        if (!addr.match(IpAddressRegex.JS_PATTERN)) {
            alert('ERROR: Invalid IP address!')
            document.getElementById('ipAddr').focus()
            return
        }

        if (pairing_method == PairingMethod.DEFAULT) {
            const pairing_code = state.pairing_code
            if (!pairing_code.match(/^\d{6}$/)) {
                alert('ERROR: Invalid pairing code!')
                document.getElementById('pairingCode').focus()
                return
            }
        }

        this.sendRequest(WsCommand.CONNECT_JOYCON, {
            pairing_method: state.pairing_method,
            host_ip_addr: state.host_ip_addr,
            console_ip_addr: state.console_ip_addr,
            pairing_code: state.pairing_code,
            joycon_serial: serial,
        })
    }

    requestDisconnectJoycon(serial) {
        this.sendRequest(WsCommand.DISCONNECT_JOYCON, {
            joycon_serial: serial,
        })
    }

    connectWs() {
        window.mitty.emit('ws_reconnecting');
        const that = this
        this.socket = new WebSocket('ws://' + window.location.host + '/ws')

        this.socket.onopen = (event) => {
            console.log('[open] Connection established')
            that.requestGetJoyconList()
            window.mitty.emit('ws_connected');
            this.reconnectAttempts = 0;
        }

        this.socket.onmessage = (event) => {
            const msg = JSON.parse(event.data)
            console.log('Received WebSocket message:', msg)
            const cmd = msg['cmd']
            const shortCmd = msg['cmd'].slice(5)  // Remove "resp_" prefix

            switch (shortCmd) {
                case WsCommand.GET_JOYCON_LIST:
                    that.setState({
                        joycons: msg['data'],
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

        this.socket.onclose = (event) => {
            if (event.wasClean) {
                console.log(`[close] Connection closed cleanly, code=${event.code} reason=${event.reason}`);
            } else {
                console.log('[close] Connection died');
            }
            window.mitty.emit('ws_disconnected');
            this.scheduleReconnect();
        }

        this.socket.onerror = (error) => {
            console.log(`[error] ${error.message}`);
            window.mitty.emit('ws_disconnected');
            this.scheduleReconnect();
        }
    }

    scheduleReconnect() {
        if (this.reconnectAttempts < this.maxReconnectAttempts) {
            let delay = this.reconnectDelay;
            // let delay = this.reconnectDelay * (2 ** this.reconnectAttempts); // Exponential backoff
            console.log(`Reconnecting in ${delay / 1000} seconds...`);

            setTimeout(() => {
                this.reconnectAttempts++;
                this.connectWs();
            }, delay);
        } else {
            console.warn('Max reconnect attempts reached. Stopping.');
        }
    }

    handleMethodChange(pairing_method) {
        this.setState({
            pairing_method: pairing_method,
        })
    }

    handleAddrChange(addr) {
        const key = this.state.pairing_method == PairingMethod.DEFAULT ? 'host_ip_addr' : 'console_ip_addr'
        this.setState({
            [key]: addr,
        })
    }

    handleCodeChange(pairing_code) {
        this.setState({
            pairing_code: pairing_code,
        })
    }

    componentDidMount() {
        this.connectWs()
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

                <form class="pure-form pure-form-stacked">
                    <fieldset>
                        <div class="pure-g">
                            <h2 class="pure-u-1">Config</h2>
                            <div class="pure-u-1">
                                <${PairingMethodPicker} pairing_method=${state.pairing_method}/>
                            </div>
                            <div class="pure-u-1-2">
                                <${IpAddressField} pairing_method=${state.pairing_method} host_ip_addr=${state.host_ip_addr} console_ip_addr=${state.console_ip_addr} />
                            </div>
                            <div class="pure-u-1-2">
                                <${PairingCodeField} pairing_method=${state.pairing_method} pairing_code=${state.pairing_code} />
                            </div>
                            <div class="pure-u-1">
                                <${SearchInputField} />
                            </div>
                        </div>
                    </fieldset>
                </form>

                <div class="pure-u-1 joycons">
                    <${JoyCons} 
                        pairing_method=${state.pairing_method} 
                        joycons=${state.joycons} 
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
