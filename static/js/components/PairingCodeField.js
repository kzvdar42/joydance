import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod, pairingCodeCharacters, pairingCodePattern, pairingCodeRule } from '/js/consts.js';

export default class PairingCodeField extends Component {
    constructor(props) {
        super(props)
        this.onChange = this.onChange.bind(this)
    }

    onChange(e) {
        const rule = pairingCodeRule(this.props.pairing_method)
        const value = rule?.uppercase ? e.target.value.toUpperCase() : e.target.value
        this.props.onChange(value)
    }

    render(props) {
        const rule = pairingCodeRule(props.pairing_method)
        const jdnow = props.pairing_method === PairingMethod.JDNOW
        const characters = rule && new RegExp(`^${pairingCodeCharacters(rule)}$`, 'u')
        return html`
            <label for="pairingCode">${jdnow ? 'Dance Room number' : 'Pairing Code'}</label>
            ${rule && html`
                <input required id="pairingCode" type="text"
                    inputmode=${rule.uppercase ? 'text' : 'decimal'} autocapitalize="characters" spellcheck="false"
                    value=${props.pairing_code} placeholder=${rule.uppercase ? 'ABCD1234' : jdnow ? 'e.g. 34017' : '000000'}
                    maxlength=${rule.max_length} size=${rule.max_length} pattern=${pairingCodePattern(rule)}
                    onKeyPress=${e => e.key.length === 1 && !characters.test(e.key) && e.preventDefault()}
                    onChange=${this.onChange} />
            `}
            ${!rule && html`
                <input type="text" id="pairingCode" value="" readonly placeholder="Not Required" size="12" />
            `}
        `
    }
}
