const ACTIVE_STATES = new Set(['created', 'starting', 'queued', 'running', 'resuming']);
const ACTIVE_RUN_STATES = new Set(['queued', 'running']);
const INTERRUPTIBLE_STATES = new Set(['queued', 'running', 'resuming']);
const RESUME_STATES = new Set(['ready', 'paused', 'idle', 'idle_terminated', 'stopped', 'completed', 'retained']);
const DISABLED_CONTROL_STATES = new Set(['created', 'starting', 'failed', 'terminated']);
const MAX_LIVE_TILE_IFRAMES = 4;
const ACTIVE_TILE_REFRESH_MS = 7000;
const RETAINED_TILE_REFRESH_MS = 60000;
const GLASSHIVE_UI_REV = '20260616a';
let workspaceRefreshInFlight = false;
const pageParams = new URLSearchParams(window.location.search);
const signedToken = pageParams.get('gh_token') || '';

const defaultHivePrefs = {
  showInactive: false,
  showWatch: true,
  showStatus: true,
};

async function loadBootstrap() {
  const response = await fetch(withAuth('/api/bootstrap'));
  if (!response.ok) throw new Error(await responseMessage(response, 'Failed to load workspace options'));
  return response.json();
}

function withAuth(url) {
  const value = String(url || '');
  if (!signedToken || /(?:^|[?&])gh_token=/.test(value)) return value;
  const hashIndex = value.indexOf('#');
  const base = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
  const hash = hashIndex >= 0 ? value.slice(hashIndex) : '';
  return `${base}${base.includes('?') ? '&' : '?'}gh_token=${encodeURIComponent(signedToken)}${hash}`;
}

function withUiRev(url) {
  const value = String(url || '');
  if (!value || /(?:^|[?&])gh_ui_rev=/.test(value)) return value;
  const hashIndex = value.indexOf('#');
  const base = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
  const hash = hashIndex >= 0 ? value.slice(hashIndex) : '';
  return `${base}${base.includes('?') ? '&' : '?'}gh_ui_rev=${encodeURIComponent(GLASSHIVE_UI_REV)}${hash}`;
}

async function responseMessage(response, fallback) {
  const contentType = response.headers.get('content-type') || '';
  try {
    if (contentType.includes('application/json')) {
      const payload = await response.json();
      return String(payload.detail || payload.message || fallback);
    }
    const text = await response.text();
    return text.trim() || fallback;
  } catch {
    return fallback;
  }
}

async function postJson(url, payload) {
  const response = await fetch(withAuth(url), {
    method: 'POST',
    headers: payload ? { 'Content-Type': 'application/json' } : {},
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) throw new Error(await responseMessage(response, 'Request failed'));
  return response.json();
}

async function patchJson(url, payload) {
  const response = await fetch(withAuth(url), {
    method: 'PATCH',
    headers: payload ? { 'Content-Type': 'application/json' } : {},
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) throw new Error(await responseMessage(response, 'Request failed'));
  return response.json();
}

function fileToPayload(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener('load', () => {
      const value = String(reader.result || '');
      resolve({
        name: file.name,
        mime_type: file.type || '',
        size: file.size,
        content_base64: value.includes(',') ? value.split(',', 2)[1] : value,
      });
    });
    reader.addEventListener('error', () => reject(reader.error || new Error(`Could not read ${file.name}`)));
    reader.readAsDataURL(file);
  });
}

async function selectedFilePayloads(input) {
  const files = Array.from(input?.files || []);
  return Promise.all(files.map((file) => fileToPayload(file)));
}

function renderWorkspaceTypeOptions(select, help, data) {
  const options = [];
  const items = data.workspace_type_options?.length
    ? data.workspace_type_options
    : [
        {
          value: 'sandboxed',
          label: 'Sandboxed Workspace',
          description: 'Runs on managed GlassHive workspace compute with project files and browser state preserved for resume.',
        },
      ];
  for (const item of items) {
    const option = document.createElement('option');
    option.value = String(item.value || '');
    option.textContent = String(item.label || item.value || '');
    option.disabled = Boolean(item.disabled);
    option.dataset.description = String(item.description || '');
    options.push(option);
  }
  select.replaceChildren(...options);
  select.value = String(data.default_workspace_type || 'sandboxed');
  if (help) {
    const selected = select.selectedOptions?.[0];
    help.textContent = selected?.dataset.description || 'Runs on managed GlassHive workspace compute.';
  }
}

function renderDefaultWorkerOptions(select, data) {
  if (!select) return;
  const current = String(data?.user_preferences?.default_worker_profile || '');
  const options = [];
  const deploymentDefault = document.createElement('option');
  deploymentDefault.value = '';
  deploymentDefault.textContent = 'Deployment default';
  options.push(deploymentDefault);
  for (const item of data.new_workspace_options || []) {
    const option = document.createElement('option');
    const profile = String(item.profile || String(item.value || '').split(':', 2)[1] || '');
    option.value = profile;
    option.textContent = String(item.label || profile || 'Worker');
    options.push(option);
  }
  select.replaceChildren(...options);
  select.value = Array.from(select.options).some((option) => option.value === current) ? current : '';
}

