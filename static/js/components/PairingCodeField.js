import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod, formatPairingCode, pairingCodeCharacters, pairingCodePattern, pairingCodeRule } from '/js/consts.js';

export default class PairingCodeField extends Component {
    constructor(props) {
        super(props)
        this.onChange = this.onChange.bind(this)
        this.onBeforeInput = this.onBeforeInput.bind(this)
    }

    onBeforeInput(event) {
        const field = event.target
        if (this.props.pairing_method === PairingMethod.JDNOW || field.selectionStart !== field.selectionEnd) return
        const caret = field.selectionStart
        if (event.inputType === 'deleteContentBackward' && field.value[caret - 1] === '-') {
            field.setSelectionRange(caret - 1, caret - 1)
        } else if (event.inputType === 'deleteContentForward' && field.value[caret] === '-') {
            field.setSelectionRange(caret + 1, caret + 1)
        }
    }

    onChange(e) {
        const field = e.target
        const rule = pairingCodeRule(this.props.pairing_method)
        const jdnow = this.props.pairing_method === PairingMethod.JDNOW
        const caret = jdnow ? field.selectionStart : field.value.slice(0, field.selectionStart).replaceAll('-', '').length
        let value = jdnow ? field.value : field.value.replaceAll('-', '')
        if (rule?.uppercase) value = value.toUpperCase()
        field.value = formatPairingCode(value, this.props.pairing_method)
        const position = jdnow ? caret : caret + Math.max(0, Math.floor((caret - 1) / 2))
        field.setSelectionRange(position, position)
        this.props.onChange(value)
    }

    render(props) {
        const rule = pairingCodeRule(props.pairing_method)
        const jdnow = props.pairing_method === PairingMethod.JDNOW
        const characters = rule && new RegExp(`^${pairingCodeCharacters(rule)}$`, 'u')
        const length = rule && rule.max_length + (jdnow ? 0 : Math.floor((rule.max_length - 1) / 2))
        return html`
            <label for="pairingCode">${jdnow ? 'Dance Room number' : 'Pairing Code'}</label>
            ${rule && html`
                <input required id="pairingCode" type="text"
                    inputmode=${rule.uppercase ? 'text' : 'numeric'} autocapitalize="characters" spellcheck="false"
                    value=${formatPairingCode(props.pairing_code, props.pairing_method)}
                    placeholder=${jdnow ? 'e.g. 34017' : formatPairingCode('0'.repeat(rule.max_length))}
                    maxlength=${length} size=${length}
                    pattern=${jdnow ? pairingCodePattern(rule) : `${pairingCodeCharacters(rule)}{2}(?:-${pairingCodeCharacters(rule)}{2}){${rule.max_length / 2 - 1}}`}
                    onKeyPress=${e => e.key.length === 1 && !characters.test(e.key) && e.preventDefault()}
                    onBeforeInput=${this.onBeforeInput} onInput=${this.onChange} />
            `}
            ${!rule && html`
                <input type="text" id="pairingCode" value="" readonly placeholder="Not Required" size="12" />
            `}
        `
    }
}
