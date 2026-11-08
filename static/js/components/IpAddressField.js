import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { IpAddressRegex, isPrivateIPv4, pairingAddressField, usesHostAddress } from '/js/consts.js';

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
        const field = pairingAddressField(this.props.pairing_method)
        if (field) {
            const locked = usesHostAddress(this.props.pairing_method) && this.state.lock_host
            this.props.onChange(locked ? window.location.hostname : this.props[field])
        }
    }

    render(props, state) {
        const field = pairingAddressField(props.pairing_method)
        if (!field) return null
        const hostAddress = usesHostAddress(props.pairing_method)
        const locked = hostAddress && state.lock_host
        return html`
            <label for="ipAddr">${hostAddress ? 'Host Private IP' : 'Console Private IP'}</label>
            <input id="ipAddr" type="text" inputmode="decimal" size="15" maxlength="15"
                required=${!locked} readonly=${locked} placeholder="e.g. 172.16.0.2"
                pattern=${IpAddressRegex.HTML_PATTERN} value=${props[field]}
                onKeyPress=${this.onKeyPress} onChange=${this.onChange} />
        `
    }
}
