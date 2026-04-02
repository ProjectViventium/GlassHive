const params = new URLSearchParams(window.location.search);
const workerId = window.location.pathname.split('/').filter(Boolean).at(-1);
const projectId = params.get('project_id');
const requestedSurface = params.get('surface') === 'desktop' ? 'desktop' : 'terminal';
const runtimeBase = `${window.location.protocol}//${window.location.hostname}:8766`;
const uiBase = `${window.location.protocol}//${window.location.host}`;

const frame = document.getElementById('desktop-frame');
const overlay = document.getElementById('stage-overlay');
const overlayTitle = document.getElementById('overlay-title');
const overlayDetail = document.getElementById('overlay-detail');
const title = document.getElementById('watch-title');
const subtitle = document.getElementById('watch-subtitle');
const latestOutputInline = document.getElementById('latest-output-inline');
const latestOutputFull = document.getElementById('latest-output-full');
const statusLabel = document.getElementById('status-label');
const statePill = document.getElementById('watch-state');
const menu = document.getElementById('more-menu');
const menuToggle = document.getElementById('menu-toggle');
const resultToggle = document.getElementById('result-toggle');
const resultPanel = document.getElementById('result-panel');
const resultPanelTitle = document.getElementById('result-panel-title');
const resultClose = document.getElementById('result-close');
const surfaceTerminalButton = document.getElementById('surface-terminal');
const surfaceDesktopButton = document.getElementById('surface-desktop');
const openExternal = document.getElementById('open-external');
const openTerminalLink = document.getElementById('open-terminal-link');
const openWorkerConsole = document.getElementById('open-worker-console');

let activeSurface = requestedSurface;
let currentDesktopUrl = `${uiBase}/desktop/${workerId}`;
let currentTerminalUrl = `${runtimeBase}/ui/workers/${workerId}/terminal`;
let lastAttachedUrl = '';
let attachStartedAt = 0;
let retryTimers = [];
let frameReady = false;
let currentRunState = '';
let currentSummary = 'No run output yet.';
let currentFullOutput = 'No run output yet.';
let currentProjectTitle = projectId || 'Project';
let currentDeliverable = null;
let lastPromotedDeliverableKey = '';
let deliverablePromotionPending = false;

function syncDocumentTitle(workerName, projectTitle) {
  const safeWorkerName = String(workerName || 'Worker').trim() || 'Worker';
  const safeProjectTitle = String(projectTitle || 'Project').trim() || 'Project';
  document.title = `GlassHive | ${safeWorkerName} - ${safeProjectTitle}`;
}

function clearRetryTimers() {
  for (const timer of retryTimers) window.clearTimeout(timer);
  retryTimers = [];
}

function closeMenu() {
  menu.hidden = true;
  menuToggle.setAttribute('aria-expanded', 'false');
}

function closeResultPanel() {
  resultPanel.hidden = true;
  resultToggle.setAttribute('aria-expanded', 'false');
}

function openResultPanel() {
  if (!currentFullOutput.trim()) return;
  resultPanel.hidden = false;
  resultToggle.setAttribute('aria-expanded', 'true');
}

function forceReloadFrame(url) {
  frameReady = false;
  frame.src = 'about:blank';
  window.setTimeout(() => {
    frame.src = url;
  }, 180);
}

function scheduleReconnects(url) {
  clearRetryTimers();
  for (const delay of activeSurface === 'terminal' ? [2500, 7000] : [3500, 9000]) {
    retryTimers.push(window.setTimeout(() => {
      if (lastAttachedUrl === url && !frameReady) {
        forceReloadFrame(url);
      }
    }, delay));
  }
}

function attachView(url) {
  if (!url) return;
  if (lastAttachedUrl === url && frame.src === url) return;
  lastAttachedUrl = url;
  attachStartedAt = Date.now();
  frameReady = false;
  frame.src = url;
  scheduleReconnects(url);
}

function currentSurfaceUrl() {
  if (activeSurface === 'desktop') {
    return currentDesktopUrl || currentTerminalUrl;
  }
  return currentTerminalUrl || currentDesktopUrl;
}

function syncMenuLabels() {
  surfaceTerminalButton.dataset.active = String(activeSurface === 'terminal');
  surfaceDesktopButton.dataset.active = String(activeSurface === 'desktop');
  openExternal.textContent = activeSurface === 'desktop' ? 'Open current desktop in new tab' : 'Open current session in new tab';
}

function setSurface(surface, { force = false } = {}) {
  activeSurface = surface === 'desktop' ? 'desktop' : 'terminal';
  syncMenuLabels();
  const url = currentSurfaceUrl();
  if (force || lastAttachedUrl !== url || frame.src !== url) {
    attachView(url);
  }
  setOverlay(statePill.textContent || 'starting');
}

