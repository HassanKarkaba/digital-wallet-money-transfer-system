/* Status reads enhance the ordinary HTML status page and never admit money. */
(() => {
  const sessionChannel = 'BroadcastChannel' in window ? new BroadcastChannel('dwmt-session') : null;
  let invalidateView = () => {};
  document.addEventListener('submit', event => {
    const action = new URL(event.target.action || location.href, location.href);
    if (action.pathname === '/accounts/logout/') {
      sessionChannel?.postMessage({ kind: 'session-invalidation' });
      invalidateView();
    }
  });
  window.addEventListener('pagehide', () => sessionChannel?.close(), { once: true });

  const panel = document.getElementById('payment-status');
  if (!panel) return;
  const terminal = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
  if (terminal.has(panel.dataset.initialState)) return;
  const generation = Symbol('payment-view');
  let currentGeneration = generation;
  let controller;
  let timer;
  let finished = false;
  let lastVersion = Number(panel.dataset.initialVersion || 0);
  const started = performance.now();
  const labels = { REVIEW_PENDING: 'Under review', READY: 'Queued', SUBMITTED: 'Processing', UNKNOWN: 'Outcome unknown', COMPLETED: 'Completed', FAILED: 'Failed', CANCELLED: 'Cancelled' };
  const announce = text => {
    panel.classList.remove('sr-only');
    panel.classList.add('notice', 'info');
    panel.textContent = text;
  };
  invalidateView = () => {
    currentGeneration = null;
    controller?.abort();
    clearTimeout(timer);
  };
  if (sessionChannel) {
    sessionChannel.onmessage = event => {
      if (event.data?.kind === 'session-invalidation') {
        invalidateView();
        announce('The session changed in another tab. Refresh this reference to retrieve its current authorized result.');
      }
    };
  }
  const tick = async () => {
    if (performance.now() - started > 300000 || currentGeneration !== generation || finished) return;
    if (document.hidden) { timer = setTimeout(tick, 5000); return; }
    controller = new AbortController();
    try {
      const response = await fetch(panel.dataset.statusUrl, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' } });
      if (currentGeneration !== generation) return;
      if (response.status === 401) {
        finished = true;
        announce('Your session ended. Sign in again to retrieve the current result under this same reference.');
        return;
      }
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
      const result = await response.json();
      if (currentGeneration !== generation || result.id !== panel.dataset.operationId) return;
      if (Number(result.object_version) < lastVersion) return;
      lastVersion = Number(result.object_version);
      if (result.state !== panel.dataset.initialState) {
        announce(`Status updated: ${labels[result.state] || result.state}. Refresh this page to retrieve the current result and available actions.`);
      }
      if (terminal.has(result.state)) finished = true;
    } catch (error) {
      if (error.name !== 'AbortError' && currentGeneration === generation) {
        announce('Status refresh is unavailable. Use the manual refresh link to check this same reference.');
      }
    } finally {
      if (currentGeneration === generation && !finished && performance.now() - started < 300000) timer = setTimeout(tick, 5000);
    }
  };
  timer = setTimeout(tick, 5000);
  window.addEventListener('pagehide', invalidateView, { once: true });
})();
