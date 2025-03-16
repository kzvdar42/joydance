import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod } from '/js/consts.js';

export default class PairingCodeField extends Component {
    constructor(props) {
        super(props)
        this.state = {
            pairing_code: props.pairing_code,
        }

        this.onChange = this.onChange.bind(this)
    }

    onChange(e) {
        const value = e.target.value
        this.setState({
            pairing_code: value,
        })

        window.mitty.emit('update_code', value)
    }

    render(props, state) {
        const pairing_method = props.pairing_method
        return html`
            <label>Pairing Code</label>
            ${[PairingMethod.DEFAULT, PairingMethod.STADIA].indexOf(pairing_method) > -1 && html`
                <input required id="pairingCode" type="text" inputmode="decimal" value=${state.pairing_code} placeholder="000000" maxlength="6" size="6" pattern="[0-9]{6}" onKeyPress=${(e) => !/[0-9]/.test(e.key) && e.preventDefault()} onChange=${this.onChange} />
            `}
            ${[PairingMethod.DEFAULT, PairingMethod.STADIA].indexOf(pairing_method) == -1 && html`
                <input type="text" id="pairingCode" value="" readonly placeholder="Not Required" size="12" />
            `}
        `
    }
}