function syncPreferenceControls(data, controls) {
  const prefs = data?.user_preferences || {};
  renderDefaultWorkerOptions(controls.defaultWorker, data);
  if (controls.codexEffort) controls.codexEffort.value = String(prefs.codex_reasoning_effort || '');
  if (controls.claudeEffort) controls.claudeEffort.value = String(prefs.claude_effort || '');
  if (controls.openclawEffort) controls.openclawEffort.value = String(prefs.openclaw_effort || '');
}

function renderWorkspaceOptions(select, data, selectedValue = '') {
  const existing = uniqueWorkspaces(data.existing_workspaces || []);
  const groups = [];

  if (existing.length) {
    const openGroup = document.createElement('optgroup');
    openGroup.label = 'Saved workspaces';
    for (const workspace of existing) {
      const option = document.createElement('option');
      option.value = `open:${String(workspace.worker_id || '')}`;
      option.textContent = workspaceOptionLabel(workspace);
      openGroup.appendChild(option);
    }
    groups.push(openGroup);

    if ((selectedValue || '').startsWith('duplicate:')) {
      const workerId = selectedValue.split(':', 2)[1];
      const workspace = findWorkspace(existing, workerId);
      const duplicateGroup = document.createElement('optgroup');
      duplicateGroup.label = 'Duplicate selected workspace';
      const option = document.createElement('option');
      option.value = selectedValue;
      option.textContent = `Duplicate ${workspaceOptionLabel(workspace)}`;
      duplicateGroup.appendChild(option);
      groups.push(duplicateGroup);
    }
  }

  const newGroup = document.createElement('optgroup');
  newGroup.label = 'New workers';
  for (const item of data.new_workspace_options || []) {
    const option = document.createElement('option');
    option.value = String(item.value || '');
    option.textContent = String(item.label || item.value || '');
    newGroup.appendChild(option);
  }
  groups.push(newGroup);

  select.replaceChildren(...groups);
  const optionValues = Array.from(select.querySelectorAll('option')).map((option) => option.value);
  select.value = optionValues.includes(selectedValue)
    ? selectedValue
    : String(data.default_workspace_option || 'new:codex-cli');
}

function uniqueWorkspaces(workspaces) {
  const seen = new Set();
  const items = [];
  for (const workspace of workspaces || []) {
    const workerId = String(workspace?.worker_id || '');
    if (!workerId || seen.has(workerId)) continue;
    seen.add(workerId);
    items.push(workspace);
  }
  return items;
}

function rawWorkspaceState(workspace) {
  return String(workspace?.state || 'unknown').trim().toLowerCase() || 'unknown';
}

function workspaceStateLabel(workspace) {
  return String(workspace?.state_label || rawWorkspaceState(workspace)).trim() || 'unknown';
}

function isWorkspaceActive(workspace) {
  const state = rawWorkspaceState(workspace);
  return Boolean(workspace?.is_active) || ACTIVE_STATES.has(state);
}

function isWorkspaceResumable(workspace) {
  const state = rawWorkspaceState(workspace);
  return Boolean(workspace?.is_resumable) || RESUME_STATES.has(state);
}

function workspaceProfileLabel(profile) {
  return {
    'codex-cli': 'Codex',
    'claude-code': 'Claude Code',
    'openclaw-general': 'OpenClaw',
  }[profile] || profile || 'Worker';
}

function displayStateLabel(state) {
  const normalized = String(state || '').trim().toLowerCase();
  if (normalized === 'completed') return 'Completed';
  if (normalized === 'idle_terminated') return 'Idle stopped';
  if (normalized === 'ready') return 'Ready';
  if (normalized === 'retained') return 'Retained';
  return normalized || 'unknown';
}

function workspaceTileTitle(workspace) {
  const label = String(workspace?.workspace_label || '').trim();
  const name = String(workspace?.name || '').trim();
  if (label && name && label !== name) return `${label} · ${name}`;
  return label || name || 'Workspace';
}

function workspaceOptionLabel(workspace) {
  if (!workspace) return 'selected workspace';
  return `${workspaceTileTitle(workspace)} · ${workspaceProfileLabel(workspace.profile)} · ${workspaceStateLabel(workspace)}`;
}

function workerActionForState(state) {
  return RESUME_STATES.has(String(state || '').trim().toLowerCase()) ? 'resume' : 'pause';
}

function workerDesktopUrl(workerId, signedUrl = '') {
  return withUiRev(signedUrl || withAuth(`/desktop/${encodeURIComponent(String(workerId || ''))}`));
}

function appendUrlPath(url, path) {
  const value = String(url || '');
  if (!value) return '';
  const hashIndex = value.indexOf('#');
  const withoutHash = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
  const hash = hashIndex >= 0 ? value.slice(hashIndex) : '';
  const queryIndex = withoutHash.indexOf('?');
  if (queryIndex < 0) return `${withoutHash}${path}${hash}`;
  return `${withoutHash.slice(0, queryIndex)}${path}${withoutHash.slice(queryIndex)}${hash}`;
}

function workerApiUrl(workerId, path = '') {
  const tile = Array.from(document.querySelectorAll('.workspace-tile')).find((item) => item.dataset.workerId === String(workerId || ''));
  const signedBase = String(tile?.dataset.apiUrl || '');
  if (signedBase) return appendUrlPath(signedBase, path);
  return `/api/worker/${encodeURIComponent(String(workerId || ''))}${path}`;
}

