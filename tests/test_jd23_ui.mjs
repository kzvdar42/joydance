// Run with: node --experimental-vm-modules tests/test_jd23_ui.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createContext, SourceTextModule } from 'node:vm';

const root = fileURLToPath(new URL('../static/', import.meta.url));
const pairingRules = JSON.parse(readFileSync(resolve(root, 'pairing_rules.json'), 'utf8'));
const context = createContext({window: {location: {hostname: 'localhost'}, PAIRING_RULES: pairingRules}});
const modules = new Map();
const load = path => {
    if (!modules.has(path)) modules.set(path, new SourceTextModule(readFileSync(path, 'utf8'), {context, identifier:path}));
    return modules.get(path);
};
for (const name of ['PairingCodeField', 'PairingMethodPicker', 'IpAddressField', 'controller']) {
    const module = load(resolve(root, 'js/components/' + name + '.js'));
    if (module.status === 'unlinked') await module.link(specifier => load(resolve(root, '.' + specifier)));
    await module.evaluate();
}
const get = name => modules.get(resolve(root, 'js/components/' + name + '.js')).namespace.default;
const {usesHostAddress, usesPairingCode, isValidPairingCode, formatPairingCode} = modules.get(resolve(root, 'js/consts.js')).namespace;
const nodes = node => [node, ...(Array.isArray(node) ? node.flatMap(nodes) :
    (node?.props?.children ? [node.props.children].flatMap(nodes) : []))];
const input = tree => nodes(tree).find(node => node?.type === 'input');

assert.equal(usesHostAddress('jd23'), false);
assert.equal(usesPairingCode('jd23'), true);
for (const [value, valid] of [
    ['ABCD1234',true], ['abcd1234',true], ['01234567',true], ['012345',false],
    ['ABCD123\n',false], ['ABCD1234\n',false], ['ABCD1234\r',false],
    ['ABCD1234\r\n',false], ['ABCD1234\u2028',false], ['ABCD1234\u2029',false],
    ['ＡBCD1234',false], [null,false], [12345678,false],
]) {
    assert.equal(isValidPairingCode(value, 'jd23'), valid, value);
}
assert.equal(isValidPairingCode('012345', 'default'), true);
assert.equal(isValidPairingCode('٠١٢٣٤٥', 'stadia'), true);
assert.equal(isValidPairingCode('ABCD1234', 'default'), false);
for (const ending of ['\n', '\r', '\r\n', '\u2028', '\u2029']) {
    assert.equal(isValidPairingCode('012345' + ending, 'default'), false);
}
let updated;
const props = {pairing_method:'jd23', pairing_code:'', onChange:value => updated=value};
const Code = get('PairingCodeField');
const field = new Code(props);
const jd23 = input(field.render(props)).props;
assert.equal(jd23.maxlength, 11);
assert.equal(jd23.inputmode, 'text');
assert.equal(jd23.pattern, '[A-Za-z0-9]{2}(?:-[A-Za-z0-9]{2}){3}');
assert.equal(jd23.placeholder, '00-00-00-00');
const edited = {value:'ab-cd-12-34', selectionStart:11, setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }};
field.onChange({target:edited});
assert.equal(updated, 'ABCD1234');
assert.equal(edited.value, 'AB-CD-12-34');
assert.equal(edited.selectionStart, 11);
edited.value = 'AB-CD-12-34';
edited.selectionStart = edited.selectionEnd = 3;
field.onBeforeInput({target:edited,inputType:'deleteContentBackward'});
assert.equal(edited.selectionStart, 2);
field.onBeforeInput({target:edited,inputType:'deleteContentForward'});
assert.equal(edited.selectionStart, 3);
assert.equal(formatPairingCode('012345', 'jdmo'), '01-23-45');
assert.equal(formatPairingCode('٠١٢٣٤٥', 'default'), '٠١-٢٣-٤٥');
const old = input(field.render({...props, pairing_method:'default'})).props;
assert.equal(old.maxlength, 8);
assert.equal(old.pattern, '\\p{Nd}{2}(?:-\\p{Nd}{2}){2}');
const Address = get('IpAddressField');
assert.equal(new Address(props).render(props, {}), null);
const stadiaProps = {...props, pairing_method:'stadia', host_ip_addr:'192.168.1.2', console_ip_addr:'192.168.1.3'};
const stadiaAddress = input(new Address(stadiaProps).render(stadiaProps, {lock_host:false})).props;
assert.equal(stadiaAddress.value, stadiaProps.host_ip_addr);
assert.equal(stadiaAddress.required, true);
assert.equal(stadiaAddress.readonly, false);
assert.equal(usesHostAddress('stadia'), true);
assert.equal(usesPairingCode('stadia'), true);
const Picker = get('PairingMethodPicker');
assert(nodes(new Picker(props).render(props)).some(node => node?.type === 'option' && node.props.value === 'jd23'));
console.log('PASS: JD23+ pairing selection, eight-character codes, uppercase input, no host-address field, legacy code validation.');

