const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const react = require('../node_modules/react');
const { renderToStaticMarkup } = require('../node_modules/react-dom/server');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/local-import-failure.tsx'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const loaded = { exports: {} };
vm.runInNewContext(code, { module: loaded, exports: loaded.exports, require: name => require('../node_modules/' + name) });
function render(connect = false, disabled = false) {
  return renderToStaticMarkup(react.createElement(loaded.exports.default, {
    failure: { title: '보관함 업로드에 실패했습니다.', message: '내 컴퓨터의 다운로드는 끝났습니다. 다시 시도하세요.', connect },
    disabled, onRetry() {}, onUpload() {},
  }));
}
test('import failure renders an accessible persistent message and recovery actions', () => {
  const html = render();
  assert.match(html, /role="alert"/); assert.match(html, /tabindex="-1"/);
  assert.match(html, /보관함 업로드에 실패했습니다/); assert.match(html, /다운로드는 끝났습니다/);
  assert.match(html, /현재 링크로 다시 가져오기/); assert.match(html, /MP4 파일로 업로드/);
});
test('connection failures do not offer an import retry before pairing', () => {
  assert.doesNotMatch(render(true), /현재 링크로 다시 가져오기/);
});
test('pending actions disable both recovery buttons', () => {
  assert.equal((render(false, true).match(/disabled=""/g) || []).length, 2);
});
