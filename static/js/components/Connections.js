import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { PairingMethod, WsCommand, formatPairingCode, isPrivateIPv4, pairingAddressField, usesPairingCode } from '/js/consts.js';
import PairingMethodPicker from '/js/components/PairingMethodPicker.js';
import IpAddressField from '/js/components/IpAddressField.js';
import PairingCodeField from '/js/components/PairingCodeField.js';
import ActionIcon from '/js/components/ActionIcon.js';

const methodNames = {default:'Just Dance', fast:'Fast', stadia:'Stadia', old:'Legacy', jdmo:'JDMO 2021', jd23:'JD23+', jdnow:'Just Dance Now'};
const connectionKey = connection => usesPairingCode(connection.pairing_method) ? (connection.pairing_method === PairingMethod.JDNOW ? 'jdnow:' : 'code:') + connection.pairing_code :
    connection.console_url || `${connection.pairing_method === PairingMethod.OLD ? 'ws' : 'wss'}://${connection.console_ip_addr}:8080/smartphone`;
const connectionKeys = connection => connection.console_urls || [connectionKey(connection)];
const selectionKey = connection => connection.id || 'local:' + (connection.title_id || '') + ':' + connectionKey(connection);

export default class Connections extends Component {
    constructor(props) {
        super(props)
        this.state = { saved: window.CONNECTIONS || [], local: [], selected: null, draft: null, scanning: false, saving: false, error: '', message: '' }
        this.scan = this.scan.bind(this)
        this.save = this.save.bind(this)
        this.closeEditor = this.closeEditor.bind(this)
        this.onSavedConnections = this.onSavedConnections.bind(this)
    }

    componentDidMount() {
        window.mitty.on('resp_' + WsCommand.GET_CONNECTIONS, this.onSavedConnections)
        const previous = this.state.saved.find(connection => Object.keys(this.props.config).every(key => connection[key] === this.props.config[key]))
        if (previous || this.state.saved.length) this.select(previous || this.state.saved[0])
    }

    componentWillUnmount() {
        window.mitty.off('resp_' + WsCommand.GET_CONNECTIONS, this.onSavedConnections)
        this.scanRequest?.abort()
        this.saveRequest?.abort()
    }

    select(connection) {
        this.setState({ selected: connection ? selectionKey(connection) : null })
        this.props.onSelect(connection)
    }

    onSavedConnections(saved) {
        if (this.state.selected && !this.state.selected.startsWith('local:')) {
            this.select(saved.find(connection => connection.id === this.state.selected) || null)
        }
        this.setState({ saved })
    }

    newDraft(connection = {}) {
        if (this.state.saving) return
        this.setState({ draft: {
            ...this.props.config, name: '', ...connection,
            host_ip_addr: isPrivateIPv4(window.location.hostname) ? window.location.hostname : (connection.host_ip_addr || this.props.config.host_ip_addr),
        }, error: '' }, () => {
            this.editorDialog.showModal()
            document.getElementById('connectionName').focus()
        })
    }

    closeEditor() {
        if (this.state.saving) return
        this.editorDialog.close()
        this.setState({draft:null, error:''})
    }

    changeDraft(key, value) {
        this.setState(state => {
            const draft = {...state.draft, [key]: value}
            if (value !== state.draft[key] && ['pairing_method', 'console_ip_addr'].includes(key)) delete draft.console_url
            return { draft }
        })
    }

    async save(event) {
        event.preventDefault()
        if (this.state.saving) return
        this.setState({ saving: true, error: '' })
        this.saveRequest = new AbortController()
        try {
            const response = await fetch('/connections', {
                method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify(this.state.draft), signal: this.saveRequest.signal,
            })
            const data = await response.json()
            if (!response.ok) throw new Error(data.error || 'Could not save this connection.')
            this.onSavedConnections(data.connections)
            this.select(data.connection)
            // Re-enable the opener before the dialog restores focus.
            this.setState({ saving: false }, this.closeEditor)
        } catch (error) {
            if (error.name !== 'AbortError') this.setState({ error: error.message })
        } finally {
            this.setState({ saving: false })
        }
    }

