// Run with: node --experimental-vm-modules tests/test_ip_address.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createContext, SourceTextModule } from 'node:vm';

const staticRoot = fileURLToPath(new URL('../static/', import.meta.url));
const events = [];
const window = { location: { hostname: '' }, mitty: { emit: (...args) => events.push(args) } };
const context = createContext({ window });
const modules = new Map();
const load = path => {
    if (!modules.has(path)) {
        modules.set(path, new SourceTextModule(readFileSync(path, 'utf8'), { context, identifier: path }));
    }
    return modules.get(path);
};
const module = load(resolve(staticRoot, 'js/components/IpAddressField.js'));
await module.link(specifier => load(resolve(staticRoot, '.' + specifier)));
await module.evaluate();
const IpAddressField = module.namespace.default;
const { isValidIPv4, IpAddressRegex } = modules.get(resolve(staticRoot, 'js/consts.js')).namespace;
const props = { pairing_method: 'default', host_ip_addr: '192.168.1.2', console_ip_addr: '192.168.1.3' };

for (const [address, valid] of [
    ['0.0.0.0', true], ['255.255.255.255', true], ['172.16.0.1', true], ['8.8.8.8', true],
    ['127.0.0.1', true], ['169.254.1.2', true],
    ['10.01.2.3', false], ['010.1.2.3', false], ['256.1.2.3', false],
    ['1.2.3', false], ['1.2.3.4.5', false], ['10.console.example', false],
    ['10.1.2.3\n', false], ['10.1.2.3\r', false], ['10.1.2.3\u2028', false],
    [' 10.1.2.3', false], ['10.1.2.3 ', false], ['::1', false], ['', false],
    [null, false], [undefined, false], [1234, false],
]) {
    assert.equal(isValidIPv4(address), valid, JSON.stringify(address));
    if (typeof address === 'string') {
        // HTML patterns must match the entire field value.
        const match = new RegExp(IpAddressRegex.HTML_PATTERN, 'v').exec(address);
        assert.equal(match?.[0] === address, valid, 'HTML pattern: ' + JSON.stringify(address));
    }
}

for (const [hostname, privateAddress] of [
    ['10.0.0.0', true], ['10.255.255.255', true],
    ['172.16.0.0', true], ['172.31.255.255', true],
    ['192.168.0.0', true], ['192.168.255.255', true],
    ['172.15.255.255', false], ['172.32.0.0', false],
    ['8.8.8.8', false], ['127.0.0.1', false], ['169.254.1.2', false], ['192.0.2.3', false],
    ['localhost', false], ['10.console.example', false], ['192.168.console.example', false],
    ['10.1.2.999', false], ['10.01.2.3', false], ['10.1.2.3\n', false],
    ['::1', false], ['[::1]', false], ['fc00::1', false], ['', false],
]) {
    for (const method of ['default', 'jdmo', 'fast']) {
        window.location.hostname = hostname;
        const field = new IpAddressField({ ...props, pairing_method: method });
        const expected = privateAddress ? hostname : props.host_ip_addr;
        assert.equal(field.state.lock_host, privateAddress, hostname);
        assert.equal(field.state.host_ip_addr, expected, hostname);
        assert.equal(field.state.console_ip_addr, props.console_ip_addr, hostname);
        field.componentDidMount();
        assert.deepEqual(events.pop(), ['update_addr', method === 'fast' ? props.console_ip_addr : expected]);
    }
}
console.log('PASS: IPv4 validation, HTML pattern, private address detection, and host/console selection.');
