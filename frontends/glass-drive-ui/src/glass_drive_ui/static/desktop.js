const workerId = window.location.pathname.split('/').filter(Boolean).at(-1);
const params = new URLSearchParams(window.location.search);
const signedToken = params.get('gh_token') || '';

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
let rfbImportAttempt = 0;
let desktopRefreshTimer = 0;
let desktopRefreshInFlight = false;
let desktopRefreshDelayMs = 5000;

function withAuth(url) {
  if (!signedToken || /(?:^|[?&])gh_token=/.test(String(url || ''))) return url;
  return `${url}${url.includes('?') ? '&' : '?'}gh_token=${encodeURIComponent(signedToken)}`;
}

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

function refreshDelayForLiveState(data) {
  if (document.hidden) return 15000;
  const workerState = String(data?.worker?.state || '').trim().toLowerCase();
  const runState = String(data?.latest_run?.state || '').trim().toLowerCase();
  return ['queued', 'running'].includes(runState) || ['created', 'starting', 'resuming'].includes(workerState)
    ? 5000
    : 15000;
}

function scheduleDesktopRefresh(delayMs = desktopRefreshDelayMs) {
  if (desktopRefreshTimer) window.clearTimeout(desktopRefreshTimer);
  desktopRefreshTimer = window.setTimeout(() => {
    void connectDesktop();
  }, delayMs);
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
  if (desktopRefreshInFlight) {
    scheduleDesktopRefresh(5000);
    return;
  }
  desktopRefreshInFlight = true;
  try {
    const response = await fetch(withAuth(`/api/worker/${workerId}/live`));
    if (!response.ok) {
      setStatus('Desktop unavailable', 'GlassHive could not load the worker runtime details for this sandbox.');
      return;
    }
    const data = await response.json();
    desktopRefreshDelayMs = refreshDelayForLiveState(data);
    const runtime = data.runtime_details || {};
    const viewAvailable = Boolean(runtime.view_available || runtime.view_url);
    buildDesktopTitle(data.worker?.name, data.project_title);

    if (!viewAvailable) {
      setStatus('Desktop unavailable', 'This worker does not currently expose a live desktop surface.');
      return;
    }

    const wsScheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsScheme}//${window.location.host}${withAuth(`/novnc/${workerId}/websockify`)}`;
    const password = '';

    if (rfb && wsUrl === currentWsUrl && password === currentPassword) {
      return;
    }

    currentWsUrl = wsUrl;
    currentPassword = password;

    setStatus('Attaching live sandbox…', 'GlassHive is connecting directly to the worker desktop and enabling clipboard sync.');

    const modulePath = withAuth(`/novnc/${workerId}/core/rfb.js?attempt=${rfbImportAttempt}`);
    let RFB;
    try {
      ({ default: RFB } = await import(modulePath));
    } catch (error) {
      rfbImportAttempt += 1;
      setStatus(
        'Desktop reconnecting…',
        'GlassHive is refreshing the live desktop client and will attach again automatically.',
      );
      console.debug('rfb import failed', error);
      return;
    }

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
  } finally {
    desktopRefreshInFlight = false;
    scheduleDesktopRefresh();
  }
}

document.addEventListener('visibilitychange', () => {
  scheduleDesktopRefresh(0);
});

void connectDesktop();
