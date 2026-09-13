// Messages originate in the isolated extension world, never from page scripts.
async function tick() {
  try {
    const reply = await chrome.runtime.sendMessage({type: 'rym-tick'});
    if (reply?.registered && /^#rym-crawler=\d{1,5}\.[A-Za-z0-9_-]{43}$/.test(location.hash)) {
      // Remove only our own marker without navigating or reloading the page.
      history.replaceState(history.state, '', location.pathname + location.search);
    }
    if (reply?.again) setTimeout(tick, 1000);
  } catch (_) { /* The extension was disabled or the crawler exited. */ }
}
tick();