function summarizeLive(data) {
  const runState = String(data?.latest_run?.state || '').trim();
  const output = String(data?.latest_output || '').trim();
  const deliverable = data?.deliverable || null;
  if (runState === 'queued') return 'Queued follow-up is waiting for this workspace.';
  if (runState === 'running') return 'Workspace is running now.';
  if (deliverable && runState === 'completed') {
    const label = deliverable.kind === 'file' ? 'Delivered file ready' : 'Delivered page ready';
    return `Completed · ${label} · ${String(deliverable.label || deliverable.workspace_path || deliverable.browser_url || 'deliverable')}`;
  }
  if (runState === 'completed') return output ? `Completed · ${output.split(/\n\s*\n|\n/)[0].trim()}` : 'Completed.';
  if (output) {
    const firstLine = output.split(/\n\s*\n|\n/)[0].trim();
    return firstLine.length <= 150 ? firstLine : `${firstLine.slice(0, 147)}...`;
  }
  const state = String(data?.worker?.state || '').trim();
  return state === 'ready' ? 'Workspace is ready for the next instruction.' : 'No run output yet.';
}

function updateTileControlLabels(tile, state) {
  const normalized = String(state || '').trim().toLowerCase();
  const action = workerActionForState(state);
  const toggle = tile.querySelector('[data-worker-action-toggle]');
  if (toggle) {
    toggle.dataset.action = action;
    toggle.textContent = normalized === 'completed' ? 'Continue' : action === 'resume' ? 'Resume' : 'Pause';
    toggle.disabled = DISABLED_CONTROL_STATES.has(normalized);
  }
  const interrupt = tile.querySelector('[data-worker-interrupt]');
  if (interrupt) {
    interrupt.hidden = !INTERRUPTIBLE_STATES.has(normalized);
    interrupt.disabled = DISABLED_CONTROL_STATES.has(normalized);
  }
  const stateLabel = tile.querySelector('[data-worker-state]');
  if (stateLabel) stateLabel.textContent = displayStateLabel(state);
}

function setGlassPane(glass, workerId, state, hasLiveDesktop, refreshBootstrap) {
  const pane = glass.querySelector('[data-worker-glass]');
  if (!pane) return;
  const tile = glass.closest('.workspace-tile');
  const watchVisible = tile?.dataset.watchVisible !== 'false';
  if (!watchVisible) {
    pane.replaceChildren();
    return;
  }
  const normalized = String(state || '').trim().toLowerCase();
  const alreadyHasFrame = Boolean(pane.querySelector('.workspace-live-frame'));
  const canMountLiveFrame = alreadyHasFrame || document.querySelectorAll('.workspace-live-frame').length < MAX_LIVE_TILE_IFRAMES;
  if ((ACTIVE_STATES.has(normalized) || normalized === 'running' || normalized === 'queued') && hasLiveDesktop && canMountLiveFrame) {
    let frame = pane.querySelector('.workspace-live-frame');
    if (!frame) {
      frame = document.createElement('iframe');
      frame.className = 'workspace-live-frame';
      frame.loading = 'lazy';
      frame.title = 'Live workspace desktop';
      frame.src = workerDesktopUrl(workerId, tile?.dataset.desktopUrl || '');
      pane.replaceChildren(frame);
    }
    return;
  }
  if (ACTIVE_STATES.has(normalized) || normalized === 'running' || normalized === 'queued') {
    const note = document.createElement('div');
    note.className = 'workspace-glass-note';
    note.textContent = hasLiveDesktop
      ? 'Live available'
      : normalized === 'ready'
      ? 'Workspace ready'
      : 'Live surface warming up';
    pane.replaceChildren(note);
    return;
  }

  const wakeButton = createButton(normalized === 'completed' ? 'Completed' : 'Resume workspace', 'workspace-glass-action');
  if (normalized === 'completed') {
    wakeButton.dataset.intent = 'completed';
    wakeButton.title = 'The last run completed. Click to continue this workspace with follow-up work.';
  }
  wakeButton.addEventListener('click', async () => {
    await runWorkerAction(workerId, 'resume', wakeButton, refreshBootstrap);
  });
  pane.replaceChildren(wakeButton);
}

function displayStateForLive(data) {
  const workerState = String(data?.worker?.state || '').trim().toLowerCase();
  const runState = String(data?.latest_run?.state || '').trim().toLowerCase();
  if (runState === 'completed') return 'completed';
  if (ACTIVE_RUN_STATES.has(runState)) return runState;
  if (['paused', 'idle', 'idle_terminated', 'stopped', 'ready'].includes(workerState) && !ACTIVE_RUN_STATES.has(runState)) {
    return workerState === 'ready' ? 'completed' : workerState;
  }
  return workerState || 'unknown';
}

