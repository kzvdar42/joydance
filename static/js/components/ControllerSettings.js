import { Component } from '/js/preact.module.js';
import { html } from '/js/utils.js';
import { WsCommand } from '/js/consts.js';
import ActionIcon from '/js/components/ActionIcon.js';

export default class ControllerSettings extends Component {
    constructor(props) {
        super(props)
        this.state = {motionSettings: null, savingSettings: false, settingsFeedback: null}
        this.onSettingsSaved = this.onSettingsSaved.bind(this)
        this.onSettingsDisconnected = this.onSettingsDisconnected.bind(this)
    }

    componentDidMount() {
        window.mitty.on('resp_' + WsCommand.SAVE_CONTROLLER_SETTINGS, this.onSettingsSaved)
        window.mitty.on('ws_disconnected', this.onSettingsDisconnected)
    }

    componentWillUnmount() {
        window.mitty.off('resp_' + WsCommand.SAVE_CONTROLLER_SETTINGS, this.onSettingsSaved)
        window.mitty.off('ws_disconnected', this.onSettingsDisconnected)
    }

    onSettingsSaved({controller_serial, error}) {
        if (controller_serial !== this.props.controller.serial) return
        this.setState({savingSettings: false, settingsFeedback: error || 'Settings saved.',
            ...(error ? {} : {motionSettings: null})})
    }

    onSettingsDisconnected() {
        if (this.state.savingSettings) {
            this.setState({savingSettings: false, settingsFeedback: 'Connection lost. Refresh to check saved settings.'})
        }
    }

    open() {
        this.settingsDialog.showModal()
    }

    render({ controller }, { motionSettings, savingSettings, settingsFeedback }) {
        const { name } = controller
        return html`
            <dialog class="controller-settings" aria-labelledby=${`controller-settings-${controller.serial}`}
                ref=${element => this.settingsDialog = element}>
                <div class="controller-settings-heading">
                    <h3 id=${`controller-settings-${controller.serial}`}>${name} settings</h3>
                    <button type="button" class="pure-button icon-button" aria-label=${`Close ${name} settings`}
                        title="Close" onClick=${() => this.settingsDialog.close()}><${ActionIcon} name="close" /></button>
                </div>
                <form class="pure-form" onSubmit=${event => {
                    event.preventDefault()
                    if (savingSettings) return
                    this.setState({savingSettings: true, settingsFeedback: null})
                    window.mitty.emit('req_' + WsCommand.SAVE_CONTROLLER_SETTINGS, {
                        controller_serial: controller.serial,
                        settings: motionSettings || controller.motion_settings,
                    })
                }}>
                    <fieldset disabled=${savingSettings}>
                        <label class="pure-checkbox">
                            <input type="checkbox" name="idle_imu_shutdown"
                                aria-labelledby=${`idle-power-title-${controller.serial}`}
                                aria-describedby=${`idle-power-description-${controller.serial}`}
                                checked=${(motionSettings || controller.motion_settings).idle_imu_shutdown}
                                onChange=${event => this.setState({motionSettings: {
                                    ...(motionSettings || controller.motion_settings), idle_imu_shutdown: event.target.checked,
                                }, settingsFeedback: null})} />
                            <span>
                                <span class="controller-setting-title" id=${`idle-power-title-${controller.serial}`}>
                                    Automatic sensor sleep <span class="controller-setting-alpha" role="img" aria-label="Experimental" title="Experimental">α</span>
                                </span>
                                <span class="controller-setting-description" id=${`idle-power-description-${controller.serial}`}>
                                    Motion sensors wake up only during dance scoring
                                </span>
                            </span>
                        </label>
                        <label class="pure-checkbox">
                            <input type="checkbox" name="gyro_shutdown"
                                aria-labelledby=${`song-power-title-${controller.serial}`}
                                aria-describedby=${`song-power-description-${controller.serial}`}
                                checked=${(motionSettings || controller.motion_settings).gyro_shutdown}
                                onChange=${event => this.setState({motionSettings: {
                                    ...(motionSettings || controller.motion_settings), gyro_shutdown: event.target.checked,
                                }, settingsFeedback: null})} />
                            <span>
                                <span class="controller-setting-title" id=${`song-power-title-${controller.serial}`}>
                                    Accelerometer only <span class="controller-setting-alpha" role="img" aria-label="Experimental" title="Experimental">α</span>
                                </span>
                                <span class="controller-setting-description" id=${`song-power-description-${controller.serial}`}>
                                    Turn off gyroscope (it's not used during scoring) to save power
                                </span>
                            </span>
                        </label>
                        <div class="controller-settings-actions">
                            <button type="submit" class="pure-button pure-button-primary" disabled=${!motionSettings}>${savingSettings ? 'Saving…' : 'Save changes'}</button>
                        </div>
                    </fieldset>
                </form>
                ${controller.motion_settings.gyro_shutdown && !controller.gyro_shutdown_available
                    ? html`<p class="controller-field-status">Power saving during songs could not be enabled on this controller.</p>` : null}
                ${settingsFeedback && html`<p class="controller-field-status" role="status">${settingsFeedback}</p>`}
            </dialog>
        `
    }
}
