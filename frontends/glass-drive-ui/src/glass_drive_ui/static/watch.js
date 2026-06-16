const params = new URLSearchParams(window.location.search);
const workerId = window.location.pathname.split('/').filter(Boolean).at(-1);
const projectId = params.get('project_id');
const signedToken = params.get('gh_token') || '';
const requestedSurface = params.get('surface') === 'desktop' ? 'desktop' : 'terminal';
const uiBase = `${window.location.protocol}//${window.location.host}`;
const runtimeBase = uiBase;
const isApplePlatform = /Mac|iPhone|iPad|iPod/i.test(
  navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || ''
);
const queueShortcutLabel = isApplePlatform ? '⌘+Enter' : 'Ctrl+Enter';
const LONG_PRESS_MS = 550;
const ACTIVE_REFRESH_MS = 2000;
const IDLE_REFRESH_MS = 10000;
const GLASSHIVE_UI_REV = '20260616a';

const frame = document.getElementById('desktop-frame');
const overlay = document.getElementById('stage-overlay');
const stage = document.querySelector('.watch-stage');
const overlayLabel = document.querySelector('#stage-overlay .overlay-label');
const overlayTitle = document.getElementById('overlay-title');
const overlayDetail = document.getElementById('overlay-detail');
const title = document.getElementById('watch-title');
const subtitle = document.getElementById('watch-subtitle');
const latestOutputInline = document.getElementById('latest-output-inline');
const latestOutputFull = document.getElementById('latest-output-full');
const resultActions = document.getElementById('result-actions');
const artifactList = document.getElementById('artifact-list');
const statusLabel = document.getElementById('status-label');
const statePill = document.getElementById('watch-state');
const menu = document.getElementById('more-menu');
const menuToggle = document.getElementById('menu-toggle');
const resultToggle = document.getElementById('result-toggle');
const resultToggleAction = document.getElementById('result-toggle-action');
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
const runToggleButton = document.getElementById('run-toggle');
const guidancePrimary = document.getElementById('steer-guidance-primary');
const guidanceQueue = document.getElementById('steer-guidance-queue');

let activeSurface = requestedSurface;
let currentDesktopUrl = withUiRev(withAuth(`${uiBase}/desktop/${workerId}`));
let currentTerminalUrl = withAuth(`${runtimeBase}/ui/workers/${workerId}/terminal`);
let lastAttachedUrl = '';
let attachStartedAt = 0;
let retryTimers = [];
let frameReady = false;
let currentRunState = '';
let currentSummary = 'No run output yet.';
let currentFullOutput = 'No run output yet.';
let currentProjectTitle = projectId || 'Project';
let currentDeliverable = null;
let currentDesktopAvailable = false;
let lastPromotedDeliverableKey = '';
let lastAttachedFilePreviewKey = '';
let currentFilePreviewKey = '';
let currentFilePreviewUrl = '';
let currentFileDownloadUrl = '';
let deliverablePromotionPending = false;
let queueModifierActive = false;
let longPressTimer = 0;
let longPressArmed = false;
let suppressNextClick = false;
let refreshTimer = 0;
let refreshInFlight = false;

function withAuth(url) {
  if (!signedToken) return url;
  return `${url}${url.includes('?') ? '&' : '?'}gh_token=${encodeURIComponent(signedToken)}`;
}

function withUiRev(url) {
  const value = String(url || '');
  if (!value || /(?:^|[?&])gh_ui_rev=/.test(value)) return value;
  return `${value}${value.includes('?') ? '&' : '?'}gh_ui_rev=${encodeURIComponent(GLASSHIVE_UI_REV)}`;
}

function syncDocumentTitle(workerName, projectTitle) {
  const safeProjectTitle = String(projectTitle || 'Workspace').trim() || 'Workspace';
  const safeWorkerName = String(workerName || 'Workspace').trim() || 'Workspace';
  document.title = `GlassHive | ${safeProjectTitle} - ${safeWorkerName}`;
}

function displayStateForLive(data) {
  const workerState = String(data?.worker?.state || '').trim().toLowerCase();
  const runState = String(data?.latest_run?.state || '').trim().toLowerCase();
  if (runState === 'completed') return 'completed';
  if (['queued', 'running'].includes(runState)) return runState;
  if (['paused', 'idle', 'idle_terminated', 'stopped', 'ready'].includes(workerState)) {
    return workerState === 'ready' ? 'completed' : workerState;
  }
  return workerState || 'starting';
}

