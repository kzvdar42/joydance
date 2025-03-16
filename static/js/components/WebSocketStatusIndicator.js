import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { WebSocketState } from '/js/consts.js';

export default class WebSocketStatusIndicator extends Component {
    constructor(props) {
        super(props);
        this.state = {
            status: WebSocketState.DISCONNECTED
        };

        this.checkWebSocketStatus = this.checkWebSocketStatus.bind(this);
    }

    componentDidMount() {
        window.mitty.on('ws_connected', () => this.setState({ status: WebSocketState.CONNECTED }));
        window.mitty.on('ws_reconnecting', () => this.setState({ status: WebSocketState.RECONNECTING }));
        window.mitty.on('ws_disconnected', () => this.setState({ status: WebSocketState.DISCONNECTED }));
    }

    checkWebSocketStatus() {
        switch (this.state.status) {
            case WebSocketState.CONNECTED:
                return html`<span title="Connected" class="ws-status">🟢</span>`;
            case WebSocketState.RECONNECTING:
                return html`<span title="Reconnecting..." class="ws-status">🟡</span>`;
            case WebSocketState.DISCONNECTED:
                return html`<span title="Disconnected" class="ws-status">🔴</span>`;
        }
    }

    render() {
        return html`<div class="ws-status">${this.checkWebSocketStatus()}</div>`;
    }
}
