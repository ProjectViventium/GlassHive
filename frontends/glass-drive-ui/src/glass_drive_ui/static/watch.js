const params = new URLSearchParams(window.location.search);
const workerId = window.location.pathname.split('/').filter(Boolean).at(-1);
const projectId = params.get('project_id');
const requestedSurface = params.get('surface') === 'desktop' ? 'desktop' : 'terminal';
const runtimeBase = `${window.location.protocol}//${window.location.hostname}:8766`;
const uiBase = `${window.location.protocol}//${window.location.host}`;
const isApplePlatform = /Mac|iPhone|iPad|iPod/i.test(
  navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || ''
);
const queueShortcutLabel = isApplePlatform ? '⌘+Enter' : 'Ctrl+Enter';
const LONG_PRESS_MS = 550;

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
const openProjectWorkspace = document.getElementById('open-project-workspace');
const openProjectWorkspaceMenu = document.getElementById('open-project-workspace-menu');
const steerForm = document.getElementById('steer-form');
const steerInput = document.getElementById('steer-input');
const sendButton = document.getElementById('send-button');
const guidancePrimary = document.getElementById('steer-guidance-primary');
const guidanceQueue = document.getElementById('steer-guidance-queue');

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
let queueModifierActive = false;
let longPressTimer = 0;
let longPressArmed = false;
let suppressNextClick = false;

