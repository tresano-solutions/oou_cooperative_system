const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../deploy/vps/landing/sign-in.js'), 'utf8');

async function submit(code, response) {
  const state = { requests: [], redirects: [] };
  const button = {};
  const input = { value: code, focus() {} };
  const status = {};
  const form = { querySelector: () => button, addEventListener: (_, handler) => { state.submit = handler; } };
  const elements = { 'cooperative-sign-in': form, 'cooperative-code': input, status };
  vm.runInNewContext(source, {
    document: { getElementById: id => elements[id] },
    window: { location: { assign: url => state.redirects.push(url) } },
    fetch: async (url, options) => {
      state.requests.push({ url, options });
      if (response instanceof Error) throw response;
      return response;
    }, URL, AbortController, setTimeout, clearTimeout
  });
  await state.submit({ preventDefault() {} });
  return { ...state, button, status };
}
const valid = base_url => ({ ok: true, json: async () => ({ success: true, tenant: { code: 'test-society', base_url } }) });

test('normalizes code and opens validated HTTPS login without credentials or referrer', async () => {
  const result = await submit(' TEST-SOCIETY ', valid('https://test-society.cooperativems.com/'));
  assert.deepEqual(result.redirects, ['https://test-society.cooperativems.com/login']);
  assert.equal(result.requests[0].options.credentials, 'omit');
  assert.equal(result.requests[0].options.referrerPolicy, 'no-referrer');
  assert.equal(result.button.disabled, false);
});
test('rejects URLs and invalid codes before requesting', async () => {
  for (const code of ['', 'https://example.com', '../admin', '<script>']) {
    const result = await submit(code);
    assert.equal(result.requests.length, 0);
    assert.equal(result.redirects.length, 0);
  }
});
test('unknown code stays on page with generic error', async () => {
  const result = await submit('test-society', { ok: false, status: 404 });
  assert.equal(result.redirects.length, 0);
  assert.match(result.status.textContent, /Check your code/);
});
test('rejects external, insecure, credentialed and nonstandard-port destinations', async () => {
  for (const url of ['https://evil.test', 'http://test-society.cooperativems.com',
    'https://cooperativems.com.evil.test', 'https://user:pass@test-society.cooperativems.com',
    'https://test-society.cooperativems.com:8000']) {
    const result = await submit('test-society', valid(url));
    assert.equal(result.redirects.length, 0);
    assert.match(result.status.textContent, /try again/);
  }
});
test('network failure restores form', async () => {
  const result = await submit('test-society', new Error('offline'));
  assert.equal(result.button.disabled, false);
  assert.match(result.status.textContent, /try again/);
});
test('mismatched resolver response is rejected', async () => {
  const result = await submit('another-code', valid('https://test-society.cooperativems.com'));
  assert.equal(result.redirects.length, 0);
});
