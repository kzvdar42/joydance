import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { WsCommand } from '/js/consts.js';

export default class SearchInputField extends Component {
    constructor(props) {
        super(props)
        this.state = {
            text: '',
            controllerSerial: null,
            isPassword: false
        }

        this.handleInput = this.handleInput.bind(this)
        this.handleSubmit = this.handleSubmit.bind(this)

        // Listen for keyboard show/hide events from server
        window.mitty.on('show_search', ({serial, isPassword = false}) => this.setState({
            controllerSerial: serial, text: '', isPassword
        }))
        window.mitty.on('hide_search', ({serial}) => this.setState(state => (
            state.controllerSerial === serial ? {controllerSerial: null, text: '', isPassword: false} : null
        )))
    }

    handleInput(e) {
        const text = e.target.value
        this.setState({text})
    }

    handleSubmit(e) {
        e.preventDefault()
        if (!this.state.controllerSerial) return
        window.mitty.emit('req_' + WsCommand.SEARCH_INPUT, {
            text: this.state.text,
            controller_serial: this.state.controllerSerial
        })
        this.setState({text: ''})
    }

    render(props, state) {
        if (!state.controllerSerial) return null

        return html`
            <div class="search-overlay">
                <h2 class="pure-u-18-24">Search</h2>
                <form class="pure-form" onSubmit=${this.handleSubmit}>
                    <input 
                        type=${state.isPassword ? 'password' : 'text'}
                        value=${state.text}
                        onInput=${this.handleInput}
                        autoFocus
                    />
                    <button type="submit" class="pure-button pure-button-primary">Send</button>
                </form>
            </div>
        `
    }
}
