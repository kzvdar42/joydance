import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod, IpAddressRegex, isPrivateIPv4, usesHostAddress } from '/js/consts.js';

export default class IpAddressField extends Component {
    constructor(props) {
        super(props)

        this.state = {
            lock_host: isPrivateIPv4(window.location.hostname),
        }

        this.onKeyPress = this.onKeyPress.bind(this)
        this.onChange = this.onChange.bind(this)
    }

    onChange(e) {
        const value = e.target.value
        this.props.onChange(value)
    }

    onKeyPress(e) {
        if (e.key.length === 1 && !/[0-9\.]/.test(e.key)) {
            e.preventDefault()
            return
        }
    }

    componentDidMount() {
        const host = this.state.lock_host ? window.location.hostname : this.props.host_ip_addr
        const addr = usesHostAddress(this.props.pairing_method) ? host : this.props.console_ip_addr
        this.props.onChange(addr)
    }

    render(props, state) {
        const pairing_method = props.pairing_method
        const hostAddress = usesHostAddress(pairing_method)
        const addr = hostAddress ? props.host_ip_addr : props.console_ip_addr
        return html`
            <label for="ipAddr">
                ${(hostAddress || pairing_method == PairingMethod.STADIA) && html`Host Private IP`}
                ${(!hostAddress && pairing_method != PairingMethod.STADIA) && html`Console Private IP`}
            </label>

            ${(pairing_method == PairingMethod.STADIA) && html`
                <input readonly id="ipAddr" type="text" size="15" placeholder="Not Required" />
            `}

            ${(hostAddress && state.lock_host) && html`
                <input readonly id="ipAddr" type="text" size="15" value=${addr} />
            `}

            ${([PairingMethod.FAST, PairingMethod.OLD].indexOf(pairing_method) > -1 || (hostAddress && !state.lock_host)) && html`
                <input required id="ipAddr" type="text" inputmode="decimal" size="15" maxlength="15" placeholder="e.g. 172.16.0.2" pattern="${IpAddressRegex.HTML_PATTERN}" value=${addr} onKeyPress=${this.onKeyPress} onChange="${this.onChange}" />
            `}

        `
    }
}
