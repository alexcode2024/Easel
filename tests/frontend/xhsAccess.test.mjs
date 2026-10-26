import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const source = readFileSync(new URL('../../web/frontend/src/components/XhsAccessBanner.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const exports = {};
runInNewContext(compiled, { exports, require, Date });
const idle = {
  resource: 'xiaohongshu', concurrency: 1, active: null, queue: [], queued: 0,
  risk: { active: false, until: 0, reason: '', triggered_at: 0 },
};
const render = status => renderToStaticMarkup(React.createElement(exports.default, { status }));

test('idle status always explains single-account concurrency', () => {
  assert.match(render(idle), /小红书访问空闲/);
  assert.match(render(idle), /同一登录态并发 1/);
});

test('queue status displays active task and waiting positions', () => {
  const html = render({ ...idle, active: { id: 'one', label: '读取作品', started_at: 1 }, queued: 1,
    queue: [{ id: 'two', label: '登录校验', position: 1, state: 'queued', queued_at: 1 }] });
  assert.match(html, /正在访问小红书：读取作品/);
  assert.match(html, /排队第 1 位：登录校验/);
});

test('risk status explains cooldown without promising automatic retries', () => {
  const html = render({ ...idle, risk: { active: true, until: 2000000000, reason: 'HTTP 461' } });
  assert.match(html, /小红书访问已暂停：HTTP 461/);
  assert.match(html, /本轮已停止，不会自动重试/);
});

test('refresh only opts into sync for a positive download budget', async () => {
  const source = readFileSync(new URL('../../web/frontend/src/lib/api.ts', import.meta.url), 'utf8');
  const parsed = ts.createSourceFile('api.ts', source, ts.ScriptTarget.Latest, true);
  const method = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'fetchBenchmarkNow');
  const compiled = ts.transpileModule(method.getText(parsed), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const exports = {}, calls = [];
  runInNewContext(compiled, { exports, encodeURIComponent, request: async url => { calls.push(url); } });
  await exports.fetchBenchmarkNow('account');
  await exports.fetchBenchmarkNow('account', 0);
  await exports.fetchBenchmarkNow('account', 3);
  assert.equal(calls[0], '/api/benchmarks/account/fetch');
  assert.match(calls[1], /sync=false/);
  assert.match(calls[2], /sync=true&max_downloads=3/);
});
