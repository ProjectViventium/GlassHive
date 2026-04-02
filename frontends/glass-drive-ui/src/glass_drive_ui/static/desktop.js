const workerId = window.location.pathname.split('/').filter(Boolean).at(-1);
const runtimeBase = `${window.location.protocol}//${window.location.hostname}:8766`;

const stage = document.getElementById('desktop-stage');
const overlay = document.getElementById('desktop-overlay');
const statusEl = document.getElementById('desktop-status');
const detailEl = document.getElementById('desktop-detail');
const clipboardStatus = document.getElementById('clipboard-status');

let rfb = null;
let currentWsUrl = '';
let currentPassword = '';
let clipboardInterval = null;
let lastLocalClipboard = '';
let lastRemoteClipboard = '';

function setStatus(title, detail, { hideOverlay = false } = {}) {
  statusEl.textContent = title;
  detailEl.textContent = detail;
  overlay.hidden = hideOverlay;
}

function setClipboardStatus(text) {
  clipboardStatus.textContent = text;
}

function buildDesktopTitle(workerName, projectTitle) {
  const safeWorker = String(workerName || 'Worker').trim() || 'Worker';
  const safeProject = String(projectTitle || 'Project').trim() || 'Project';
  document.title = `GlassHive | ${safeWorker} - ${safeProject}`;
}

function stopClipboardSync() {
  if (clipboardInterval) {
    window.clearInterval(clipboardInterval);
    clipboardInterval = null;
  }
}

async function pushLocalClipboardToRemote(text) {
  if (!rfb || !text || text === lastRemoteClipboard) return;
  try {
    rfb.clipboardPasteFrom(text);
    lastLocalClipboard = text;
    setClipboardStatus('Clipboard sync: local → sandbox');
  } catch (error) {
    console.debug('clipboard paste failed', error);
  }
}

function installClipboardSync() {
  stopClipboardSync();

  window.addEventListener('paste', (event) => {
    const text = event.clipboardData?.getData('text/plain') || '';
    if (!text) return;
    void pushLocalClipboardToRemote(text);
  });

  const tryPollClipboard = async () => {
    if (!navigator.clipboard?.readText) return;
    try {
      const text = await navigator.clipboard.readText();
      if (!text || text === lastLocalClipboard) return;
      lastLocalClipboard = text;
      await pushLocalClipboardToRemote(text);
      setClipboardStatus('Clipboard sync: bi-directional');
    } catch (error) {
      setClipboardStatus('Clipboard sync: click desktop once to enable');
    }
  };

  const armClipboardOnGesture = async () => {
    try {
      await tryPollClipboard();
    } finally {
      window.removeEventListener('pointerdown', armClipboardOnGesture);
      window.removeEventListener('keydown', armClipboardOnGesture);
      clipboardInterval = window.setInterval(() => {
        void tryPollClipboard();
      }, 1500);
    }
  };

  window.addEventListener('pointerdown', armClipboardOnGesture, { once: true });
  window.addEventListener('keydown', armClipboardOnGesture, { once: true });
  void tryPollClipboard();
}

async function connectDesktop() {
  const response = await fetch(`/api/worker/${workerId}/live`);
  if (!response.ok) {
    setStatus('Desktop unavailable', 'GlassHive could not load the worker runtime details for this sandbox.');
    return;
  }
  const data = await response.json();
  const runtime = data.runtime_details || {};
  const viewUrl = String(runtime.view_url || '').trim();
  buildDesktopTitle(data.worker?.name, data.project_title);

  if (!viewUrl) {
    setStatus('Desktop unavailable', 'This worker does not currently expose a live desktop surface.');
    return;
  }

  const parsed = new URL(viewUrl);
  const wsScheme = parsed.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsPath = parsed.searchParams.get('path') || 'websockify';
  const wsUrl = `${wsScheme}//${parsed.host}/${wsPath}`;
  const password = parsed.searchParams.get('password') || '';

  if (rfb && wsUrl === currentWsUrl && password === currentPassword) {
    return;
  }

  currentWsUrl = wsUrl;
  currentPassword = password;

  setStatus('Attaching live sandbox…', 'GlassHive is connecting directly to the worker desktop and enabling clipboard sync.');

  const modulePath = `/novnc/${workerId}/core/rfb.js`;
  const { default: RFB } = await import(modulePath);

  if (rfb) {
    try {
      rfb.disconnect();
    } catch (error) {
      console.debug('rfb disconnect failed', error);
    }
    stage.replaceChildren();
  }

  rfb = new RFB(stage, wsUrl, {
    credentials: password ? { password } : {},
  });
  rfb.scaleViewport = true;
  rfb.background = '#000';
  rfb.focusOnClick = true;
  rfb.showDotCursor = true;

  rfb.addEventListener('connect', () => {
    setStatus('Sandbox connected', 'Click anywhere inside the desktop to steer directly. Clipboard sync is active when the browser allows it.', { hideOverlay: true });
    setClipboardStatus('Clipboard sync: bi-directional');
    try {
      rfb.focus();
    } catch (error) {
      console.debug('rfb focus failed', error);
    }
  });

  rfb.addEventListener('disconnect', (event) => {
    setStatus(
      event.detail.clean ? 'Desktop disconnected' : 'Desktop reconnecting…',
      event.detail.clean
        ? 'The sandbox desktop session ended. Reload the page or reopen the worker if needed.'
        : 'The desktop connection dropped. GlassHive will retry automatically.',
      { hideOverlay: false },
    );
  });

  rfb.addEventListener('clipboard', async (event) => {
    const text = String(event.detail?.text || '');
    if (!text) return;
    lastRemoteClipboard = text;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      }
      setClipboardStatus('Clipboard sync: bi-directional');
    } catch (error) {
      setClipboardStatus('Clipboard sync: sandbox → local blocked by browser');
    }
  });

  installClipboardSync();
}

void connectDesktop();
window.setInterval(() => {
  void connectDesktop();
}, 5000);
