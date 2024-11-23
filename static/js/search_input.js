import { Component } from '/js/preact.module.js';
import { html, WsCommand } from '/js/app.js';

export default class SearchInput extends Component {
    constructor(props) {
        super(props)
        this.state = {
            text: '',
            // FIXME: after adding support for show/hide keyboard, set isVisible to false 
            isVisible: true
        }

        this.handleInput = this.handleInput.bind(this)
        this.handleSubmit = this.handleSubmit.bind(this)

        // Listen for keyboard show/hide events from server
        window.mitty.on('show_search', () => this.setState({isVisible: true}))
        window.mitty.on('hide_search', () => this.setState({isVisible: false}))
    }

    handleInput(e) {
        const text = e.target.value
        this.setState({text})
    }

    handleSubmit(e) {
        e.preventDefault()
        window.mitty.emit('req_' + WsCommand.SEARCH_INPUT, {
            text: this.state.text
        })
        this.setState({text: ''})
    }

    render(props, state) {
        if (!state.isVisible) return null

        return html`
            <div class="search-overlay">
                <h2 class="pure-u-18-24">Search</h2>
                <form class="pure-form" onSubmit=${this.handleSubmit}>
                    <input 
                        type="text" 
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