    async remove(connection) {
        if (this.state.saving) return
        if (!window.confirm(`Are you sure you want to delete the saved connection "${connection.name}"?`)) return
        this.setState({ error: '' })
        try {
            const response = await fetch('/connections/' + encodeURIComponent(connection.id), {method:'DELETE'})
            const data = await response.json()
            if (!response.ok) throw new Error(data.error || 'Could not delete this connection.')
            this.onSavedConnections(data.connections)
            if (this.state.draft?.id === connection.id) this.setState({draft:null})
        } catch (error) {
            this.setState({ error: error.message })
        }
    }

    async scan() {
        if (this.state.scanning) return
        if (this.state.selected?.startsWith('local:')) this.select(null)
        this.setState({ scanning: true, local: [], error: '', message: 'Looking for games on the same network…' })
        this.scanRequest = new AbortController()
        try {
            const response = await fetch('/discovery', {signal:this.scanRequest.signal, cache:'no-store'})
            const data = await response.json()
            if (!response.ok) throw new Error(data.error || 'Could not scan the network.')
            const local = data.consoles.map(console => ({
                ...this.props.config, name: console.name.slice(0, 80),
                pairing_method: console.protocol === 'v1' ? PairingMethod.OLD :
                    (this.props.pairing_method === PairingMethod.JDMO ? PairingMethod.JDMO : PairingMethod.DEFAULT),
                console_ip_addr: console.console_ip_addr || '', pairing_code: console.pairing_code || '',
                ...(console.console_url ? {console_url:console.console_url} : {}),
                ...(console.console_urls ? {console_urls:console.console_urls} : {}),
                ...(console.title_id ? {title_id:console.title_id} : {}),
            }))
            this.setState({ local, message: local.length ? 'Local games found. Select one, or save it for later.' :
                'No local games found. Open the game’s phone pairing screen, or add a connection manually.' })
        } catch (error) {
            if (error.name !== 'AbortError') this.setState({ message: '', error: error.message })
        } finally {
            this.setState({ scanning: false })
        }
    }

