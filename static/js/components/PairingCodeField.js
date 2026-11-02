import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod, usesPairingCode } from '/js/consts.js';

export default class PairingCodeField extends Component {
    constructor(props) {
        super(props)
        this.onChange = this.onChange.bind(this)
    }

    onChange(e) {
        const value = this.props.pairing_method === PairingMethod.JD23 ? e.target.value.toUpperCase() : e.target.value
        this.props.onChange(value)
    }

    render(props) {
        const requiresCode = usesPairingCode(props.pairing_method)
        const jd23 = props.pairing_method === PairingMethod.JD23
        return html`
            <label for="pairingCode">Pairing Code</label>
            ${requiresCode && html`
                <input required id="pairingCode" type="text"
                    inputmode=${jd23 ? 'text' : 'decimal'} autocapitalize="characters" spellcheck="false"
                    value=${props.pairing_code} placeholder=${jd23 ? 'ABCD1234' : '000000'}
                    maxlength=${jd23 ? 8 : 6} size=${jd23 ? 8 : 6}
                    pattern=${jd23 ? '[A-Za-z0-9]{8}' : '[0-9]{6}'}
                    onKeyPress=${(e) => e.key.length === 1 && !(jd23 ? /[A-Za-z0-9]/ : /[0-9]/).test(e.key) && e.preventDefault()}
                    onChange=${this.onChange} />
            `}
            ${!requiresCode && html`
                <input type="text" id="pairingCode" value="" readonly placeholder="Not Required" size="12" />
            `}
        `
    }
}
