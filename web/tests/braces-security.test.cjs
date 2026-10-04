const assert = require('node:assert/strict');
const { test } = require('node:test');
const { createRequire } = require('node:module');
const { realpathSync, readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const braces = require('braces');
const micromatch = require('micromatch');
const glob = require('fast-glob');

const nest = (depth, open = '{', close = '}') => open.repeat(depth) + 'a,b' + close.repeat(depth);
const depthError = error => /exceeds max depth/.test(error.message) && !/call stack/i.test(error.message);
const ast = depth => {
  let node = { type: 'text', value: 'a' };
  for (let i = 0; i < depth; i++) node = { type: 'brace', nodes: [node] };
  return { type: 'root', nodes: [node] };
};

test('clean installs resolve every braces consumer to the reviewed local backport', () => {
  const expected = realpathSync(path.join(__dirname, '../vendor/braces/index.js'));
  assert.equal(realpathSync(require.resolve('braces')), expected);
  const fromMicromatch = createRequire(require.resolve('micromatch'));
  assert.equal(realpathSync(fromMicromatch.resolve('braces')), expected);
  const lock = JSON.parse(readFileSync(path.join(__dirname, '../package-lock.json'), 'utf8'));
  const entries = Object.entries(lock.packages).filter(([name]) => /(^|\/)node_modules\/braces$/.test(name));
  assert.equal(entries.length, 1, 'no unpatched nested registry copies');
  assert.equal(entries[0][1].resolved, 'vendor/braces');
  assert.equal(entries[0][1].link, true);
});

for (const [name, call] of Object.entries({
  default: value => braces(value),
  create: value => braces.create(value),
  parse: value => braces.parse(value),
  compile: value => braces.compile(value),
  expand: value => braces.expand(value),
  stringify: value => braces.stringify(value),
  array: value => braces(['normal/{a,b}', value]),
})) {
  test(`${name} rejects the CVE input before recursive stack exhaustion`, () => {
    for (const value of [nest(3500), nest(3500, '(', ')'), '{('.repeat(1800) + 'x' + ')}'.repeat(1800), '{'.repeat(4000), '('.repeat(4000)]) {
      assert.throws(() => call(value), depthError);
    }
  });
}

test('micromatch rejects nested braces and preserves its brace-free literal fast path', () => {
  assert.throws(() => micromatch.braces(nest(3500), { expand: true }), depthError);
  const literal = nest(3500, '(', ')');
  assert.deepEqual(micromatch.braces(literal), [literal]);
});

test('depth options can lower but cannot disable the hard ceiling', () => {
  assert.doesNotThrow(() => braces.parse(nest(100)));
  assert.throws(() => braces.parse(nest(101)), depthError);
  for (const maxDepth of [Infinity, NaN, 10000]) {
    assert.throws(() => braces.parse(nest(101), { maxDepth }), depthError);
    assert.throws(() => braces.compile(ast(101), { maxDepth }), depthError);
  }
  assert.doesNotThrow(() => braces.parse('{{a,b},c}', { maxDepth: 2 }));
  assert.throws(() => braces.parse('{{a,b},c}', { maxDepth: 1 }), depthError);
  assert.throws(() => braces.parse('{{a,b},c}', { maxDepth: 1.5 }), depthError);
});

for (const name of ['compile', 'expand', 'stringify']) {
  test(`${name} bounds direct AST depth and cyclic child graphs`, () => {
    assert.throws(() => braces[name](ast(3500)), depthError);
    const cycle = { type: 'root', nodes: [] };
    cycle.nodes.push(cycle);
    assert.throws(() => braces[name](cycle), depthError);
  });
}

test('expand rejects cyclic parent chains without hanging', () => {
  for (const length of [1, 2]) {
    const node = { type: 'paren', nodes: [{ type: 'text', value: 'a' }] };
    const parent = length === 1 ? node : { type: 'paren', parent: node };
    node.parent = parent;
    assert.throws(
      () => vm.runInNewContext('braces.expand(node)', { braces, node }, { timeout: 500 }),
      /AST parent chain contains a cycle/,
    );
  }
});

test('normal ranges, extglobs, escaping and parsed ASTs preserve their results', () => {
  assert.deepEqual(braces.expand('a{1..3}b{c,d}'), ['a1bc', 'a1bd', 'a2bc', 'a2bd', 'a3bc', 'a3bd']);
  assert.deepEqual(braces('{a,b{1..2}}'), ['(a|b(1|2))']);
  assert.deepEqual(braces.expand('{a,b{1..2}}'), ['a', 'b1', 'b2']);
  assert.deepEqual(braces.expand('file-{01..03}.ts'), ['file-01.ts', 'file-02.ts', 'file-03.ts']);
  assert.deepEqual(braces.expand('*(a|{b|c,d})'), ['*(a|b|c)', '*(a|d)']);
  assert.deepEqual(braces.expand(braces.parse('foo/({a,b})')), ['foo/(a)', 'foo/(b)']);
  assert.deepEqual(braces.expand(String.raw`\{a,b\}`), ['{a,b}']);
  for (const pattern of ['{{a}}', '{a,{b}}', '{{x}y}', '{}{a}']) {
    assert.equal(braces.stringify(braces.parse(pattern), { escapeInvalid: true }), pattern);
  }
  assert.throws(() => braces.expand('{1..100000}'), /range limit/);
  assert.throws(() => braces.parse('x'.repeat(10001)), /max characters/);
});

test('micromatch and fast-glob retain build-tool matching behavior', async () => {
  const patterns = ['app/**/*.{ts,tsx}', '!**/*.test.*'];
  assert.deepEqual(micromatch(['app/page.tsx', 'app/api.ts', 'app/x.test.ts', 'app/style.css'], patterns), ['app/page.tsx', 'app/api.ts']);
  const options = { cwd: path.join(__dirname, '..'), onlyFiles: true };
  const files = await glob(['app/{commercial,page}.tsx'], options);
  assert.deepEqual(files.sort(), ['app/commercial.tsx', 'app/page.tsx']);
  assert.deepEqual(glob.sync(['app/{commercial,page}.tsx'], options).sort(), files);
  assert.throws(() => glob.generateTasks(nest(3500)), depthError);
});