function displayStateLabel(state) {
  const normalized = String(state || '').trim().toLowerCase();
  if (normalized === 'completed') return 'Completed';
  if (normalized === 'idle_terminated') return 'Idle stopped';
  return normalized || 'starting';
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
  resultToggle.setAttribute('aria-label', 'Open latest workspace output status');
  if (resultToggleAction) resultToggleAction.textContent = 'Open status';
}

function openResultPanel() {
  if (!currentFullOutput.trim()) return;
  resultPanel.hidden = false;
  resultToggle.setAttribute('aria-expanded', 'true');
  resultToggle.setAttribute('aria-label', 'Close latest workspace output status');
  if (resultToggleAction) resultToggleAction.textContent = 'Close status';
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
  lastAttachedFilePreviewKey = isFilePreviewUrl(url) ? currentFilePreviewKey : '';
  attachStartedAt = Date.now();
  frameReady = false;
  frame.src = url;
  if (!isFilePreviewUrl(url)) {
    scheduleReconnects(url);
  }
}

function filePreviewUrl() {
  return currentRunState === 'completed' && currentDeliverable?.kind === 'file'
    ? String(currentFilePreviewUrl || currentDeliverable.open_url || currentDeliverable.browser_url || '')
    : '';
}

function fileDeliverableKey(deliverable, runId) {
  if (!deliverable || deliverable.kind !== 'file') return '';
  const stablePath = String(deliverable.workspace_path || deliverable.label || '').trim();
  if (!stablePath) return '';
  return `${String(runId || '').trim()}:${stablePath}`;
}

function isFilePreviewUrl(url) {
  const previewUrl = filePreviewUrl();
  return Boolean(previewUrl) && String(url || '') === previewUrl;
}

function currentSurfaceUrl() {
  if (activeSurface === 'desktop') {
    const previewUrl = filePreviewUrl();
    if (previewUrl) return previewUrl;
    return currentDesktopUrl || currentTerminalUrl;
  }
  return currentTerminalUrl || currentDesktopUrl;
}

function clearAttachedView() {
  clearRetryTimers();
  lastAttachedUrl = '';
  lastAttachedFilePreviewKey = '';
  attachStartedAt = 0;
  frameReady = false;
  if (frame.src !== 'about:blank') {
    frame.src = 'about:blank';
  }
}

