const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('deploy/vps/landing/index.html', 'utf8');
const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).find(s => s.includes("const form = document.getElementById('demoForm')"));
const source = script.slice(script.lastIndexOf('(function(){', script.indexOf("const form = document.getElementById('demoForm')")));
assert.ok(source);
assert.match(html, /name="affiliate_code"/);

async function run(search, storage, blocked = false) {
  const fields = {};
  let submit, sent;
  const form = {
    dataset: {endpoint: '/api/marketing/leads'},
    querySelector(selector) {
      const key = /name="([^"]+)"/.exec(selector)?.[1];
      return key ? (fields[key] ||= {value: ''}) : null;
    },
    addEventListener(name, fn) { if (name === 'submit') submit = fn; },
    reset() { Object.values(fields).forEach(f => { f.value = ''; }); },
  };
  vm.runInNewContext(source, {
    URLSearchParams, Date, Object, Array, encodeURIComponent,
    document: {getElementById: id => id === 'demoForm' ? form : {}, referrer: ''},
    window: {location: {search, href: 'https://www.cooperativems.com/'}, dataLayer: []},
    sessionStorage: {
      setItem(k, v) { if (blocked) throw Error('blocked'); storage[k] = v; },
      getItem(k) { if (blocked) throw Error('blocked'); return storage[k]; },
    },
    FormData: class { entries() { return Object.entries(fields).map(([k, f]) => [k, f.value]); } },
    fetch: async (_, options) => { sent = JSON.parse(options.body); return {ok: true}; },
  });
  await submit({preventDefault() {}});
  return {sent, fields};
}
(async () => {
  const storage = {};
  const first = await run('?ref=CMA-AB1234', storage);
  assert.equal(first.sent.affiliate_code, 'CMA-AB1234');
  assert.equal(first.fields.affiliate_code.value, 'CMA-AB1234');
  assert.equal((await run('?utm_source=website', storage)).sent.affiliate_code, 'CMA-AB1234');
  assert.equal((await run('?affiliate_code=CML-CD9876', {}, true)).sent.affiliate_code, 'CML-CD9876');
  assert.equal((await run('', {})).sent.affiliate_code, '');
  console.log('Affiliate referral capture, navigation persistence, reset and storage fallback passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