assert.equal(usesHostAddress('jdnow'), false);
assert.equal(usesPairingCode('jdnow'), true);
for (const [value, valid] of [['22965',true], ['012345',true], ['123',false], ['1234567890',false], ['22965\n',false], ['１２３４５',false]]) {
    assert.equal(isValidPairingCode(value, 'jdnow'), valid, value);
}
const nowProps = {...props, pairing_method:'jdnow', pairing_code:'22965'};
const nowField = input(field.render(nowProps)).props;
assert.equal(nowField.maxlength, 9);
assert.equal(nowField.pattern, '[0-9]{4,9}');
assert.equal(nowField.value, '22965');
assert.equal(formatPairingCode('012345', 'jdnow'), '012345');
assert.equal(new Address(nowProps).render(nowProps, {}), null);
assert(nodes(new Picker(nowProps).render(nowProps)).some(node => node?.type === 'option' && node.props.value === 'jdnow'));
console.log('PASS: JDN room selection and validation; no host-address field.');

const emitted = [];
context.window.mitty = {emit: (...args) => emitted.push(args)};
const controllerData = {serial:'jdn-left', name:'JoyCon', state:4, player_name:'Dancer',
    player_color:[0,200,255,1], supports_song_search:true, supports_player_rename:true,
    song_search_enabled:true, song_search_total:1, song_search_results:[{id:'24K', name:'24K Magic', artist:'Bruno Mars'}]};
const Controllers = modules.get(resolve(root, 'js/components/controller.js')).namespace.Controllers;
const list = new Controllers();
const cardNode = nodes(list.render({controllers:[controllerData]}, list.state)).find(node => node?.props?.controller);
const card = new cardNode.type(cardNode.props);
card.setState = update => Object.assign(card.state, typeof update === 'function' ? update(card.state) : update);
card.state.playerName = 'New Dancer';
card.state.songQuery = 'Magic';
let tree = card.render(card.props, card.state);
const forms = nodes(tree).filter(node => node?.type === 'form');
assert.equal(forms.length, 2);
forms[0].props.onSubmit({preventDefault(){}});
forms[1].props.onSubmit({preventDefault(){}});
tree = card.render(card.props, card.state);
const result = nodes(tree).find(node => node?.props?.class === 'controller-song-result');
assert.equal(nodes(result).find(node => node?.props?.class === 'controller-song-title').props.children, '24K Magic');
assert.equal(nodes(result).find(node => node?.props?.class === 'controller-song-artist').props.children, 'Bruno Mars');
result.props.onClick();
assert.deepEqual(JSON.parse(JSON.stringify(emitted)), [
    ['req_rename_player',{controller_serial:'jdn-left',name:'New Dancer'}],
    ['req_search_input',{controller_serial:'jdn-left',text:'Magic'}],
    ['req_select_song',{controller_serial:'jdn-left',song:'24K'}],
]);
assert(nodes(tree).some(node => node?.props?.class === 'player-color' && node.props.style.includes('rgba(0, 200, 255, 1)')));
card.state.controller = {...controllerData, song_search_total:42};
tree = card.render(card.props, card.state);
assert.equal(nodes(tree).find(node => node?.props?.role === 'status').props.children, 'Showing 1 of 42 songs');
card.state.controller = {...controllerData, song_search_total:0, song_search_results:[]};
tree = card.render(card.props, card.state);
assert.equal(nodes(tree).find(node => node?.props?.role === 'status').props.children, 'No songs match. Try another title or artist.');
assert(!nodes(tree).some(node => node?.props?.class === 'controller-song-results'));
card.state.controller = {...controllerData, song_search_enabled:false};
tree = card.render(card.props, card.state);
assert(nodes(tree).find(node => node?.type === 'input' && node.props.type === 'search').props.disabled);
card.state.controller = {...controllerData, supports_song_search:false, supports_player_rename:false};
assert.equal(nodes(card.render(card.props, card.state)).filter(node => node?.type === 'form').length, 0);
console.log('PASS: JDN player color, rename, search, selection, and disabled controls outside song browsing.');