function projectWorkspaceUrl() {
  if (!projectId) return '';
  return withAuth(`${runtimeBase}/ui/projects/${projectId}?worker_id=${workerId}`);
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

function syncRunToggle(state) {
  if (!runToggleButton) return;
  const normalized = String(state || '').trim().toLowerCase();
  const shouldResume = normalized === 'paused' || normalized === 'idle' || normalized === 'idle_terminated' || normalized === 'stopped' || normalized === 'completed';
  const disabled = ['created', 'starting', 'failed', 'terminated'].includes(normalized);
  runToggleButton.dataset.action = shouldResume ? 'resume' : 'pause';
  runToggleButton.textContent = normalized === 'completed' ? 'Continue' : shouldResume ? 'Resume' : 'Pause';
  runToggleButton.setAttribute('aria-label', normalized === 'completed' ? 'Continue workspace' : shouldResume ? 'Resume workspace' : 'Pause workspace');
  runToggleButton.setAttribute('aria-pressed', String(!shouldResume && !disabled));
  runToggleButton.title = normalized === 'completed'
    ? 'Continue this completed workspace'
    : shouldResume
    ? 'Resume this workspace'
    : 'Pause this workspace';
  runToggleButton.disabled = disabled;
}

function autoResizeSteerInput() {
  if (!steerInput) return;
  steerInput.style.height = 'auto';
  steerInput.style.height = `${Math.min(Math.max(steerInput.scrollHeight, 52), 156)}px`;
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

function refreshDelayForState() {
  if (document.hidden) return IDLE_REFRESH_MS;
  const state = String(currentRunState || '').trim().toLowerCase();
  return ['created', 'starting', 'queued', 'running', 'resuming'].includes(state)
    ? ACTIVE_REFRESH_MS
    : IDLE_REFRESH_MS;
}

function scheduleRefresh(delayMs = refreshDelayForState()) {
  if (refreshTimer) window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => {
    refresh().catch(() => {});
  }, delayMs);
}

function syncMenuLabels() {
  surfaceTerminalButton.dataset.active = String(activeSurface === 'terminal');
  surfaceDesktopButton.dataset.active = String(activeSurface === 'desktop');
  const filePreviewActive = activeSurface === 'desktop' && Boolean(filePreviewUrl());
  openExternal.textContent = filePreviewActive
    ? 'Open delivered file in new tab'
    : activeSurface === 'desktop'
      ? 'Open current desktop in new tab'
      : 'Open current session in new tab';
}

function setSurface(surface, { force = false } = {}) {
  activeSurface = surface === 'desktop' ? 'desktop' : 'terminal';
  syncMenuLabels();
  const state = String(statePill.textContent || 'starting').trim().toLowerCase();
  if (['created', 'starting', 'paused', 'idle', 'idle_terminated', 'stopped', 'failed', 'terminated'].includes(state)) {
    clearAttachedView();
    setOverlay(state);
    return;
  }
  const filePreviewAvailable = activeSurface === 'desktop'
    && currentDeliverable?.kind === 'file'
    && (currentDeliverable.open_url || currentDeliverable.browser_url);
  if (activeSurface === 'desktop' && !currentDesktopAvailable && !filePreviewAvailable) {
    clearAttachedView();
    overlay.hidden = false;
    if (stage) {
      stage.dataset.overlayActive = 'true';
    }
    if (overlayLabel) {
      overlayLabel.textContent = state === 'ready' || state === 'idle' || state === 'completed' ? 'Workspace complete' : 'GlassHive desktop';
    }
    overlayTitle.textContent = state === 'idle' ? 'Worker idle' : state === 'completed' ? 'Worker completed' : state === 'ready' ? 'Workspace ready' : 'Desktop unavailable';
    overlayDetail.textContent = state === 'ready' || state === 'idle' || state === 'completed'
      ? 'The worker is ready for a follow-up. Compute may be stopped to save resources; GlassHive resumes it automatically when you send work.'
      : 'This workspace does not currently expose a live desktop surface.';
    return;
  }
  const url = currentSurfaceUrl();
  const filePreviewKey = activeSurface === 'desktop' ? currentFilePreviewKey : '';
  const sameFilePreviewAttached = Boolean(
    filePreviewKey
      && lastAttachedFilePreviewKey === filePreviewKey
      && lastAttachedUrl
      && !force
  );
  const stalledFilePreviewAttach = sameFilePreviewAttached
    && !frameReady
    && attachStartedAt
    && Date.now() - attachStartedAt > 12000;
  if (sameFilePreviewAttached && !stalledFilePreviewAttach && !force) {
    // Keep the completed file preview stable while signed URLs rotate in live payloads.
  } else if (force || lastAttachedUrl !== url || frame.src !== url) {
    attachView(url);
  }
  setOverlay(state || 'starting');
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
    const deliverableLabel = deliverable.kind === 'file' ? 'Delivered file ready' : 'Delivered page ready';
    const full = [
      `${deliverableLabel} · ${label}`,
      deliverable.browser_url ? `Browser target: ${deliverable.browser_url}` : '',
      raw,
    ].filter(Boolean).join('\n\n');
    return {
      label: 'Latest result',
      panelTitle: 'Delivered result',
      summary: `${deliverableLabel} · ${label}`,
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

function syncResultActions(deliverable) {
  if (!resultActions) return;
  resultActions.replaceChildren();
  const actions = [];
  if (deliverable?.kind === 'file') {
    const openUrl = String(deliverable.open_url || deliverable.browser_url || '');
    const downloadUrl = String(deliverable.download_url || '');
    if (openUrl) actions.push({ label: 'Open file', url: openUrl, primary: true });
    if (downloadUrl) actions.push({ label: 'Download file', url: downloadUrl, primary: false });
  }
  resultActions.hidden = actions.length === 0;
  for (const action of actions) {
    const link = document.createElement('a');
    link.className = `result-action${action.primary ? ' primary' : ''}`;
    link.href = action.url;
    link.textContent = action.label;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    resultActions.appendChild(link);
  }
}

function formatArtifactSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '';
  if (value < 1024) return `${value} bytes`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function syncArtifactList(items) {
  if (!artifactList) return;
  artifactList.replaceChildren();
  const files = Array.isArray(items)
    ? items.filter((item) => item && !item.is_dir && (item.open_url || item.download_url || item.path))
    : [];
  artifactList.hidden = files.length === 0;
  if (!files.length) return;

  const heading = document.createElement('div');
  heading.className = 'artifact-list-heading';
  heading.textContent = files.length === 1 ? 'Workspace file' : `Workspace files (${files.length})`;
  artifactList.appendChild(heading);

  const visibleFiles = files.slice(0, 20);
  for (const file of visibleFiles) {
    const row = document.createElement('div');
    row.className = 'artifact-row';

    const label = document.createElement('span');
    label.className = 'artifact-label';
    label.textContent = String(file.path || file.name || 'artifact');
    row.appendChild(label);

    const meta = document.createElement('span');
    meta.className = 'artifact-meta';
    const size = formatArtifactSize(file.size);
    meta.textContent = size || 'file';
    row.appendChild(meta);

    if (file.open_url) {
      const open = document.createElement('a');
      open.className = 'artifact-link';
      open.href = String(file.open_url);
      open.target = '_blank';
      open.rel = 'noopener noreferrer';
      open.textContent = 'Open';
      row.appendChild(open);
    }
    if (file.download_url) {
      const download = document.createElement('a');
      download.className = 'artifact-link';
      download.href = String(file.download_url);
      download.target = '_blank';
      download.rel = 'noopener noreferrer';
      download.textContent = 'Download';
      row.appendChild(download);
    }

    artifactList.appendChild(row);
  }
  if (files.length > visibleFiles.length) {
    const more = document.createElement('div');
    more.className = 'artifact-list-more';
    more.textContent = `${files.length - visibleFiles.length} more files`;
    artifactList.appendChild(more);
  }
}

async function maybePromoteDeliverable(data) {
  const deliverable = data.deliverable || null;
  const runState = String(data.latest_run?.state || '').trim();
  const runId = String(data.latest_run?.run_id || '').trim();
  if (deliverable?.kind === 'file' && (deliverable.open_url || deliverable.browser_url) && runState === 'completed') {
    const fileUrl = String(deliverable.open_url || deliverable.browser_url || '').trim();
    const promotionKey = fileDeliverableKey(deliverable, runId) || `${runId}:${fileUrl}`;
    currentFilePreviewKey = promotionKey;
    currentFilePreviewUrl = fileUrl;
    currentFileDownloadUrl = String(deliverable.download_url || '');
    currentDeliverable = {
      ...deliverable,
      open_url: currentFilePreviewUrl,
      download_url: currentFileDownloadUrl,
    };
    syncResultActions(currentDeliverable);
    if (promotionKey && promotionKey !== lastPromotedDeliverableKey) {
      lastPromotedDeliverableKey = promotionKey;
      activeSurface = 'desktop';
      setSurface('desktop', { force: true });
    }
    return;
  }
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
  syncResultActions(data.deliverable || null);
  syncArtifactList(data.artifacts?.items || []);
  currentSummary = output.summary;
  currentFullOutput = output.full;
  resultToggle.hidden = !output.full.trim();
  if (!output.full.trim()) {
    closeResultPanel();
  }
}

async function postAction(action, payload) {
  const response = await fetch(withAuth(`/api/worker/${workerId}/${action.startsWith('action:') ? 'action/' + action.split(':', 2)[1] : action}`), {
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
    const response = await fetch(withAuth(`/api/worker/${workerId}/${path}`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    steerInput.value = '';
    autoResizeSteerInput();
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
  const filePreviewActive = activeSurface === 'desktop' && Boolean(filePreviewUrl());
  const waiting = state === 'starting' || state === 'paused' || state === 'idle' || state === 'idle_terminated' || state === 'stopped' || connecting;
  overlay.hidden = !waiting;
  if (stage) {
    stage.dataset.overlayActive = String(waiting);
  }
  if (!waiting) return;

  if (state === 'paused') {
    if (overlayLabel) {
      overlayLabel.textContent = 'Workspace resuming';
    }
    overlayTitle.textContent = 'Workspace resuming…';
    overlayDetail.textContent = activeSurface === 'desktop'
      ? 'The worker is already moving. The desktop will attach automatically when the workspace is ready.'
      : 'The worker is already moving. The exact live session will attach automatically when the workspace is ready.';
    return;
  }

  if (state === 'idle' || state === 'idle_terminated' || state === 'stopped') {
    if (overlayLabel) {
      overlayLabel.textContent = 'Workspace idle';
    }
    overlayTitle.textContent = 'Worker idle';
    overlayDetail.textContent = 'The last run is complete and compute is stopped to save resources. Send follow-up work and GlassHive will resume this workspace automatically.';
    return;
  }

  if (connecting) {
    if (overlayLabel) {
      overlayLabel.textContent = filePreviewActive ? 'Delivered file' : 'Workspace attaching';
    }
    overlayTitle.textContent = filePreviewActive
      ? 'Opening delivered file…'
      : activeSurface === 'desktop'
        ? 'Attaching live workspace…'
        : 'Attaching exact live session…';
    overlayDetail.textContent = filePreviewActive
      ? 'The completed file preview is loading. Use Open delivered file in new tab if this takes more than a few seconds.'
      : activeSurface === 'desktop'
        ? 'The desktop is waking up. If it takes more than a few seconds, open the current desktop in a new tab.'
      : 'We are connecting to the exact running session. If it takes more than a few seconds, open the current session in a new tab.';
    return;
  }

  if (overlayLabel) {
    overlayLabel.textContent = 'Workspace warming up';
  }
  overlayTitle.textContent = filePreviewActive
    ? 'Opening delivered file…'
    : activeSurface === 'desktop'
      ? 'Preparing live workspace…'
      : 'Preparing exact live session…';
  overlayDetail.textContent = detail || (filePreviewActive
    ? 'The completed file preview will appear here automatically.'
    : activeSurface === 'desktop'
      ? 'The desktop will attach automatically when the workspace is ready.'
    : 'The exact workspace session will appear here as soon as the workspace is ready.');
}

async function refresh() {
  if (refreshInFlight) {
    scheduleRefresh(ACTIVE_REFRESH_MS);
    return;
  }
  refreshInFlight = true;
  try {
    const response = await fetch(withAuth(`/api/worker/${workerId}/live`));
    if (!response.ok) return;
    const data = await response.json();
    const worker = data.worker;
    const runtime = data.runtime_details || {};
    const displayState = displayStateForLive(data);
    currentProjectTitle = String(data.project_title || worker.project_id || projectId || 'Project');
    currentDeliverable = data.deliverable || null;

    title.textContent = currentProjectTitle || 'Workspace live view';
    subtitle.textContent = `${worker.profile || 'worker'} workspace · ${displayStateLabel(displayState)}`;
    syncDocumentTitle(currentProjectTitle, worker.name);
    statePill.textContent = displayStateLabel(displayState);
    syncRunToggle(displayState);

    currentDesktopAvailable = Boolean(runtime.view_available || runtime.view_url);
  currentDesktopUrl = currentDesktopAvailable ? withUiRev(withAuth(`${uiBase}/desktop/${workerId}`)) : '';
    currentTerminalUrl = withAuth(`${runtimeBase}/ui/workers/${workerId}/terminal`);

    renderOutput(data);
    await maybePromoteDeliverable(data);
    syncMenuLabels();
    setSurface(activeSurface, { force: false });
    if (!(activeSurface === 'desktop' && !currentDesktopAvailable)) {
      setOverlay(
        displayState,
        displayState === 'paused'
          ? 'Use Resume to continue the same workspace.'
          : activeSurface === 'desktop'
            ? 'The desktop will attach automatically when the workspace is ready.'
            : 'The exact workspace session will attach automatically when the workspace is ready.'
      );
    }
  } finally {
    refreshInFlight = false;
    scheduleRefresh();
  }
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
  if (!['paused', 'idle', 'idle stopped'].includes(String(statePill.textContent || '').trim().toLowerCase())) {
    overlay.hidden = true;
    if (stage) {
      stage.dataset.overlayActive = 'false';
    }
  }
});

menuToggle.addEventListener('click', () => {
  const open = menu.hidden;
  menu.hidden = !open;
  menuToggle.setAttribute('aria-expanded', String(open));
  if (open) {
    const firstMenuItem = menu.querySelector('button:not([hidden])');
    if (firstMenuItem instanceof HTMLElement) {
      firstMenuItem.focus();
    }
  }
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
  window.open(withAuth(`${runtimeBase}/ui/workers/${workerId}`), '_blank', 'noopener,noreferrer');
});

steerInput.addEventListener('input', autoResizeSteerInput);

steerInput.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey) return;
  if (event.metaKey || event.ctrlKey) {
    event.preventDefault();
    submitFooterInstruction('queue');
    return;
  }
  event.preventDefault();
  submitFooterInstruction('steer');
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

document.addEventListener('visibilitychange', () => {
  scheduleRefresh(0);
});

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
syncRunToggle('starting');
autoResizeSteerInput();
refresh().catch(() => {});