async function refreshWorkspaceTile(workerId, refreshBootstrap) {
  const tile = Array.from(document.querySelectorAll('.workspace-tile')).find((item) => item.dataset.workerId === workerId);
  if (!tile) return;
  const output = tile.querySelector('[data-worker-output]');
  const markNextRefresh = (delayMs) => {
    tile.dataset.liveLoaded = 'true';
    tile.dataset.nextLiveRefreshAt = String(Date.now() + delayMs);
  };
  try {
    const response = await fetch(withAuth(workerApiUrl(workerId, '/live')));
    if (!response.ok) throw new Error(await responseMessage(response, 'Live status unavailable'));
    const data = await response.json();
    const rawState = String(data?.worker?.state || '').trim().toLowerCase() || 'unknown';
    const runState = String(data?.latest_run?.state || '').trim().toLowerCase();
    const state = displayStateForLive(data);
    tile.dataset.state = ACTIVE_RUN_STATES.has(runState) || ACTIVE_STATES.has(rawState)
      ? 'active'
      : RESUME_STATES.has(rawState) || state === 'completed'
        ? 'resumable'
        : 'inactive';
    markNextRefresh(tile.dataset.state === 'active' ? ACTIVE_TILE_REFRESH_MS : RETAINED_TILE_REFRESH_MS);
    updateTileControlLabels(tile, state);
    const glass = tile.querySelector('.workspace-tile-glass');
    if (glass) setGlassPane(glass, workerId, state, Boolean(data?.runtime_details?.view_available || data?.runtime_details?.view_url), refreshBootstrap);
    const meta = tile.querySelector('[data-worker-meta]');
    if (meta) meta.textContent = `${workspaceProfileLabel(data?.worker?.profile)} · ${displayStateLabel(state)}`;
    const favorite = tile.querySelector('[data-worker-favorite]');
    if (favorite && Object.prototype.hasOwnProperty.call(data?.worker || {}, 'favorite')) {
      const isFavorite = Boolean(data.worker.favorite);
      favorite.dataset.favorite = String(isFavorite);
      favorite.textContent = isFavorite ? '★' : '☆';
      favorite.title = isFavorite ? 'Remove favorite' : 'Mark favorite';
      favorite.setAttribute('aria-label', favorite.title);
    }
    if (output) output.textContent = summarizeLive(data);
  } catch (error) {
    markNextRefresh(ACTIVE_TILE_REFRESH_MS);
    if (output) output.textContent = error.message;
  }
}

async function refreshVisibleWorkspaceTiles(refreshBootstrap, { force = false } = {}) {
  if (document.hidden || workspaceRefreshInFlight) return;
  const now = Date.now();
  const workerIds = Array.from(document.querySelectorAll('.workspace-tile'))
    .filter((tile) => tile.dataset.watchVisible === 'true' || tile.dataset.statusVisible === 'true')
    .filter((tile) => {
      if (force) return true;
      if (tile.dataset.liveLoaded !== 'true') return true;
      const nextRefreshAt = Number(tile.dataset.nextLiveRefreshAt || 0);
      return !nextRefreshAt || now >= nextRefreshAt;
    })
    .map((tile) => tile.dataset.workerId)
    .filter(Boolean);
  if (!workerIds.length) return;
  workspaceRefreshInFlight = true;
  try {
    await Promise.all(workerIds.map((workerId) => refreshWorkspaceTile(workerId, refreshBootstrap)));
  } finally {
    workspaceRefreshInFlight = false;
  }
}

function createButton(label, className = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  if (className) button.className = className;
  return button;
}

function autoResizeTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = 'auto';
  textarea.style.height = `${Math.min(Math.max(textarea.scrollHeight, 44), 168)}px`;
}