    render(props, state) {
        const savedKeys = new Set(state.saved.map(connectionKey))
        const localKeys = new Set(state.local.flatMap(connectionKeys))
        const unsaved = state.local.filter(connection => !connectionKeys(connection).some(key => savedKeys.has(key)))
        const connections = [...state.saved, ...unsaved]
        const draft = state.draft
        return html`
            <section class="connections" aria-label="Connections">
                <div class="connections-heading">
                    <h2>Connections</h2>
                    <div class="connection-actions">
                        <button type="button" class="pure-button icon-button" disabled=${state.scanning} aria-busy=${state.scanning}
                            aria-label=${state.scanning ? 'Scanning…' : 'Scan network'} title=${state.scanning ? 'Scanning…' : 'Scan network'}
                            onClick=${this.scan}><${ActionIcon} name=${state.scanning ? 'loading' : 'scan'} /></button>
                        <button type="button" class="pure-button pure-button-primary icon-button" disabled=${state.saving}
                            aria-label="Add connection" title="Add connection" onClick=${() => this.newDraft()}><${ActionIcon} name="add" /></button>
                    </div>
                </div>
                <p>Choose a connection, then connect a controller below.</p>
                <p role="status" aria-live="polite">${state.message}</p>
                ${state.error && !draft && html`<p class="connection-error" role="alert">${state.error}</p>`}
                ${!connections.length && html`<p class="connections-empty">No connections yet. Add one or scan your local network.</p>`}
                <ul class="connections-list">
                    ${connections.map(connection => {
                        const key = selectionKey(connection)
                        return html`
                            <li key=${key} class=${state.selected === key ? 'selected' : ''}>
                                <label class="connection-choice">
                                    <input type="radio" name="connection" checked=${state.selected === key} disabled=${state.saving} onChange=${() => this.select(connection)} />
                                    <span><strong>${connection.name}</strong>
                                        <span class="connection-source">${connection.id ? 'Saved' : 'Local'}${connection.id && localKeys.has(connectionKey(connection)) ? ' · Local' : ''}</span>
                                        <small>${methodNames[connection.pairing_method]} · ${usesPairingCode(connection.pairing_method) ? 'Code ' + formatPairingCode(connection.pairing_code, connection.pairing_method) : connection.console_ip_addr}</small>
                                    </span>
                                </label>
                                <div class="connection-actions">
                                    <button type="button" class=${`pure-button${connection.id ? ' icon-button' : ''}`} disabled=${state.saving}
                                        aria-label=${(connection.id ? 'Edit ' : 'Save ') + connection.name} title=${connection.id ? 'Edit connection' : 'Save connection'}
                                        onClick=${() => this.newDraft(connection)}>${connection.id ? html`<${ActionIcon} name="edit" />` : 'Save'}</button>
                                    ${connection.id && html`<button type="button" class="pure-button pure-button-error icon-button" disabled=${state.saving}
                                        aria-label=${'Delete ' + connection.name} title="Delete connection"
                                        onClick=${() => this.remove(connection)}><${ActionIcon} name="delete" /></button>`}
                                </div>
                            </li>
                        `
                    })}
                </ul>
                ${unsaved.some(connection => usesPairingCode(connection.pairing_method)) && html`
                    <p>For JDMO 2021, use Save to choose its pairing method before connecting.</p>
                `}
                ${draft && html`
                    <dialog class="connection-dialog" aria-labelledby="connection-editor-title"
                        ref=${element => this.editorDialog = element}
                        onCancel=${event => { event.preventDefault(); this.closeEditor() }}>
                        <div class="connection-dialog-heading">
                            <h3 id="connection-editor-title">${draft.id ? 'Edit connection' : 'Add connection'}</h3>
                            <button type="button" class="pure-button icon-button" disabled=${state.saving}
                                aria-label="Close connection editor" title="Close" onClick=${this.closeEditor}>
                                <${ActionIcon} name="close" />
                            </button>
                        </div>
                        <form class="pure-form pure-form-stacked connection-editor" onSubmit=${this.save}>
                            <fieldset disabled=${state.saving}>
                                <div class="pure-g">
                                    <div class="pure-u-1">
                                        <label for="connectionName">Name</label>
                                        <input required id="connectionName" maxlength="80" value=${draft.name} placeholder="e.g. Living room" onChange=${event => this.changeDraft('name', event.target.value)} />
                                    </div>
                                    <div class="pure-u-1"><${PairingMethodPicker} pairing_method=${draft.pairing_method} onChange=${value => this.changeDraft('pairing_method', value)} /></div>
                                    ${pairingAddressField(draft.pairing_method) && html`
                                        <div class="pure-u-1-2"><${IpAddressField} ...${draft} onChange=${value => this.changeDraft(pairingAddressField(draft.pairing_method), value)} /></div>
                                    `}
                                    <div class=${pairingAddressField(draft.pairing_method) ? 'pure-u-1-2' : 'pure-u-1'}>
                                        <${PairingCodeField} ...${draft} onChange=${value => this.changeDraft('pairing_code', value)} />
                                    </div>
                                </div>
                                ${state.error && html`<p class="connection-error" role="alert">${state.error}</p>`}
                                <div class="connection-actions">
                                    <button type="submit" class="pure-button pure-button-primary">${state.saving ? 'Saving…' : draft.id ? 'Save changes' : 'Add connection'}</button>
                                </div>
                            </fieldset>
                        </form>
                    </dialog>
                `}
            </section>
        `
    }
}
