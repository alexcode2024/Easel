import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const source = readFileSync(new URL('../../web/frontend/src/components/PlatformAccessBanner.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.ReactJSX,
  },
}).outputText;
const exports = {};
runInNewContext(compiled, { exports, require, Date });
const render = (status) => renderToStaticMarkup(React.createElement(exports.default, {
  statuses: { [status.platform]: status },
}));
const idle = {
  resource: 'xiaohongshu:default', platform: 'xiaohongshu', platform_name: '小红书',
  concurrency: 1, active: null, external_busy: false, queue: [], queued: 0,
  risk: { active: false, until: 0, reason: '', triggered_at: 0 },
};

test('queue banner displays the active task and this task position', () => {
  const html = render({ ...idle,
    active: { id: 'active', label: '抓取对标账号', started_at: 1 },
    queue: [{ id: 'sample', platform: 'xiaohongshu', label: 'xiaohongshu作品表现监控',
      state: 'queued', position: 1, queued_at: 2 }],
    queued: 1,
  });
  assert.match(html, /正在访问小红书：抓取对标账号/);
  assert.match(html, /排队第 1 位：xiaohongshu作品表现监控/);
});

test('idle access status does not render a banner', () => {
  assert.equal(renderToStaticMarkup(React.createElement(exports.default, { statuses: { xiaohongshu: idle } })), '');
});
