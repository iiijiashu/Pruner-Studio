(async () => {
  const token = document.querySelector('meta[name="pruner-browser-session"]').content;
  history.replaceState(null, '', location.pathname);
  const cache = await caches.open('pruner-browser-session');
  await cache.put('/session', new Response(token));
  const workers = navigator.serviceWorker;
  await workers.register('/session-worker.js', { scope: '/' });
  if (!workers.controller) {
    await new Promise(resolve => workers.addEventListener('controllerchange', resolve, { once: true }));
  }
  location.replace(location.pathname);
})().catch(() => {
  document.body.textContent = '窗口授权失败，请从应用重新打开。';
});