card.state.controller = controllerData;
for (const feedback of [{text:'Player name updated.'}, {text:'Rename failed.', error:true}]) {
    card.onStateUpdated({serial:'jdn-left', state:4, player_rename_feedback:feedback});
    tree = card.render(card.props, card.state);
    const renameSection = nodes(tree).find(node => node?.props?.['aria-label'] === 'Player rename');
    const status = nodes(renameSection).find(node => node?.props?.role === (feedback.error ? 'alert' : 'status'));
    assert.equal(status?.props.children, feedback.text);
    assert(!nodes(tree).some(node => node?.props?.class?.includes('controller-message')));
}
console.log('PASS: JDN rename confirmations and errors appear below the Player name field.');

const renameInput = () => nodes(card.render(card.props, card.state))
    .find(node => node?.type === 'input' && node.props.type === 'text');
assert.equal(renameInput().props.maxlength, '20');
card.state.playerName = 'New  Dancer';
card.onStateUpdated({serial:'jdn-left', state:4, player_name:'New Dancer'});
card.onPlayerRenamed({serial:'jdn-left', submitted_name:'New  Dancer'});
assert.equal(renameInput().props.value, 'New Dancer');
card.state.playerName = 'Still editing';
card.onPlayerRenamed({serial:'jdn-left', submitted_name:'New  Dancer'});
assert.equal(renameInput().props.value, 'Still editing');
card.onStateUpdated({serial:'jdn-left', state:10,
    player_rename_feedback:{text:'Player name saved. Reconnect to update the Dance Room.', error:true}});
assert(!renameInput());
assert(nodes(card.render(card.props, card.state)).some(node => node?.props?.role === 'alert' &&
    node.props.children === 'Player name saved. Reconnect to update the Dance Room.'));

card.onStateUpdated({serial:'jdn-left', state:4, song_search_feedback:{text:'Unknown song.', error:true}});
tree = card.render(card.props, card.state);
const searchSection = nodes(tree).find(node => node?.props?.['aria-label'] === 'Song search');
assert(nodes(searchSection).some(node => node?.props?.role === 'alert' && node.props.children === 'Unknown song.'));
assert.equal(nodes(tree).filter(node => node?.props?.children === 'Unknown song.').length, 1);
const searchInput = tree => nodes(tree).find(node => node?.type === 'input' && node.props.type === 'search');
const searchField = {value:'', validationMessage:'', reported:false,
    setCustomValidity(message) { this.validationMessage = message; },
    reportValidity() { this.reported = true; }};
searchInput(tree).props.onInput({target:searchField});
assert.deepEqual(JSON.parse(JSON.stringify(emitted.at(-1))), ['req_search_input',{controller_serial:'jdn-left',text:''}]);
tree = card.render(card.props, card.state);
assert(!nodes(tree).some(node => node?.props?.class === 'controller-song-results' || node?.props?.children === 'Unknown song.'));
assert(!searchInput(tree).props.required);
assert.equal(searchField.validationMessage, '');
const searchesBeforeInvalidSubmit = emitted.length;
const searchForm = nodes(nodes(tree).find(node => node?.props?.['aria-label'] === 'Song search'))
    .find(node => node?.type === 'form');
searchForm.props.onSubmit({
    preventDefault(){}, currentTarget:{elements:{songQuery:searchField}},
});
assert(searchField.reported && searchField.validationMessage);
assert.equal(emitted.length, searchesBeforeInvalidSubmit);
searchField.value = 'Another song';
searchInput(tree).props.onInput({target:searchField});
assert.equal(searchField.validationMessage, '');
assert(!nodes(card.render(card.props, card.state)).some(node => node?.props?.class === 'controller-song-results'));
console.log('PASS: JDN search validation starts on submit, and clearing dismisses results and feedback.');