function renderWorkspaceTile(workspace, refreshBootstrap, draftMessage = '', viewPrefs = defaultHivePrefs) {
  const workerId = String(workspace.worker_id || '');
  const state = workspaceStateLabel(workspace).toLowerCase();
  const isActive = isWorkspaceActive(workspace);
  const isResumable = isWorkspaceResumable(workspace);

  const tile = document.createElement('article');
  tile.className = 'workspace-tile';
  tile.dataset.state = isActive ? 'active' : isResumable ? 'resumable' : 'inactive';
  tile.dataset.workerId = workerId;
  tile.dataset.desktopUrl = String(workspace.desktop_url || '');
  tile.dataset.apiUrl = String(workspace.api_url || '');
  tile.dataset.watchVisible = String(Boolean(viewPrefs.showWatch));
  tile.dataset.statusVisible = String(Boolean(viewPrefs.showStatus));
  tile.dataset.liveLoaded = 'false';
  tile.dataset.nextLiveRefreshAt = '0';

  const glass = document.createElement('div');
  glass.className = 'workspace-tile-glass';

  const status = document.createElement('span');
  status.className = 'workspace-status-dot';
  status.dataset.workerState = 'true';
  status.textContent = displayStateLabel(state);
  glass.appendChild(status);

  const glassPane = document.createElement('div');
  glassPane.className = 'workspace-glass-content';
  glassPane.dataset.workerGlass = 'true';
  if (isActive) {
    const note = document.createElement('div');
    note.className = 'workspace-glass-note';
    note.textContent = 'Checking live surface...';
    glassPane.appendChild(note);
  } else {
    const wakeButton = createButton(state === 'completed' || state === 'ready' || state === 'retained' ? 'Completed' : 'Resume workspace', 'workspace-glass-action');
    if (wakeButton.textContent === 'Completed') {
      wakeButton.dataset.intent = 'completed';
      wakeButton.title = 'The last run completed. Use Send below to continue this workspace.';
    }
    wakeButton.addEventListener('click', async () => {
      await runWorkerAction(workerId, 'resume', wakeButton, refreshBootstrap);
    });
    glassPane.appendChild(wakeButton);
  }
  glass.appendChild(glassPane);

  const body = document.createElement('div');
  body.className = 'workspace-tile-body';

  const title = document.createElement('h3');
  title.textContent = workspaceTileTitle(workspace);
  body.appendChild(title);

  const meta = document.createElement('p');
  meta.dataset.workerMeta = 'true';
  meta.textContent = `${workspaceProfileLabel(workspace.profile)} · ${displayStateLabel(state)}`;
  body.appendChild(meta);

  const report = document.createElement('button');
  report.type = 'button';
  report.className = 'workspace-status-report workspace-status-button';
  report.setAttribute('aria-label', `Open latest workspace output for ${workspaceTileTitle(workspace)}`);
  report.addEventListener('click', () => {
    window.location.href = String(workspace.watch_url || '#');
  });
  const reportHead = document.createElement('span');
  reportHead.className = 'workspace-report-head';
  const reportLabel = document.createElement('span');
  reportLabel.className = 'workspace-report-label';
  reportLabel.textContent = 'Latest workspace output';
  const reportAction = document.createElement('span');
  reportAction.className = 'workspace-report-action';
  reportAction.textContent = 'Open status';
  reportHead.append(reportLabel, reportAction);
  const liveOutput = document.createElement('span');
  liveOutput.className = 'workspace-live-output';
  liveOutput.dataset.workerOutput = 'true';
  liveOutput.textContent = 'Loading workspace status...';
  report.append(reportHead, liveOutput);
  body.appendChild(report);

  const actions = document.createElement('div');
  actions.className = 'workspace-tile-actions';

  const favorite = createButton(workspace.favorite ? '★' : '☆', 'workspace-icon-button');
  favorite.dataset.workerFavorite = 'true';
  favorite.dataset.favorite = String(Boolean(workspace.favorite));
  favorite.title = workspace.favorite ? 'Remove favorite' : 'Mark favorite';
  favorite.setAttribute('aria-label', favorite.title);
  favorite.addEventListener('click', async () => {
    const next = favorite.dataset.favorite !== 'true';
    favorite.textContent = next ? '★' : '☆';
    favorite.dataset.favorite = String(next);
    await runWorkerMetadata(workerId, { favorite: next }, favorite, refreshBootstrap);
  });
  actions.appendChild(favorite);

  const watch = createButton('Full watch');
  watch.addEventListener('click', () => {
    window.location.href = String(workspace.watch_url || '#');
  });
  actions.appendChild(watch);

  const project = createButton('Project');
  project.addEventListener('click', () => {
    window.location.href = String(workspace.project_url || '#');
  });
  actions.appendChild(project);

  const duplicate = createButton('Duplicate');
  duplicate.addEventListener('click', () => {
    window.dispatchEvent(new CustomEvent('glasshive:duplicate-workspace', { detail: { workerId } }));
  });
  actions.appendChild(duplicate);

  const toggle = createButton(state === 'completed' ? 'Continue' : workerActionForState(state) === 'resume' ? 'Resume' : 'Pause', 'workspace-run-toggle');
  toggle.dataset.workerActionToggle = 'true';
  toggle.dataset.action = workerActionForState(state);
  toggle.disabled = DISABLED_CONTROL_STATES.has(state);
  toggle.addEventListener('click', async () => {
    await runWorkerAction(workerId, toggle.dataset.action, toggle, refreshBootstrap);
  });
  actions.appendChild(toggle);

  const interrupt = createButton('Interrupt', 'workspace-secondary');
  interrupt.dataset.workerInterrupt = 'true';
  interrupt.hidden = !INTERRUPTIBLE_STATES.has(state);
  interrupt.disabled = DISABLED_CONTROL_STATES.has(state);
  interrupt.addEventListener('click', async () => {
    await runWorkerAction(workerId, 'interrupt', interrupt, refreshBootstrap);
  });
  actions.appendChild(interrupt);

  const steerForm = document.createElement('form');
  steerForm.className = 'workspace-steer';
  const steerInput = document.createElement('textarea');
  steerInput.name = 'message';
  steerInput.rows = 1;
  steerInput.value = draftMessage;
  steerInput.placeholder = 'Steer this workspace';
  steerInput.setAttribute('aria-label', `Steer ${workspaceTileTitle(workspace)}`);
  steerInput.addEventListener('input', () => autoResizeTextarea(steerInput));
  steerInput.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    steerForm.requestSubmit();
  });
  const steerButton = document.createElement('button');
  steerButton.type = 'submit';
  steerButton.textContent = 'Send';
  steerForm.append(steerInput, steerButton);
  steerForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = steerInput.value.trim();
    if (!message) {
      liveOutput.textContent = 'Add a steer instruction first.';
      steerInput.focus();
      return;
    }
    steerButton.disabled = true;
    liveOutput.textContent = 'Sending steer instruction...';
    try {
      await postJson(workerApiUrl(workerId, '/steer'), { message });
      steerInput.value = '';
      autoResizeTextarea(steerInput);
      liveOutput.textContent = 'Steer instruction accepted.';
      await refreshBootstrap();
    } catch (error) {
      liveOutput.textContent = error.message;
    } finally {
      steerButton.disabled = false;
    }
  });

  tile.append(glass, body, actions, steerForm);
  requestAnimationFrame(() => autoResizeTextarea(steerInput));
  return tile;
}

