// Run with: node --experimental-vm-modules tests/test_jd23_ui.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createContext, SourceTextModule } from 'node:vm';

const root = fileURLToPath(new URL('../static/', import.meta.url));
const context = createContext({window: {location: {hostname: 'localhost'}}});
const modules = new Map();
const load = path => {
    if (!modules.has(path)) modules.set(path, new SourceTextModule(readFileSync(path, 'utf8'), {context, identifier:path}));
    return modules.get(path);
};
for (const name of ['PairingCodeField', 'PairingMethodPicker', 'IpAddressField']) {
    const module = load(resolve(root, 'js/components/' + name + '.js'));
    if (module.status === 'unlinked') await module.link(specifier => load(resolve(root, '.' + specifier)));
    await module.evaluate();
}
const get = name => modules.get(resolve(root, 'js/components/' + name + '.js')).namespace.default;
const {usesHostAddress, usesPairingCode, isValidPairingCode} = modules.get(resolve(root, 'js/consts.js')).namespace;
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
assert.equal(isValidPairingCode('ABCD1234', 'default'), false);
for (const ending of ['\n', '\r', '\r\n', '\u2028', '\u2029']) {
    assert.equal(isValidPairingCode('012345' + ending, 'default'), false);
}
let updated;
const props = {pairing_method:'jd23', pairing_code:'', onChange:value => updated=value};
const Code = get('PairingCodeField');
const field = new Code(props);
const jd23 = input(field.render(props)).props;
assert.equal(jd23.maxlength, 8);
assert.equal(jd23.inputmode, 'text');
assert.equal(jd23.pattern, '[A-Za-z0-9]{8}');
field.onChange({target:{value:'abcd1234'}});
assert.equal(updated, 'ABCD1234');
const old = input(field.render({...props, pairing_method:'default'})).props;
assert.equal(old.maxlength, 6);
assert.equal(old.pattern, '[0-9]{6}');
const Address = get('IpAddressField');
assert.equal(new Address(props).render(props, {}), null);
const Picker = get('PairingMethodPicker');
assert(nodes(new Picker(props).render(props)).some(node => node?.type === 'option' && node.props.value === 'jd23'));
console.log('PASS: JD23+ pairing selection, eight-character codes, uppercase input, no host-address field, legacy code validation.');