function summarizeOutput(data) {
  const runState = String(data.latest_run?.state || '').trim();
  const raw = String(data.latest_output || '').trim();
  const deliverable = data.deliverable || null;
  currentRunState = runState;

  if (runState === 'running') {
    if (deliverable && deliverable.preferred_surface === 'desktop' && deliverable.browser_url) {
      const label = String(deliverable.label || deliverable.workspace_path || deliverable.browser_url || 'Preview');
      const full = [
        `Preview available while the worker continues verification · ${label}`,
        deliverable.browser_url ? `Browser target: ${deliverable.browser_url}` : '',
        raw || '',
      ].filter(Boolean).join('\n\n');
      return {
        label: 'Live preview',
        panelTitle: 'Live preview',
        summary: `Preview available · ${label}`,
        full,
      };
    }
    const summary = activeSurface === 'desktop'
      ? 'Worker is actively executing. You are watching the live sandbox desktop for this run.'
      : 'Worker is actively executing. You are attached to the exact live session for this run.';
    const full = raw || (activeSurface === 'desktop'
      ? 'Worker is actively executing. You are watching the live sandbox desktop. Switch to Watch exact live session from the menu if you want the raw terminal session.'
      : 'Worker is actively executing. Open the current view or steer the worker from the ribbon controls if you need to intervene.');
    return {
      label: 'Live status',
      panelTitle: 'Live session details',
      summary,
      full,
    };
  }

  if (deliverable && runState === 'completed') {
    const label = String(deliverable.label || deliverable.workspace_path || deliverable.browser_url || 'Delivered result');
    const full = [
      `Delivered page ready · ${label}`,
      deliverable.browser_url ? `Browser target: ${deliverable.browser_url}` : '',
      raw,
    ].filter(Boolean).join('\n\n');
    return {
      label: 'Latest result',
      panelTitle: 'Delivered result',
      summary: `Delivered page ready · ${label}`,
      full,
    };
  }

  if (!raw) {
    const idle = data.worker?.state === 'ready' ? 'Worker is ready for the next instruction.' : 'No run output yet.';
    return {
      label: 'Worker status',
      panelTitle: 'Worker status',
      summary: idle,
      full: idle,
    };
  }

  const firstBlock = raw.split(/\n\s*\n|\n/)[0].trim();
  const summary = firstBlock.length <= 180 ? firstBlock : `${firstBlock.slice(0, 177)}...`;
  return {
    label: runState === 'completed' ? 'Latest result' : 'Worker status',
    panelTitle: runState === 'completed' ? 'Latest result' : 'Worker output',
    summary,
    full: raw,
  };
}

async function maybePromoteDeliverable(data) {
  const deliverable = data.deliverable || null;
  const runState = String(data.latest_run?.state || '').trim();
  const runId = String(data.latest_run?.run_id || '').trim();
  if (!deliverable || !deliverable.browser_url || deliverable.preferred_surface !== 'desktop') return;
  if (!['running', 'completed'].includes(runState)) return;
  const promotionKey = `${runId}:${deliverable.browser_url}`;
  if (!promotionKey || promotionKey === lastPromotedDeliverableKey || deliverablePromotionPending) return;

  deliverablePromotionPending = true;
  try {
    await postAction('action:browser', { url: deliverable.browser_url });
    lastPromotedDeliverableKey = promotionKey;
    activeSurface = 'desktop';
    setSurface('desktop', { force: true });
    window.setTimeout(() => {
      postAction('action:focus_browser').catch(() => {});
    }, 300);
  } catch (error) {
    console.debug('deliverable promotion failed', error);
  } finally {
    deliverablePromotionPending = false;
  }
}

function renderOutput(data) {
  const output = summarizeOutput(data);
  statusLabel.textContent = output.label;
  resultPanelTitle.textContent = output.panelTitle;
  latestOutputInline.textContent = output.summary;
  latestOutputFull.textContent = output.full;
  currentSummary = output.summary;
  currentFullOutput = output.full;
  resultToggle.hidden = !output.full.trim();
  if (!output.full.trim()) {
    closeResultPanel();
  }
}

