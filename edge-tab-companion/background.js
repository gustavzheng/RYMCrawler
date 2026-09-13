const busy = new Set();
function bridgeAddress(url) {
  if (!release(url)) return null;
  const match = new URL(url).hash.match(/^#rym-crawler=(\d{1,5})\.([A-Za-z0-9_-]{43})$/);
  if (!match || Number(match[1]) < 1 || Number(match[1]) > 65535) return null;
  return `http://127.0.0.1:${Number(match[1])}/${match[2]}/`;
}

function release(url) {
  try {
    const u = new URL(url);
    return u.origin === 'https://rateyourmusic.com' &&
      /^\/release\/[^/]+\/[^/]+\/[^/]+\/$/.test(u.pathname) ? u.origin + u.pathname : null;
  } catch (_) { return null; }
}

async function request(base, action, post = false) {
  const response = await fetch(base + action, {
    method: post ? 'POST' : 'GET', cache: 'no-store',
    headers: post ? {'X-RYM-Bridge': '1'} : {},
    signal: AbortSignal.timeout(5000)
  });
  if (!response.ok) throw new Error(`Bridge HTTP ${response.status}`);
  return post ? null : response.json();
}

async function poll(id, senderUrl) {
  if (busy.has(id)) return {again: true};
  busy.add(id);
  const key = `tab:${id}`;
  try {
    let entry = (await chrome.storage.session.get(key))[key];
    if (!entry && bridgeAddress(senderUrl)) {
      const base = bridgeAddress(senderUrl);
      const state = await request(base, 'state');
      if (!release(state.url)) return {again: false};
      entry = {base, registered: false};
      await chrome.storage.session.set({[key]: entry});
    }
    if (!entry) return {again: false};
    if (entry.registered === false) {
      await request(entry.base, 'registered', true);
      entry.registered = true;
      await chrome.storage.session.set({[key]: entry});
    }
    const state = await request(entry.base, 'state');
    if (state.done_url) {
      // Use the stored ID, never the active tab or a title/URL search.
      let tab;
      try { tab = await chrome.tabs.get(id); } catch (_) { /* Already closed. */ }
      if (tab) {
        if (!release(state.done_url) || release(tab.pendingUrl || tab.url) !== release(state.done_url)) {
          return {again: true, registered: true}; // User navigated elsewhere: leave it untouched.
        }
        await chrome.tabs.remove(id);
      }
      await request(entry.base, 'closed', true);
      await chrome.storage.session.remove(key);
      return {again: false};
    }
    return {again: true, registered: true};
  } catch (_) {
    return {again: true}; // A transient local error must not close a tab.
  } finally { busy.delete(id); }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.type !== 'rym-tick' || sender.frameId !== 0 || !sender.tab) return;
  poll(sender.tab.id, sender.url).then(respond, () => respond({again: false}));
  return true;
});

// Also works if the owned tab is suspended, navigated, or closed by the user.
chrome.alarms.create('rym-poll', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== 'rym-poll') return;
  const entries = await chrome.storage.session.get(null);
  await Promise.all(Object.keys(entries).filter(k => k.startsWith('tab:'))
    .map(k => poll(Number(k.slice(4)))));
});