function syncDocumentTitle(workerName, projectTitle) {
  const safeProjectTitle = String(projectTitle || 'Workspace').trim() || 'Workspace';
  const safeWorkerName = String(workerName || 'Workspace').trim() || 'Workspace';
  document.title = `GlassHive | ${safeProjectTitle} - ${safeWorkerName}`;
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

function projectWorkspaceUrl() {
  if (!projectId) return '';
  return `${runtimeBase}/ui/projects/${projectId}?worker_id=${workerId}`;
}

function syncProjectWorkspaceLinks() {
  const available = Boolean(projectWorkspaceUrl());
  openProjectWorkspace.hidden = !available;
  openProjectWorkspaceMenu.hidden = !available;
}

function syncSendAffordance() {
  const queueMode = queueModifierActive || longPressArmed;
  sendButton.dataset.mode = queueMode ? 'queue' : 'steer';
  sendButton.textContent = queueMode ? 'Queue' : 'Send';
  guidancePrimary.dataset.active = String(!queueMode);
  guidanceQueue.dataset.active = String(queueMode);
  guidanceQueue.dataset.mode = queueMode ? 'queue' : 'steer';
  if (longPressArmed) {
    guidanceQueue.textContent = 'Release to queue this follow-up without interrupting current work';
  } else if (queueModifierActive) {
    guidanceQueue.textContent = `Click Send or press ${queueShortcutLabel} to queue without interrupting current work`;
  } else {
    guidanceQueue.textContent = `Hold Send or ${queueShortcutLabel} to queue instead`;
  }
  sendButton.title = `Send redirects now. Hold Send or use ${queueShortcutLabel} to queue a follow-up without interrupting current work.`;
}

function clearLongPress() {
  if (longPressTimer) {
    window.clearTimeout(longPressTimer);
    longPressTimer = 0;
  }
  if (!longPressArmed) return;
  longPressArmed = false;
  syncSendAffordance();
}

function setQueueModifierActive(active) {
  if (queueModifierActive === active) return;
  queueModifierActive = active;
  syncSendAffordance();
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
  const latestInstruction = String(data.latest_run?.instruction || '').trim();
  currentRunState = runState;

  if (runState === 'queued') {
    const isSteer = latestInstruction.startsWith('Operator steer instruction');
    const isMessage = latestInstruction.startsWith('Operator message for the current worker session');
    return {
      label: isSteer ? 'Steer handoff' : isMessage ? 'Queued follow-up' : 'Queued next step',
      panelTitle: isSteer ? 'Steer handoff' : isMessage ? 'Queued follow-up' : 'Queued next step',
      summary: isSteer
        ? 'GlassHive is redirecting the workspace to your latest steer instruction.'
        : isMessage
          ? 'Current work keeps running. Your queued follow-up will start next.'
        : 'The workspace has another instruction queued and is preparing the next step.',
      full: isSteer
        ? 'Your steer instruction has been accepted. GlassHive will interrupt the current run when needed and continue from the same workspace state.'
        : isMessage
          ? 'Your follow-up was queued successfully. GlassHive will keep the current run going, then apply this queued instruction from the same workspace state.'
        : 'A follow-up instruction is queued for this workspace and will start next.',
    };
  }

  if (runState === 'running') {
    if (deliverable && deliverable.preferred_surface === 'desktop' && deliverable.browser_url) {
      const label = String(deliverable.label || deliverable.workspace_path || deliverable.browser_url || 'Preview');
      const full = [
        `Preview available while the workspace continues verification · ${label}`,
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
      ? 'Workspace is actively executing. You are watching the live desktop for this run.'
      : 'Workspace is actively executing. You are attached to the exact live session for this run.';
    const full = raw || (activeSurface === 'desktop'
      ? 'Workspace is actively executing. You are watching the live desktop. Switch to Watch exact live session from the menu if you want the raw terminal session.'
      : 'Workspace is actively executing. Open the current view or steer the workspace from the ribbon controls if you need to intervene.');
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
    const idle = data.worker?.state === 'ready' ? 'Workspace is ready for the next instruction.' : 'No run output yet.';
    return {
      label: 'Workspace status',
      panelTitle: 'Workspace status',
      summary: idle,
      full: idle,
    };
  }

  const firstBlock = raw.split(/\n\s*\n|\n/)[0].trim();
  const summary = firstBlock.length <= 180 ? firstBlock : `${firstBlock.slice(0, 177)}...`;
  return {
    label: runState === 'completed' ? 'Latest result' : 'Workspace status',
    panelTitle: runState === 'completed' ? 'Latest result' : 'Workspace output',
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

async function submitFooterInstruction(mode) {
  const message = steerInput.value.trim();
  if (!message) return;
  const path = mode === 'queue' ? 'message' : 'steer';
  try {
    const response = await fetch(`/api/worker/${workerId}/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    steerInput.value = '';
    closeResultPanel();
    if (mode === 'queue') {
      statusLabel.textContent = 'Queued follow-up';
      resultPanelTitle.textContent = 'Queued follow-up';
      latestOutputInline.textContent = 'GlassHive queued your follow-up. Current work keeps running.';
      latestOutputFull.textContent = `Queued follow-up accepted.\n\nGlassHive will keep the current run going, then apply this next instruction:\n\n${message}`;
    } else {
      statusLabel.textContent = 'Steer handoff';
      resultPanelTitle.textContent = 'Steer handoff';
      latestOutputInline.textContent = 'GlassHive is redirecting the workspace now.';
      latestOutputFull.textContent = `Steer instruction accepted.\n\nGlassHive will pivot the workspace to this direction immediately:\n\n${message}`;
    }
    currentSummary = latestOutputInline.textContent;
    currentFullOutput = latestOutputFull.textContent;
    resultToggle.hidden = false;
  } catch (error) {
    latestOutputInline.textContent = error.message;
    latestOutputFull.textContent = error.message;
    openResultPanel();
  }
}

function setOverlay(state, detail) {
  const connecting = attachStartedAt && !frameReady && Date.now() - attachStartedAt < 12000;
  const waiting = state === 'starting' || state === 'paused' || connecting;
  overlay.hidden = !waiting;
  if (!waiting) return;

  if (state === 'paused') {
    overlayTitle.textContent = 'Workspace paused';
    overlayDetail.textContent = activeSurface === 'desktop'
      ? 'The workspace is frozen. Press Play to continue, or open the desktop in a new tab and steer directly.'
      : 'The exact live session is frozen. Press Play to continue the same workspace run, or steer it directly from this session.';
    return;
  }

  if (connecting) {
    overlayTitle.textContent = activeSurface === 'desktop' ? 'Attaching live workspace…' : 'Attaching exact live session…';
    overlayDetail.textContent = activeSurface === 'desktop'
      ? 'The desktop is waking up. If it takes more than a few seconds, open the current desktop in a new tab.'
      : 'We are connecting to the exact running session. If it takes more than a few seconds, open the current session in a new tab.';
    return;
  }

  overlayTitle.textContent = activeSurface === 'desktop' ? 'Preparing live workspace…' : 'Preparing exact live session…';
  overlayDetail.textContent = detail || (activeSurface === 'desktop'
    ? 'The desktop will attach automatically when the workspace is ready.'
    : 'The exact workspace session will appear here as soon as the workspace is ready.');
}

async function refresh() {
  const response = await fetch(`/api/worker/${workerId}/live`);
  if (!response.ok) return;
  const data = await response.json();
  const worker = data.worker;
  const runtime = data.runtime_details || {};
  currentProjectTitle = String(data.project_title || worker.project_id || projectId || 'Project');
  currentDeliverable = data.deliverable || null;

  title.textContent = currentProjectTitle || 'Workspace live view';
  subtitle.textContent = `${worker.profile || 'worker'} workspace · ${worker.state || 'starting'}`;
  syncDocumentTitle(currentProjectTitle, worker.name);
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
      ? 'Use Play to continue the same workspace.'
      : activeSurface === 'desktop'
        ? 'The desktop will attach automatically when the workspace is ready.'
        : 'The exact workspace session will attach automatically when the workspace is ready.'
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
      statusLabel.textContent = 'Workspace status';
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
  setQueueModifierActive(Boolean(event.metaKey || event.ctrlKey));
});

document.addEventListener('keyup', (event) => {
  setQueueModifierActive(Boolean(event.metaKey || event.ctrlKey));
});

window.addEventListener('blur', () => {
  setQueueModifierActive(false);
  clearLongPress();
});

openExternal.addEventListener('click', () => {
  closeMenu();
  window.open(currentSurfaceUrl(), '_blank', 'noopener,noreferrer');
});

for (const button of [openProjectWorkspace, openProjectWorkspaceMenu]) {
  button.addEventListener('click', () => {
    const url = projectWorkspaceUrl();
    closeMenu();
    if (!url) return;
    window.open(url, '_blank', 'noopener,noreferrer');
  });
}

openTerminalLink.addEventListener('click', () => {
  closeMenu();
  window.open(currentTerminalUrl, '_blank', 'noopener,noreferrer');
});

openWorkerConsole.addEventListener('click', () => {
  closeMenu();
  window.open(`${runtimeBase}/ui/workers/${workerId}`, '_blank', 'noopener,noreferrer');
});

steerInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    submitFooterInstruction('queue');
  }
});

sendButton.addEventListener('pointerdown', (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey) return;
  clearLongPress();
  longPressTimer = window.setTimeout(() => {
    longPressTimer = 0;
    longPressArmed = true;
    syncSendAffordance();
  }, LONG_PRESS_MS);
});

for (const eventName of ['pointercancel', 'pointerleave']) {
  sendButton.addEventListener(eventName, () => {
    clearLongPress();
  });
}

sendButton.addEventListener('pointerup', (event) => {
  if (!longPressArmed) {
    clearLongPress();
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  suppressNextClick = true;
  clearLongPress();
  submitFooterInstruction('queue');
});

sendButton.addEventListener('click', (event) => {
  if (suppressNextClick) {
    suppressNextClick = false;
    event.preventDefault();
    return;
  }
  if (event.metaKey || event.ctrlKey) {
    event.preventDefault();
    submitFooterInstruction('queue');
  }
});

steerForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  await submitFooterInstruction('steer');
});

syncMenuLabels();
syncProjectWorkspaceLinks();
syncSendAffordance();
refresh();
setInterval(refresh, 2000);
