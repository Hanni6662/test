import { test } from 'node:test';
import assert from 'node:assert/strict';
import { originalExtension, retryable, validateFileMetadata, makePreferences, recommendationMessage } from '../src/domain.js';

test('recommendation failure and uncertain state give actionable and distinct messages', () => {
  assert.match(recommendationMessage('failed', 'V34_REFERENCE_NOT_OBSERVED'), /未能读取主参考图/);
  assert.match(recommendationMessage('failed', 'V34_DIFY_WORKFLOW_FAILED'), /重试/);
  assert.match(recommendationMessage('failed'), /无需重新上传/);
  assert.match(recommendationMessage('reconciling', 'V34_START_OBSERVATION_EXHAUSTED'), /刷新状态/);
  assert.match(recommendationMessage('reconciling'), /核实/);
  assert.match(recommendationMessage('awaiting_recommendation'), /自动显示/);
});
import { isTrustedBrowserRequest } from '../server/proxy.js';

test('download validates type and complete body before saving', () => {
  const blob = new Blob(['abcd']);
  assert.equal(originalExtension(blob, new Headers({ 'content-type': 'image/png', 'content-length': '4' })), 'png');
  for (const headers of [
    { 'content-type': 'application/json', 'content-length': '4' },
    { 'content-type': 'image/png', 'content-length': '7' },
    { 'content-type': 'image/png' },
    { 'content-type': 'image/png', 'content-length': '-4' },
  ]) assert.throws(() => originalExtension(blob, new Headers(headers)));
});
test('unknown, saved, and pre-generation failures cannot be retried', () => {
  assert.equal(retryable({ status: 'failed', error: { code: 'IMAGE_GENERATION_FAILED' } }), true);
  for (const item of [{ status: 'reconciling' }, { status: 'saving' }, { status: 'failed', image_id: 'saved' },
    { status: 'failed', error: { code: 'V34_PRE_GENERATION_FAILED' } },
    { status: 'failed', error: { code: 'GENERATION_PERMIT_EXPIRED_BEFORE_ACCEPTANCE' } }]) assert.equal(retryable(item), false);
});
test('preference submission validates custom text and server options', () => {
  const keys = ['language', 'usage_scene', 'visual_style', 'selling_points'];
  const options = { platform: ['TEMU'], ...Object.fromEntries(keys.map(k => [k, { choices: [{ code: 'A' }], allow_agent_design: true, allow_custom: true }])) };
  const modes = Object.fromEntries(keys.map(k => [k, 'D']));
  assert.equal(makePreferences(modes, {}, 'TEMU', options).language.mode, 'D');
  modes.language = 'custom';
  assert.throws(() => makePreferences(modes, { language: '   ' }, 'TEMU', options));
  assert.deepEqual(makePreferences(modes, { language: '中文' }, 'TEMU', options).language, { mode: 'custom', text: '中文' });
  assert.throws(() => makePreferences(modes, { language: '中文' }, '非法平台', options));
});
test('reject invalid upload formats, over-limit files and excess images', () => {
  const file = { type: 'image/png', size: 10 };
  assert.doesNotThrow(() => validateFileMetadata([file]));
  for (const files of [[], Array(5).fill(file), [{ type: 'image/gif', size: 10 }], [{ ...file, size: 11 * 1024 ** 2 }]]) assert.throws(() => validateFileMetadata(files));
});
test('local proxy cannot turn foreign or missing write origins into trusted requests', () => {
  const req = (method, extra = {}) => ({ method, headers: { host: '127.0.0.1:5173', ...extra } });
  assert.equal(isTrustedBrowserRequest(req('POST', { origin: 'http://127.0.0.1:5173' }), 5173), true);
  assert.equal(isTrustedBrowserRequest(req('POST', { referer: 'http://localhost:5173/' }), 5173), true);
  assert.equal(isTrustedBrowserRequest(req('POST', { origin: 'https://attacker.invalid' }), 5173), false);
  assert.equal(isTrustedBrowserRequest(req('POST', { origin: 'null' }), 5173), false);
  assert.equal(isTrustedBrowserRequest(req('POST'), 5173), false);
  assert.equal(isTrustedBrowserRequest(req('GET', { host: 'attacker.invalid:5173' }), 5173), false);
  assert.equal(isTrustedBrowserRequest(req('GET', { 'sec-fetch-site': 'cross-site' }), 5173), false);
});
