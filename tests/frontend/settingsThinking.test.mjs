import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/components/SettingsPanel.tsx', import.meta.url), 'utf8');
const parsed = ts.createSourceFile('SettingsPanel.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declarations = new Map();

function collect(node) {
  if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
    declarations.set(node.name.text, node);
  }
  ts.forEachChild(node, collect);
}

collect(parsed);
const payload = declarations.get('payload');
const addProvider = declarations.get('addProvider');
assert.ok(payload);
assert.ok(addProvider);

function compile(expression) {
  return ts.transpileModule(expression, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
}

function serialize(rows) {
  const code = compile(`${payload.initializer.getText(parsed)};`);
  return JSON.parse(JSON.stringify(runInNewContext(code, {
    rows, PLACEHOLDERS: new Set(['—', '官方', '（未配置）', '本机', '内建默认']),
  })));
}

const row = {
  slot: 'custom', name: 'test-relay', model: 'reasoner', baseUrl: 'https://relay.example.com/v1',
  role: '主', protocol: 'openai',
};

test('custom reasoning declaration and dialect reach the save payload', () => {
  const saved = serialize([{ ...row, thinking: true, thinkingFormat: 'deepseek' }])[0];
  assert.equal(saved.thinking, true);
  assert.equal(saved.thinkingFormat, 'deepseek');
});

test('disabled reasoning and native Anthropic do not send a compatible dialect', () => {
  const disabled = serialize([{ ...row, thinking: false, thinkingFormat: 'deepseek' }])[0];
  assert.equal(disabled.thinking, false);
  assert.equal(Object.hasOwn(disabled, 'thinkingFormat'), false);
  const native = serialize([{ ...row, protocol: 'anthropic', thinking: true, thinkingFormat: 'deepseek' }])[0];
  assert.equal(native.thinking, true);
  assert.equal(Object.hasOwn(native, 'thinkingFormat'), false);
});

test('existing undeclared models and direct API rows omit the reasoning declaration', () => {
  const undeclared = serialize([row])[0];
  assert.equal(Object.hasOwn(undeclared, 'thinking'), false);
  assert.equal(Object.hasOwn(undeclared, 'thinkingFormat'), false);
  const direct = serialize([{ ...row, slot: 'direct-api', thinking: true, thinkingFormat: 'openai' }])[0];
  assert.equal(Object.hasOwn(direct, 'thinking'), false);
  assert.equal(Object.hasOwn(direct, 'thinkingFormat'), false);
});

async function saveScenario({ channel = 'chat', transport = 'openclaw', restartResult = { ok: true }, restartError } = {}) {
  const calls = [];
  const notes = [];
  const saveCurrent = declarations.get('saveCurrent');
  const callback = runInNewContext(compile(`(${saveCurrent.initializer.getText(parsed)});`), {
    useCallback: callback => callback,
    chan: channel,
    chatRows: [row],
    transRows: [row],
    mediaRows: { image: [row] },
    PLACEHOLDERS: new Set(),
    fetchWithRetry: callback => callback(),
    saveModelConfig: async () => {
      calls.push('save');
      return { transport, channels: { chat: { rows: [] }, transcribe: { rows: [] } } };
    },
    restartGateway: async () => {
      calls.push('restart');
      if (restartError) throw restartError;
      return restartResult;
    },
    setSaving() {},
    setSavedNote: note => notes.push(note),
    setChatRows() {},
    setTransRows() {},
    setMediaRows() {},
    refreshEnv() {},
    setTimeout() {},
  });
  await callback();
  return { calls, notes };
}

test('saving OpenClaw chat configuration restarts the gateway after saving', async () => {
  const { calls, notes } = await saveScenario();
  assert.deepEqual(calls, ['save', 'restart']);
  assert.match(notes.at(-1), /新配置已生效/);
});

test('direct API and media configuration saves do not restart OpenClaw', async () => {
  for (const scenario of [{ transport: 'api' }, { channel: 'image' }]) {
    const { calls } = await saveScenario(scenario);
    assert.deepEqual(calls, ['save']);
  }
});

test('gateway restart failures preserve the successful save and report the failure', async () => {
  for (const scenario of [{ restartResult: { ok: false, note: 'not ready' } }, { restartError: new Error('offline') }]) {
    const { calls, notes } = await saveScenario(scenario);
    assert.deepEqual(calls, ['save', 'restart']);
    assert.match(notes.at(-1), /配置已保存，但网关/);
    assert.doesNotMatch(notes.at(-1), /新配置已生效/);
  }
});

test('new custom rows default to reasoning disabled and the OpenAI dialect', () => {
  let added;
  const callback = runInNewContext(compile(`(${addProvider.initializer.getText(parsed)});`), {
    setChatRows: update => { added = JSON.parse(JSON.stringify(update([]))); },
  });
  callback();
  assert.equal(added[0].thinking, false);
  assert.equal(added[0].thinkingFormat, 'openai');
});