async function postAction(action, payload) {
  const response = await fetch(`/api/worker/${workerId}/${action.startsWith('action:') ? 'action/' + action.split(':', 2)[1] : action}`, {
    method: 'POST',
    headers: payload ? { 'Content-Type': 'application/json' } : {},
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

function setOverlay(state, detail) {
  const connecting = attachStartedAt && !frameReady && Date.now() - attachStartedAt < 12000;
  const waiting = state === 'starting' || state === 'paused' || connecting;
  overlay.hidden = !waiting;
  if (!waiting) return;

  if (state === 'paused') {
    overlayTitle.textContent = 'Worker paused';
    overlayDetail.textContent = activeSurface === 'desktop'
      ? 'The sandbox is frozen. Press Play to continue, or open the desktop in a new tab and steer directly.'
      : 'The exact live session is frozen. Press Play to continue the same run, or steer it directly from this session.';
    return;
  }

  if (connecting) {
    overlayTitle.textContent = activeSurface === 'desktop' ? 'Attaching live sandbox…' : 'Attaching exact live session…';
    overlayDetail.textContent = activeSurface === 'desktop'
      ? 'The desktop is waking up. If it takes more than a few seconds, open the current desktop in a new tab.'
      : 'We are connecting to the worker’s exact running session. If it takes more than a few seconds, open the current session in a new tab.';
    return;
  }

  overlayTitle.textContent = activeSurface === 'desktop' ? 'Preparing live sandbox…' : 'Preparing exact live session…';
  overlayDetail.textContent = detail || (activeSurface === 'desktop'
    ? 'The desktop will attach automatically when the sandbox is ready.'
    : 'The exact worker session will appear here as soon as the sandbox is ready.');
}

async function refresh() {
  const response = await fetch(`/api/worker/${workerId}/live`);
  if (!response.ok) return;
  const data = await response.json();
  const worker = data.worker;
  const runtime = data.runtime_details || {};
  currentProjectTitle = String(data.project_title || worker.project_id || projectId || 'Project');
  currentDeliverable = data.deliverable || null;

  title.textContent = worker.name || 'Worker live view';
  subtitle.textContent = `${worker.profile || 'worker'} · ${currentProjectTitle}`;
  syncDocumentTitle(worker.name, currentProjectTitle);
  statePill.textContent = worker.state;

  currentDesktopUrl = `${uiBase}/desktop/${workerId}`;
  currentTerminalUrl = `${runtimeBase}/ui/workers/${workerId}/terminal`;

  renderOutput(data);
  await maybePromoteDeliverable(data);
  syncMenuLabels();
  setSurface(activeSurface, { force: false });
  setOverlay(
    worker.state,
    worker.state === 'paused'
      ? 'Use Play to continue the same sandbox.'
      : activeSurface === 'desktop'
        ? 'The desktop will attach automatically when the sandbox is ready.'
        : 'The exact worker session will attach automatically when the sandbox is ready.'
  );
}

for (const button of document.querySelectorAll('[data-action]')) {
  button.addEventListener('click', async () => {
    const action = button.getAttribute('data-action');
    try {
      if (['pause', 'resume', 'interrupt', 'terminate'].includes(action)) {
        await postAction(`action/${action}`.replace('action/action/', 'action/'));
      } else {
        await postAction(`action:${action}`);
      }
      closeMenu();
      await refresh();
    } catch (error) {
      statusLabel.textContent = 'Worker status';
      latestOutputInline.textContent = error.message;
      latestOutputFull.textContent = error.message;
      openResultPanel();
    }
  });
}

surfaceTerminalButton.addEventListener('click', () => {
  closeMenu();
  setSurface('terminal', { force: true });
});

surfaceDesktopButton.addEventListener('click', () => {
  closeMenu();
  setSurface('desktop', { force: true });
});

frame.addEventListener('load', () => {
  if (!frame.src || frame.src === 'about:blank') return;
  frameReady = true;
  attachStartedAt = 0;
  if (statePill.textContent !== 'paused') {
    overlay.hidden = true;
  }
});

menuToggle.addEventListener('click', () => {
  const open = menu.hidden;
  menu.hidden = !open;
  menuToggle.setAttribute('aria-expanded', String(open));
});

resultToggle.addEventListener('click', () => {
  if (resultPanel.hidden) {
    openResultPanel();
  } else {
    closeResultPanel();
  }
});

resultClose.addEventListener('click', () => {
  closeResultPanel();
});

document.addEventListener('click', (event) => {
  const target = event.target;
  if (!(target instanceof Node)) return;
  if (!menu.hidden && !menu.contains(target) && !menuToggle.contains(target)) {
    closeMenu();
  }
  if (!resultPanel.hidden && !resultPanel.contains(target) && !resultToggle.contains(target)) {
    closeResultPanel();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeMenu();
    closeResultPanel();
  }
});

openExternal.addEventListener('click', () => {
  closeMenu();
  window.open(currentSurfaceUrl(), '_blank', 'noopener,noreferrer');
});

openTerminalLink.addEventListener('click', () => {
  closeMenu();
  window.open(currentTerminalUrl, '_blank', 'noopener,noreferrer');
});

openWorkerConsole.addEventListener('click', () => {
  closeMenu();
  window.open(`${runtimeBase}/ui/workers/${workerId}`, '_blank', 'noopener,noreferrer');
});

document.getElementById('steer-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('steer-input');
  const message = input.value.trim();
  if (!message) return;
  try {
    await fetch(`/api/worker/${workerId}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    input.value = '';
    statusLabel.textContent = 'Live status';
    latestOutputInline.textContent = 'Message sent to worker.';
    latestOutputFull.textContent = `Operator message sent:\n\n${message}`;
  } catch (error) {
    latestOutputInline.textContent = error.message;
    latestOutputFull.textContent = error.message;
    openResultPanel();
  }
});

syncMenuLabels();
refresh();
setInterval(refresh, 2000);