async function runWorkerAction(workerId, action, button, refreshBootstrap) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = action === 'resume' ? 'Starting...' : `${action.charAt(0).toUpperCase()}${action.slice(1)}...`;
  try {
    await postJson(workerApiUrl(workerId, `/action/${encodeURIComponent(action)}`));
    await refreshBootstrap();
  } catch (error) {
    const tile = Array.from(document.querySelectorAll('.workspace-tile')).find((item) => item.dataset.workerId === workerId);
    const output = tile?.querySelector('[data-worker-output]');
    if (output) output.textContent = error.message;
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function runWorkerMetadata(workerId, payload, button, refreshBootstrap) {
  const originalText = button.textContent;
  button.disabled = true;
  try {
    await postJson(workerApiUrl(workerId, '/metadata'), payload);
    await refreshBootstrap();
  } catch (error) {
    const tile = Array.from(document.querySelectorAll('.workspace-tile')).find((item) => item.dataset.workerId === workerId);
    const output = tile?.querySelector('[data-worker-output]');
    if (output) output.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function renderWorkspaceHive(data, refreshBootstrap, viewPrefs = defaultHivePrefs) {
  const grid = document.getElementById('workspace-hive-grid');
  const empty = document.getElementById('workspace-hive-empty');
  const summary = document.getElementById('workspace-hive-summary');
  if (!grid || !empty || !summary) return;
  const prefs = { ...defaultHivePrefs, ...viewPrefs };

  const workspaces = uniqueWorkspaces(data?.existing_workspaces || []);
  const active = workspaces.filter((workspace) => isWorkspaceActive(workspace));
  const resumable = workspaces.filter((workspace) => !isWorkspaceActive(workspace) && isWorkspaceResumable(workspace));
  const inactive = workspaces.filter((workspace) => !isWorkspaceActive(workspace) && !isWorkspaceResumable(workspace));
  const sortFavorites = (items) => [...items].sort((left, right) => Number(Boolean(right.favorite)) - Number(Boolean(left.favorite)));
  const visible = prefs.showInactive ? sortFavorites([...active, ...resumable, ...inactive]) : sortFavorites(active);

  summary.textContent = prefs.showInactive
    ? `${active.length} active · ${resumable.length} retained · ${inactive.length} inactive`
    : `${active.length} active · ${resumable.length + inactive.length} retained hidden`;
  empty.hidden = visible.length > 0;
  if (!visible.length) {
    empty.textContent = prefs.showInactive ? 'No workspaces yet.' : 'No active workspaces right now. Turn on Inactive Workspaces to review completed or retained workspaces.';
  }

  const drafts = new Map(
    Array.from(document.querySelectorAll('.workspace-tile')).map((tile) => [
      tile.dataset.workerId,
      tile.querySelector('.workspace-steer textarea')?.value || '',
    ]),
  );
  const tiles = visible.map((workspace) => {
    const workerId = String(workspace.worker_id || '');
    return renderWorkspaceTile(workspace, refreshBootstrap, drafts.get(workerId) || '', prefs);
  });
  grid.replaceChildren(...tiles);
  refreshVisibleWorkspaceTiles(refreshBootstrap, { force: true }).catch(() => {});
}

function findWorkspace(existing, workerId) {
  return (existing || []).find((item) => item.worker_id === workerId) || null;
}

function workspaceMeta(selectValue, data) {
  const existing = uniqueWorkspaces(data.existing_workspaces || []);
  if ((selectValue || '').startsWith('open:')) {
    const workspace = findWorkspace(existing, selectValue.split(':', 2)[1]);
    const label = workspace?.workspace_label || 'Selected workspace';
    return {
      buttonText: 'Run Project',
      statusText: 'Starting project in selected workspace...',
      help: `Reuses ${label}. If it is paused, GlassHive resumes it automatically before the new run starts.`,
    };
  }
  if ((selectValue || '').startsWith('duplicate:')) {
    const workspace = findWorkspace(existing, selectValue.split(':', 2)[1]);
    const label = workspace?.workspace_label || 'Selected workspace';
    return {
      buttonText: 'Run Project',
      statusText: 'Duplicating workspace and starting project...',
      help: `Creates a new workspace using the files and project context from ${label}. Browser sessions do not copy.`,
    };
  }

  const profile = (selectValue || '').split(':', 2)[1] || 'codex-cli';
  const profileLabel = {
    'codex-cli': 'Codex',
    'claude-code': 'Claude Code',
    'openclaw-general': 'OpenClaw',
  }[profile] || profile;
  const workspaceType = document.getElementById('workspace-type')?.value || 'sandboxed';
  const workspaceKind = workspaceType === 'host' ? 'on your computer' : 'in a fresh sandboxed workspace';
  return {
    buttonText: 'Run Project',
    statusText: 'Starting project...',
    help: `Runs this project ${workspaceKind} with ${profileLabel}.`,
  };
}

function selectedWorkerEffort(selectValue, controls) {
  const profile = (selectValue || '').startsWith('new:')
    ? String(selectValue).split(':', 2)[1] || ''
    : '';
  if (profile === 'codex-cli') return controls.codexEffort?.value || '';
  if (profile === 'claude-code') return controls.claudeEffort?.value || '';
  if (profile === 'openclaw-general') return controls.openclawEffort?.value || '';
  return '';
}

function syncWorkspaceUI(select, data, button, help) {
  const meta = workspaceMeta(select.value, data);
  button.textContent = meta.buttonText;
  help.textContent = meta.help;
  return meta;
}

async function main() {
  const frame = document.querySelector('.composer-frame');
  const projectView = document.getElementById('project-view');
  const workspaceView = document.getElementById('workspace-view');
  const tabs = Array.from(document.querySelectorAll('[data-view-tab]'));
  const form = document.getElementById('launch-form');
  const select = document.getElementById('workspace-option');
  const help = document.getElementById('workspace-help');
  const launchSurface = document.getElementById('launch-surface');
  const workspaceType = document.getElementById('workspace-type');
  const workspaceTypeHelp = document.getElementById('workspace-type-help');
  const status = document.getElementById('launch-status');
  const button = document.getElementById('launch-button');
  const scheduleButton = document.getElementById('schedule-button');
  const scheduleText = document.getElementById('schedule-text');
  const fileInput = document.getElementById('project-files');
  const fileHelp = document.getElementById('file-help');
  const defaultWorker = document.getElementById('default-worker-profile');
  const codexEffort = document.getElementById('codex-effort');
  const claudeEffort = document.getElementById('claude-effort');
  const openclawEffort = document.getElementById('openclaw-effort');
  const savePreferences = document.getElementById('save-preferences');
  const preferencesStatus = document.getElementById('preferences-status');
  const inactiveToggle = document.getElementById('show-inactive-workspaces');
  const watchToggle = document.getElementById('show-workspace-watch');
  const statusToggle = document.getElementById('show-workspace-status');
  let bootstrap = null;
  let activeView = 'project';
  let hivePollTimer = 0;
  const hivePrefs = () => ({
    showInactive: Boolean(inactiveToggle?.checked),
    showWatch: watchToggle ? Boolean(watchToggle.checked) : true,
    showStatus: statusToggle ? Boolean(statusToggle.checked) : true,
  });

  const refreshBootstrap = async () => {
    const selectedValue = select.value;
    bootstrap = await loadBootstrap();
    renderWorkspaceOptions(select, bootstrap, selectedValue);
    syncPreferenceControls(bootstrap, { defaultWorker, codexEffort, claudeEffort, openclawEffort });
    renderWorkspaceHive(bootstrap, refreshBootstrap, hivePrefs());
    syncWorkspaceUI(select, bootstrap, button, help);
    return bootstrap;
  };

  function stopHivePolling() {
    if (!hivePollTimer) return;
    window.clearInterval(hivePollTimer);
    hivePollTimer = 0;
  }

  function startHivePolling() {
    stopHivePolling();
    hivePollTimer = window.setInterval(() => {
      if (activeView === 'workspaces') refreshVisibleWorkspaceTiles(refreshBootstrap).catch(() => {});
    }, ACTIVE_TILE_REFRESH_MS);
  }

  function setActiveView(view, { updateHash = true } = {}) {
    activeView = view === 'workspaces' ? 'workspaces' : 'project';
    frame.dataset.activeView = activeView;
    projectView.hidden = activeView !== 'project';
    workspaceView.hidden = activeView !== 'workspaces';
    for (const tab of tabs) {
      const selected = tab.dataset.viewTab === activeView;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    if (activeView === 'workspaces') {
      if (bootstrap) renderWorkspaceHive(bootstrap, refreshBootstrap, hivePrefs());
      startHivePolling();
    } else {
      stopHivePolling();
    }
    if (updateHash) {
      const nextUrl = `${window.location.pathname}${window.location.search}${activeView === 'workspaces' ? '#workspaces' : ''}`;
      window.history.replaceState(null, '', nextUrl);
    }
  }

  try {
    bootstrap = await loadBootstrap();
    renderWorkspaceOptions(select, bootstrap);
    syncPreferenceControls(bootstrap, { defaultWorker, codexEffort, claudeEffort, openclawEffort });
    if (launchSurface) launchSurface.value = String(bootstrap.default_launch_surface || 'desktop');
    if (workspaceType) renderWorkspaceTypeOptions(workspaceType, workspaceTypeHelp, bootstrap);
    renderWorkspaceHive(bootstrap, refreshBootstrap, hivePrefs());
    syncWorkspaceUI(select, bootstrap, button, help);
  } catch (error) {
    button.disabled = true;
    if (scheduleButton) scheduleButton.disabled = true;
    select.disabled = true;
    if (workspaceType) workspaceType.disabled = true;
    form.classList.add('is-unavailable');
    status.textContent = error.message;
  }

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => setActiveView(tab.dataset.viewTab));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const currentIndex = tabs.indexOf(tab);
      const nextIndex = event.key === 'ArrowRight'
        ? (currentIndex + 1) % tabs.length
        : (currentIndex + tabs.length - 1) % tabs.length;
      tabs[nextIndex].focus();
      setActiveView(tabs[nextIndex].dataset.viewTab);
    });
  });

  for (const toggle of [inactiveToggle, watchToggle, statusToggle]) {
    toggle?.addEventListener('change', () => {
      if (!bootstrap) return;
      renderWorkspaceHive(bootstrap, refreshBootstrap, hivePrefs());
    });
  }

  select.addEventListener('change', () => {
    if (!bootstrap) return;
    syncWorkspaceUI(select, bootstrap, button, help);
  });

  workspaceType?.addEventListener('change', () => {
    const selected = workspaceType.selectedOptions?.[0];
    if (workspaceTypeHelp) {
      workspaceTypeHelp.textContent = selected?.dataset.description || 'Runs on managed GlassHive workspace compute.';
    }
    if (bootstrap) syncWorkspaceUI(select, bootstrap, button, help);
  });

  window.addEventListener('glasshive:duplicate-workspace', (event) => {
    if (!bootstrap) return;
    const workerId = String(event.detail?.workerId || '');
    if (!workerId) return;
    const value = `duplicate:${workerId}`;
    renderWorkspaceOptions(select, bootstrap, value);
    syncWorkspaceUI(select, bootstrap, button, help);
    setActiveView('project');
    select.focus();
  });

  fileInput?.addEventListener('change', () => {
    const files = Array.from(fileInput.files || []);
    if (!fileHelp) return;
    if (!files.length) {
      fileHelp.textContent = 'No files selected.';
      return;
    }
    const total = files.reduce((sum, file) => sum + file.size, 0);
    fileHelp.textContent = `${files.length} file${files.length === 1 ? '' : 's'} selected · ${(total / 1024 / 1024).toFixed(2)} MB`;
  });

  savePreferences?.addEventListener('click', async () => {
    if (!bootstrap) return;
    savePreferences.disabled = true;
    if (preferencesStatus) preferencesStatus.textContent = 'Saving defaults...';
    try {
      const updated = await patchJson('/api/preferences', {
        default_worker_profile: defaultWorker?.value || '',
        codex_reasoning_effort: codexEffort?.value || '',
        claude_effort: claudeEffort?.value || '',
        openclaw_effort: openclawEffort?.value || '',
      });
      bootstrap.user_preferences = updated;
      bootstrap.default_workspace_option = updated.default_worker_profile
        ? `new:${updated.default_worker_profile}`
        : String(bootstrap.deployment_default_workspace_option || 'new:codex-cli');
      renderWorkspaceOptions(select, bootstrap, select.value);
      syncWorkspaceUI(select, bootstrap, button, help);
      if (preferencesStatus) preferencesStatus.textContent = 'Defaults saved.';
    } catch (error) {
      if (preferencesStatus) preferencesStatus.textContent = error.message;
    } finally {
      savePreferences.disabled = false;
    }
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!bootstrap) {
      status.textContent = 'Workspace options are not available for this session.';
      return;
    }
    const submitterId = event.submitter?.id || 'launch-button';
    const wantsSchedule = submitterId === 'schedule-button';
    const scheduleValue = scheduleText?.value.trim() || '';
    if (wantsSchedule && !scheduleValue) {
      status.textContent = 'Add a schedule before saving this for later.';
      scheduleText?.focus();
      return;
    }
    button.disabled = true;
    if (scheduleButton) scheduleButton.disabled = true;
    const meta = syncWorkspaceUI(select, bootstrap, button, help);
    status.textContent = wantsSchedule ? 'Saving schedule...' : meta.statusText;
    let files = [];
    try {
      files = await selectedFilePayloads(fileInput);
    } catch (error) {
      button.disabled = false;
      if (scheduleButton) scheduleButton.disabled = false;
      status.textContent = error.message;
      return;
    }
    const payload = {
      description: document.getElementById('description').value.trim(),
      success_criteria: document.getElementById('success_criteria').value.trim(),
      context: document.getElementById('context').value.trim(),
      workspace_option: select.value,
      workspace_type: workspaceType?.value || 'sandboxed',
      launch_surface: launchSurface?.value || 'desktop',
      schedule_text: wantsSchedule ? scheduleValue : '',
      effort: selectedWorkerEffort(select.value, { codexEffort, claudeEffort, openclawEffort }),
      files,
    };
    try {
      const response = await fetch(withAuth('/api/launch'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(await responseMessage(response, 'Launch failed'));
      }
      const data = await response.json();
      if (data.status === 'scheduled') {
        status.textContent = `Scheduled for ${data.scheduled_for || scheduleValue}.`;
        await refreshBootstrap();
        setActiveView('workspaces');
        button.disabled = false;
        if (scheduleButton) scheduleButton.disabled = false;
        return;
      }
      window.location.href = data.watch_url;
    } catch (error) {
      button.disabled = false;
      if (scheduleButton) scheduleButton.disabled = false;
      status.textContent = error.message;
    }
  });

  setActiveView(window.location.hash === '#workspaces' ? 'workspaces' : 'project', { updateHash: false });
  window.addEventListener('hashchange', () => {
    setActiveView(window.location.hash === '#workspaces' ? 'workspaces' : 'project', { updateHash: false });
  });
}

main();
