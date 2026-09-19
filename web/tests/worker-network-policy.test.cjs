/* Pure policy construction; no credentials, cloud calls, or network access. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const file = path.join(__dirname, '../lib/worker-network-policy.ts');
const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }, fileName: file,
}).outputText;
const library = { exports: {} };
vm.runInNewContext(compiled, { module: library, exports: library.exports,
  require: () => { throw new Error('Policy construction must not load a network SDK'); } }, { filename: file });
const { PUBLIC_IPV4_CIDRS, importNetworkPolicy } = library.exports;

test('imports rely only on explicit public CIDRs, leaving CDN DNS available and all other IPs denied', () => {
  const policy = importNetworkPolicy();
  assert.deepEqual(Object.keys(policy), ['subnets']);
  assert.deepEqual(Object.keys(policy.subnets), ['allow']);
  assert.equal(policy.allow, undefined);
  assert.equal(policy.subnets.deny, undefined);
  assert.equal(PUBLIC_IPV4_CIDRS.length, 105);
  assert.equal(PUBLIC_IPV4_CIDRS.some(cidr => cidr.includes(':') || cidr.includes('*') || cidr.endsWith('/0')), false);
  assert.deepEqual(Array.from(policy.subnets.allow), Array.from(PUBLIC_IPV4_CIDRS));
});

test('one dispatched policy cannot mutate the public address set of a later worker', () => {
  assert.equal(Object.isFrozen(PUBLIC_IPV4_CIDRS), true);
  const first = importNetworkPolicy();
  first.subnets.allow.push('127.0.0.0/8');
  const second = importNetworkPolicy();
  assert.notEqual(first.subnets.allow, second.subnets.allow);
  assert.equal(second.subnets.allow.includes('127.0.0.0/8'), false);
  assert.equal(second.subnets.allow.length, 105);
});
