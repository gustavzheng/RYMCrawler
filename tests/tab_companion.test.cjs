const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const url = 'https://rateyourmusic.com/release/album/example/example/';
const base = 'http://127.0.0.1:12345/' + 'x'.repeat(43) + '/';
const tagged = url + '#rym-crawler=12345.' + 'x'.repeat(43);

function setup({tracked = true, done = url, current = url, missing = false} = {}) {
  const storage = tracked ? {'tab:7': {base}} : {};
  const removed = [], requests = [], updates = [];
  const sandbox = {
    URL, AbortSignal,
    fetch: async (address, options) => {
      requests.push(address);
      return {ok: true, json: async () => ({url, done_url: done})};
    },
    chrome: {
      storage: {session: {
        get: async key => key ? {[key]: storage[key]} : {...storage},
        set: async entries => Object.assign(storage, entries),
        remove: async key => {delete storage[key];}
      }},
      tabs: {
        get: async id => { if (missing) throw Error('No tab'); return {id, url: current}; },
        remove: async id => removed.push(id),
        update: async (id, properties) => updates.push({id, ...properties})
      },
      runtime: {onMessage: {addListener() {}}},
      alarms: {create() {}, onAlarm: {addListener() {}}}
    }
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync('edge-tab-companion/background.js', 'utf8'), sandbox);
  return {sandbox, removed, requests, updates, storage};
}

test('closes only the registered ID after success and acknowledges', async () => {
  const s = setup();
  await s.sandbox.poll(7, url);
  assert.deepEqual(s.removed, [7]);
  assert.ok(s.requests.includes(base + 'closed'));
  assert.equal(s.storage['tab:7'], undefined);
});
test('pending or failed import does not close', async () => {
  const s = setup({done: null});
  await s.sandbox.poll(7, url);
  assert.deepEqual(s.removed, []);
});
test('an unrelated tab at the same URL is not closed', async () => {
  const s = setup();
  await s.sandbox.poll(8, url);
  assert.deepEqual(s.removed, []);
  assert.deepEqual(s.requests, []);
});
test('navigation away from the confirmed album prevents closing', async () => {
  const s = setup({current: 'https://rateyourmusic.com/'});
  await s.sandbox.poll(7, url);
  assert.deepEqual(s.removed, []);
});
test('registers directly on the release page without another navigation', async () => {
  const s = setup({tracked: false, done: null});
  const reply = await s.sandbox.poll(7, tagged);
  assert.equal(s.storage['tab:7'].base, base);
  assert.ok(s.requests.includes(base + 'registered'));
  assert.equal(reply.registered, true);
  assert.deepEqual(s.updates, []);
});
test('unmarked pages and invalid bridge markers cannot register', async () => {
  for (const address of [url, base + 'start', tagged.replace('12345.', '65536.'),
    tagged.replace('rateyourmusic.com', 'example.com')]) {
    const s = setup({tracked: false, done: null});
    await s.sandbox.poll(7, address);
    assert.deepEqual(s.requests, []);
    assert.equal(s.storage['tab:7'], undefined);
  }
});
test('registration is retried after a transient acknowledgement error', async () => {
  const s = setup({tracked: false, done: null});
  const fetch = s.sandbox.fetch;
  let fail = true;
  s.sandbox.fetch = async (...args) => {
    if (args[0].endsWith('/registered') && fail) {
      fail = false;
      throw Error('Temporary connection error');
    }
    return fetch(...args);
  };
  await s.sandbox.poll(7, tagged);
  assert.equal(s.storage['tab:7'].registered, false);
  const reply = await s.sandbox.poll(7, tagged);
  assert.equal(reply.registered, true);
  assert.equal(s.storage['tab:7'].registered, true);
});
test('user-closed tab can acknowledge a committed result', async () => {
  const s = setup({missing: true});
  await s.sandbox.poll(7, url);
  assert.deepEqual(s.removed, []);
  assert.ok(s.requests.includes(base + 'closed'));
});

test('content script removes only its registered marker without navigation', async () => {
  for (const [hash, registered, expected] of [
    ['#rym-crawler=12345.' + 'x'.repeat(43), true, 1],
    ['#rym-crawler=12345.' + 'x'.repeat(43), false, 0],
    ['#reviews', true, 0]
  ]) {
    const replaced = [], scheduled = [];
    const sandbox = {
      chrome: {runtime: {sendMessage: async () => ({again: true, registered})}},
      location: {hash, pathname: '/release/album/example/example/', search: '?test=1'},
      history: {state: {existing: 1}, replaceState: (...args) => replaced.push(args)},
      setTimeout: (...args) => scheduled.push(args)
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync('edge-tab-companion/content.js', 'utf8'), sandbox);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(replaced.length, expected);
    if (expected) assert.equal(replaced[0][2], '/release/album/example/example/?test=1');
    assert.equal(scheduled.length, 1);
  }
});
