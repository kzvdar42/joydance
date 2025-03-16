import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod } from '/js/consts.js';

export default class IpAddressField extends Component {
    constructor(props) {
        super(props)

        let lock_host = false
        let host_ip_addr = props.host_ip_addr
        let console_ip_addr = props.console_ip_addr

        let hostname = window.location.hostname
        if (hostname.startsWith('192.168.') || hostname.startsWith('10.')) {
            host_ip_addr = hostname
            lock_host = true
        }

        this.state = {
            host_ip_addr: host_ip_addr,
            console_ip_addr: console_ip_addr,
            lock_host: lock_host,
        }

        this.onKeyPress = this.onKeyPress.bind(this)
        this.onChange = this.onChange.bind(this)
    }

    onChange(e) {
        const key = this.props.pairing_method == PairingMethod.DEFAULT ? 'host_ip_addr' : 'console_ip_addr'
        const value = e.target.value
        this.setState({
            [key]: value,
        })

        window.mitty.emit('update_addr', value)
    }

    onKeyPress(e) {
        if (!/[0-9\.]/.test(e.key)) {
            e.preventDefault()
            return
        }
    }

    componentDidMount() {
        let addr = this.props.pairing_method == PairingMethod.DEFAULT ? this.state.host_ip_addr : this.state.console_ip_addr
        window.mitty.emit('update_addr', addr)
    }

    render(props, state) {
        const pairing_method = props.pairing_method
        const addr = pairing_method == PairingMethod.DEFAULT ? state.host_ip_addr : state.console_ip_addr
        return html`
            <label>
                ${[PairingMethod.DEFAULT, PairingMethod.STADIA].indexOf(pairing_method) > -1 && html`Host Private IP`}
                ${[PairingMethod.DEFAULT, PairingMethod.STADIA].indexOf(pairing_method) == -1 && html`Console Private IP`}
            </label>

            ${(pairing_method == PairingMethod.STADIA) && html`
                <input readonly id="ipAddr" type="text" size="15" placeholder="Not Required" />
            `}

            ${(pairing_method == PairingMethod.DEFAULT && state.lock_host) && html`
                <input readonly id="ipAddr" type="text" size="15" placeholder="${addr}" />
            `}

            ${([PairingMethod.FAST, PairingMethod.OLD].indexOf(pairing_method) > -1 || (pairing_method == PairingMethod.DEFAULT && !state.lock_host)) && html`
                <input required id="ipAddr" type="text" inputmode="decimal" size="15" maxlength="15" placeholder="192.168.?/10.?" pattern="^(192\\.168|10.(\\d{1,2}|1\\d\\d|2[0-4]\\d|25[0-5]))\\.((\\d{1,2}|1\\d\\d|2[0-4]\\d|25[0-5])\\.)(\\d{1,2}|1\\d\\d|2[0-4]\\d|25[0-5])$" value=${addr} onKeyPress=${this.onKeyPress} onChange="${this.onChange}" />
            `}

        `
    }
}
