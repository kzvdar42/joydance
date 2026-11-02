import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod } from '/js/consts.js';

export default class PairingMethodPicker extends Component {
    constructor(props) {
        super(props)

        this.onChange = this.onChange.bind(this)
    }

    onChange(e) {
        const pairing_method = e.target.value
        this.props.onChange(pairing_method)
    }

    render(props) {
        return html`
            <label for="stacked-state">Pairing Method</label>
            <select id="stacked-state" onChange=${this.onChange} value=${props.pairing_method}>
                <optgroup label="JD 2023 and later">
                    <option value="${PairingMethod.JD23}">JD23+: Pairing code</option>
                </optgroup>
                <optgroup label="JD 2020-2022">
                    <option value="${PairingMethod.DEFAULT}">Default: All platforms except Stadia</option>
                    <option value="${PairingMethod.FAST}">Fast: Xbox One/PlayStation/Nintendo Switch</option>
                    <option value="${PairingMethod.STADIA}">Stadia: for Stadia, obviously</option>
                </optgroup>
                <optgroup label="JD 2016-2019">
                    <option value="${PairingMethod.OLD}">Old: All platforms (incl. PC)</option>
                </optgroup>
                <optgroup label="Mods">
                    <option value="${PairingMethod.JDMO}">JDMO 2021: Pairing code</option>
                </optgroup>
            </select>
        `
    }
}