const guestData = {...controllerData, state:0, jdnow_guests:[
    {key:'original-key', name:'Guest 1 · Original', controllers:['unplugged-controller']},
    {key:'fresh-key', name:'Guest 2 · Fresh', controllers:['jdn-left']},
], jdnow_guest:'fresh-key'};
const guestNode = nodes(list.render({controllers:[guestData], jdnow:true}, list.state)).find(node => node?.props?.controller);
const guestCard = new guestNode.type(guestNode.props);
guestCard.setState = update => Object.assign(guestCard.state, update);
const guestPicker = nodes(guestCard.render(guestCard.props, guestCard.state)).find(node => node?.type === 'select');
assert.equal(guestPicker.props.value, 'fresh-key');
assert.deepEqual(nodes(guestPicker).filter(node => node?.type === 'option').map(node => node.props.children),
    ['Guest 1 · Original · Saved for unplugged-controller', 'Guest 2 · Fresh', 'New guest', 'Sign in with Ubisoft…']);
emitted.length = 0;
for (const value of ['new', 'original-key']) {
    guestPicker.props.onChange({target:{value}});
    guestCard.connect();
}
assert.deepEqual(JSON.parse(JSON.stringify(emitted)), [
    ['req_connect_controller',{serial:'jdn-left',guest:'new'}],
    ['req_connect_controller',{serial:'jdn-left',guest:'original-key'}],
]);
guestPicker.props.onChange({target:{value:'original-key'}});
guestCard.state.controller = {...guestData, jdnow_guests:[guestData.jdnow_guests[1]]};
const availablePicker = nodes(guestCard.render(guestCard.props, guestCard.state)).find(node => node?.type === 'select');
assert.equal(availablePicker.props.value, 'fresh-key');
assert(!nodes(availablePicker).some(node => node?.props?.value === 'original-key'));
assert(!nodes(guestCard.render({...guestCard.props, jdnow:false}, guestCard.state)).some(node => node?.type === 'select'));
const currentGuestPicker = () => nodes(guestCard.render(guestCard.props, guestCard.state)).find(node => node?.type === 'select');
const signInOption = () => nodes(currentGuestPicker()).find(node => node?.type === 'option' && node.props.value === 'sign-in');
context.fetch = async (url, options) => {
    assert.equal(url, '/jdnow/sign-in');
    assert.equal(options.method, 'POST');
    return {ok:true, json:async () => ({})};
};
const signInTarget = {value:'sign-in'};
const signIn = currentGuestPicker().props.onChange({target:signInTarget});
assert.equal(signInTarget.value, 'fresh-key');
assert.equal(guestCard.selectedGuest(), 'fresh-key');
assert(currentGuestPicker().props.disabled);
assert(currentGuestPicker().props['aria-busy']);
assert(!nodes(guestCard.render(guestCard.props, guestCard.state)).some(node => node?.type === 'button' && node.props.children === 'Sign in with Ubisoft'));
await signIn;
assert.match(guestCard.state.signInFeedback.text, /Profile imported/);
assert(!currentGuestPicker().props.disabled);
context.fetch = async () => ({ok:false,json:async () => ({error:'Test sign-in failure'})});
await currentGuestPicker().props.onChange({target:{value:'sign-in'}});
assert.equal(guestCard.state.signInFeedback.error, true);
assert.equal(guestCard.state.signInFeedback.text, 'Test sign-in failure');
assert.equal(currentGuestPicker().props.value, 'fresh-key');
assert(!currentGuestPicker().props.disabled);
context.window.location.hostname = '192.168.1.2';
assert.equal(signInOption(), undefined);
context.window.location.hostname = 'localhost';
guestCard.state.controller = {...guestData, state:4};
assert(!nodes(guestCard.render(guestCard.props, guestCard.state)).some(node => node?.type === 'select'));
console.log('PASS: JDN guest picker labels other controllers and falls back when a guest becomes unavailable.');
