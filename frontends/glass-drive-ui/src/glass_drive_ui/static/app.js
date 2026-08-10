import { initializeControlPlane, refreshControlPlane, renderActivity } from './control-plane.js?v=20260809b';

const ACTIVE_STATES = new Set(['created', 'starting', 'queued', 'running', 'resuming']);
const ACTIVE_RUN_STATES = new Set(['queued', 'running']);
const TERMINAL_ATTENTION_STATES = new Set(['failed', 'cancelled', 'interrupted']);
const INTERRUPTIBLE_STATES = new Set(['queued', 'running', 'resuming']);
const RESUME_STATES = new Set(['ready', 'paused', 'idle', 'idle_terminated', 'stopped', 'completed', 'retained']);
const DISABLED_CONTROL_STATES = new Set(['created', 'starting', 'terminating', 'termination_failed', 'terminated']);
const MAX_LIVE_TILE_IFRAMES = 4;
const ACTIVE_TILE_REFRESH_MS = 7000;
const RETAINED_TILE_REFRESH_MS = 60000;
const GLASSHIVE_UI_REV = '20260809b';
let workspaceRefreshInFlight = false;
let csrfToken = '';
let renameWorkspaceContext = null;
let saveTemplateContext = null;
const pageParams = new URLSearchParams(window.location.search);
const signedToken = pageParams.get('gh_token') || '';

const defaultHivePrefs = {
  showInactive: true,
  showWatch: true,
  showStatus: true,
  search: '',
};

async function loadBootstrap() {
  const response = await fetch(withAuth('/api/bootstrap'));
  if (!response.ok) throw new Error(await responseMessage(response, 'Failed to load workspace options'));
  const payload = await response.json();
  csrfToken = String(payload.csrf_token || '');
  return payload;
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
      if (payload.detail && typeof payload.detail === 'object') {
        return [payload.detail.message, payload.detail.recovery].filter(Boolean).join(' ') || fallback;
      }
      return String(payload.detail || payload.message || fallback);
    }
    const text = await response.text();
    return text.trim() || fallback;
  } catch {
    return fallback;
  }
}

async function getJson(url, fallback = 'Request failed') {
  const response = await fetch(withAuth(url), { cache: 'no-store' });
  if (!response.ok) throw new Error(await responseMessage(response, fallback));
  return response.json();
}

function decorateCatalogWorkspace(workspace) {
  const workerId = String(workspace?.worker_id || '');
  const projectId = String(workspace?.project_id || '');
  const state = rawWorkspaceState(workspace);
  const projectTitle = String(workspace?.project_title || workspace?.name || 'Workspace');
  return {
    ...workspace,
    workspace_label: projectTitle,
    state_label: state === 'ready' ? 'retained' : state,
    is_active: ACTIVE_STATES.has(state),
    is_resumable: RESUME_STATES.has(state),
    watch_url: `/watch/${encodeURIComponent(workerId)}?project_id=${encodeURIComponent(projectId)}&surface=desktop`,
    project_url: `/ui/projects/${encodeURIComponent(projectId)}?worker_id=${encodeURIComponent(workerId)}`,
    desktop_url: `/desktop/${encodeURIComponent(workerId)}`,
    api_url: `/api/worker/${encodeURIComponent(workerId)}`,
    control_url: `/api/worker/${encodeURIComponent(workerId)}`,
  };
}

function scheduleLabel(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? raw : parsed.toLocaleString();
}

async function postJson(url, payload) {
  const response = await fetch(withAuth(url), {
    method: 'POST',
    headers: {
      ...(payload ? { 'Content-Type': 'application/json' } : {}),
      ...(csrfToken ? { 'X-GlassHive-CSRF': csrfToken } : {}),
    },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) throw new Error(await responseMessage(response, 'Request failed'));
  return response.json();
}

async function patchJson(url, payload) {
  const response = await fetch(withAuth(url), {
    method: 'PATCH',
    headers: {
      ...(payload ? { 'Content-Type': 'application/json' } : {}),
      ...(csrfToken ? { 'X-GlassHive-CSRF': csrfToken } : {}),
    },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) throw new Error(await responseMessage(response, 'Request failed'));
  return response.json();
}

async function deleteJson(url) {
  const response = await fetch(withAuth(url), {
    method: 'DELETE',
    headers: csrfToken ? { 'X-GlassHive-CSRF': csrfToken } : {},
  });
  if (!response.ok) throw new Error(await responseMessage(response, 'Request failed'));
  return response.json();
}

function renderCurrentUser(identity = {}) {
  const control = document.getElementById('current-user-control');
  const label = document.getElementById('current-user-label');
  if (!control || !label || !csrfToken) return;
  const name = String(identity.display_name || '').trim();
  const email = String(identity.email || '').trim();
  label.textContent = name && email ? `${name} · ${email}` : name || email || 'Signed in';
  label.title = label.textContent;
  control.hidden = false;
}

async function signOut(scope) {
  const response = await postJson('/auth/logout', { scope });
  window.location.assign(String(response.redirect_url || '/login'));
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

function renderLaunchProviderAccounts(accountSelect, policySelect, help, data, workspaceValue) {
  if (!accountSelect || !policySelect) return;
  const isNewWorkspace = String(workspaceValue || '').startsWith('new:');
  const profile = isNewWorkspace ? String(workspaceValue).split(':', 2)[1] || '' : '';
  const supportedProviders = {
    'codex-cli': new Set(['codex', 'openai']),
    'claude-code': new Set(['claude', 'anthropic']),
  }[profile];
  const currentAccount = accountSelect.value;
  const currentPolicy = policySelect.value || 'personal_preferred';

  if (!isNewWorkspace) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'Uses saved workspace policy';
    accountSelect.replaceChildren(option);
    accountSelect.disabled = true;
    policySelect.disabled = true;
    if (help) help.textContent = 'Existing workspaces keep their saved worker account and credential policy.';
    return;
  }
  if (!supportedProviders) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'Deployment-managed account';
    accountSelect.replaceChildren(option);
    accountSelect.disabled = true;
    policySelect.value = 'legacy';
    policySelect.disabled = true;
    if (help) help.textContent = 'Personal subscriptions are available for Codex and Claude Code workers.';
    return;
  }

  policySelect.disabled = false;
  policySelect.value = currentPolicy;
  const readyAccounts = (data?.provider_accounts || []).filter((account) => (
    supportedProviders.has(String(account.provider || '').toLowerCase())
    && String(account.status || '').toLowerCase() === 'ready'
  ));
  const defaultAccount = readyAccounts.find((account) => Boolean(account.is_default));
  const defaultOption = document.createElement('option');
  defaultOption.value = '';
  defaultOption.textContent = defaultAccount
    ? `Default — ${String(defaultAccount.label || defaultAccount.provider || 'personal account')}`
    : 'No ready default account';
  const options = [defaultOption, ...readyAccounts.map((account) => {
    const option = document.createElement('option');
    option.value = String(account.account_id || '');
    option.textContent = `${String(account.label || account.provider || 'Personal account')}${account.is_default ? ' (default)' : ''}`;
    return option;
  })];
  accountSelect.replaceChildren(...options);
  accountSelect.value = options.some((option) => option.value === currentAccount) ? currentAccount : '';

  const policy = policySelect.value;
  accountSelect.disabled = policy === 'legacy' || readyAccounts.length === 0;
  if (!help) return;
  if (policy === 'legacy') {
    help.textContent = 'This worker will use the deployment-managed account.';
  } else if (policy === 'personal_required') {
    help.textContent = defaultAccount || accountSelect.value
      ? 'GlassHive will stop the launch if this personal account is unavailable.'
      : 'Connect or choose a ready personal account; GlassHive will not fall back to global credentials.';
  } else {
    help.textContent = defaultAccount || accountSelect.value
      ? 'Uses this personal account when ready, with deployment fallback if it becomes unavailable.'
      : 'No ready default is connected, so this launch may use the deployment-managed account.';
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
  return String(workspace?.close_state || workspace?.state || 'unknown').trim().toLowerCase() || 'unknown';
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
  if (normalized === 'terminating') return 'Closing';
  if (normalized === 'termination_failed') return 'Close needs attention';
  if (normalized === 'ready') return 'Ready';
  if (normalized === 'retained') return 'Retained';
  return normalized || 'unknown';
}

function workspaceTileTitle(workspace) {
  const label = String(workspace?.workspace_label || '').trim();
  const name = String(workspace?.name || '').trim();
  // The user-editable workspace name is the primary discovery label. Project
  // context remains available separately and must not be concatenated back
  // into the name after a rename.
  return name || label || 'Workspace';
}

function workspaceOptionLabel(workspace) {
  if (!workspace) return 'selected workspace';
  return `${workspaceTileTitle(workspace)} · ${workspaceProfileLabel(workspace.profile)} · ${workspaceStateLabel(workspace)}`;
}

function workerActionForState(state) {
  const normalized = String(state || '').trim().toLowerCase();
  return RESUME_STATES.has(normalized) ? 'resume' : 'pause';
}

function syncTileSteerAvailability(tile, state) {
  const normalized = String(state || '').trim().toLowerCase();
  tile.dataset.displayState = normalized;
  const closed = ['terminating', 'termination_failed', 'terminated'].includes(normalized);
  const input = tile.querySelector('.workspace-steer textarea');
  const button = tile.querySelector('.workspace-steer button[type="submit"]');
  if (input) {
    input.disabled = closed;
    input.placeholder = closed ? 'This workspace is closed' : 'Steer this workspace';
  }
  if (button) button.disabled = closed;
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
    toggle.hidden = TERMINAL_ATTENTION_STATES.has(normalized) || ['terminating', 'termination_failed', 'terminated'].includes(normalized);
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
  syncTileSteerAvailability(tile, state);
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

  if (TERMINAL_ATTENTION_STATES.has(normalized)) {
    const note = document.createElement('div');
    note.className = 'workspace-glass-note';
    note.textContent = 'Needs attention · Send a corrected follow-up below';
    pane.replaceChildren(note);
    return;
  }

  if (['terminating', 'termination_failed', 'terminated'].includes(normalized)) {
    const note = document.createElement('div');
    note.className = 'workspace-glass-note';
    note.textContent = normalized === 'terminating' ? 'Workspace closing' : normalized === 'termination_failed' ? 'Close needs attention' : 'Workspace closed';
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
  const workerState = String(data?.worker?.close_state || data?.worker?.state || '').trim().toLowerCase();
  const runState = String(data?.latest_run?.state || '').trim().toLowerCase();
  if (['terminating', 'termination_failed', 'terminated'].includes(workerState)) return workerState;
  if (runState === 'completed') return 'completed';
  if (ACTIVE_RUN_STATES.has(runState)) return runState;
  if (['failed', 'cancelled', 'interrupted'].includes(runState)) return runState;
  if (['paused', 'idle', 'idle_terminated', 'stopped', 'ready'].includes(workerState) && !ACTIVE_RUN_STATES.has(runState)) {
    return workerState === 'ready' ? 'completed' : workerState;
  }
  return workerState || 'unknown';
}

function updateWorkspaceMeta(meta, profile, state, catalogDetails = null) {
  if (!meta) return;
  if (Array.isArray(catalogDetails)) {
    meta.dataset.catalogDetails = JSON.stringify(catalogDetails);
  }
  let details = [];
  try {
    details = JSON.parse(meta.dataset.catalogDetails || '[]');
  } catch {
    details = [];
  }
  meta.textContent = [workspaceProfileLabel(profile), displayStateLabel(state), ...details].join(' · ');
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
    updateWorkspaceMeta(meta, data?.worker?.profile, state);
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

function renderWorkspaceTile(workspace, refreshBootstrap, draftMessage = '', viewPrefs = defaultHivePrefs, bootstrap = {}) {
  const workerId = String(workspace.worker_id || '');
  const state = workspaceStateLabel(workspace).toLowerCase();
  const isActive = isWorkspaceActive(workspace);
  const isResumable = isWorkspaceResumable(workspace);
  const currentSelection = workspace.provider_account || {};
  const currentPolicy = String(currentSelection.policy || 'legacy');
  const currentAccountId = String(currentSelection.account_id || '');
  const selectedAccount = (bootstrap.provider_accounts || []).find(
    (account) => String(account.account_id || '') === currentAccountId,
  );

  const tile = document.createElement('article');
  tile.className = 'workspace-tile';
  tile.dataset.state = isActive ? 'active' : isResumable ? 'resumable' : 'inactive';
  tile.dataset.workerId = workerId;
  tile.dataset.desktopUrl = String(workspace.desktop_url || '');
  tile.dataset.apiUrl = String(workspace.control_url || workspace.api_url || '');
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
  if (['terminating', 'termination_failed', 'terminated'].includes(state)) {
    const note = document.createElement('div');
    note.className = 'workspace-glass-note';
    note.textContent = state === 'terminating' ? 'Workspace closing' : state === 'termination_failed' ? 'Close needs attention' : 'Workspace closed';
    glassPane.appendChild(note);
  } else if (isActive) {
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
  const metaParts = [workspaceProfileLabel(workspace.profile), displayStateLabel(state)];
  const providerReadiness = workspace.provider_readiness || {};
  if (providerReadiness.readiness === 'action_required') {
    const accountLabel = String(providerReadiness.label || selectedAccount?.label || 'saved personal account');
    metaParts.push(`${accountLabel}: reconnect required`);
  } else if (providerReadiness.readiness === 'ready') {
    metaParts.push(`${String(providerReadiness.label || selectedAccount?.label || 'personal account')}: ready`);
  } else if (providerReadiness.readiness === 'deployment_managed') {
    metaParts.push(providerReadiness.policy === 'personal_preferred'
      ? 'deployment account fallback'
      : 'deployment account');
  } else if (currentPolicy === 'legacy') {
    metaParts.push('deployment account');
  } else if (selectedAccount) {
    const accountLabel = String(selectedAccount.label || selectedAccount.provider || 'personal account');
    metaParts.push(`${accountLabel}: ${String(selectedAccount.status || 'unknown').replaceAll('_', ' ')}`);
  } else {
    metaParts.push('personal account: reconnect required');
  }
  const capabilityReadiness = workspace.capability_readiness || {};
  if (capabilityReadiness.readiness === 'action_required') {
    metaParts.push(`${Number(capabilityReadiness.unavailable_grants || 0)} connection${Number(capabilityReadiness.unavailable_grants || 0) === 1 ? '' : 's'} need attention`);
  } else if (Number(capabilityReadiness.active_grants || 0) > 0) {
    metaParts.push(`${Number(capabilityReadiness.active_grants)} connected capabilit${Number(capabilityReadiness.active_grants) === 1 ? 'y' : 'ies'}`);
  }
  if (workspace.next_schedule_at) metaParts.push(`Next: ${scheduleLabel(workspace.next_schedule_at)}`);
  if (workspace.schedule_readiness === 'unavailable') metaParts.push('schedule status unavailable');
  if ((workspace.tags || []).length) metaParts.push(`Tags: ${(workspace.tags || []).join(', ')}`);
  const nextSchedule = (bootstrap.recurring_schedules || [])
    .filter((schedule) => String(schedule.worker_id || '') === workerId && schedule.enabled !== false)
    .sort((left, right) => String(left.next_run_at || '').localeCompare(String(right.next_run_at || '')))[0];
  if (nextSchedule?.next_run_at && !workspace.next_schedule_at) {
    const nextRun = new Date(String(nextSchedule.next_run_at));
    metaParts.push(`Next: ${Number.isNaN(nextRun.getTime()) ? String(nextSchedule.next_run_at) : nextRun.toLocaleString()}`);
  } else if (bootstrap.recurring_schedules_status === 'unavailable') {
    metaParts.push('schedule status unavailable');
  }
  updateWorkspaceMeta(meta, workspace.profile, state, metaParts.slice(2));
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
  watch.addEventListener('click', async () => {
    await openWorkspaceSurface(workspace, watch);
  });
  actions.appendChild(watch);

  const project = createButton('Project');
  project.addEventListener('click', () => {
    window.location.href = String(workspace.project_url || '#');
  });
  actions.appendChild(project);

  const duplicate = createButton('Duplicate');
  duplicate.addEventListener('click', () => duplicateSavedWorkspace(workspace, duplicate, refreshBootstrap));
  actions.appendChild(duplicate);

  const saveTemplate = createButton('Save as template');
  saveTemplate.addEventListener('click', () => openSaveTemplate(workspace, refreshBootstrap));
  actions.appendChild(saveTemplate);

  const accountSelect = document.createElement('select');
  accountSelect.className = 'workspace-account-select';
  accountSelect.setAttribute('aria-label', `Worker account for ${workspaceTileTitle(workspace)}`);
  const deploymentOption = document.createElement('option');
  deploymentOption.value = 'legacy:';
  deploymentOption.textContent = 'Account · deployment-managed';
  const supportedProviders = workspace.profile === 'codex-cli'
    ? new Set(['codex', 'openai'])
    : workspace.profile === 'claude-code'
      ? new Set(['claude', 'anthropic'])
      : new Set();
  const accountOptions = (bootstrap.provider_accounts || [])
    .filter((account) => supportedProviders.has(String(account.provider || '').toLowerCase()) && account.status === 'ready')
    .flatMap((account) => {
      const label = String(account.label || account.provider || 'my account');
      const preferred = document.createElement('option');
      preferred.value = `personal_preferred:${String(account.account_id || '')}`;
      preferred.textContent = `Account · prefer ${label} (fallback allowed)`;
      const required = document.createElement('option');
      required.value = `personal_required:${String(account.account_id || '')}`;
      required.textContent = `Account · only ${label}`;
      return [preferred, required];
    });
  const selectedValue = `${currentPolicy}:${currentAccountId}`;
  if (
    currentPolicy !== 'legacy'
    && currentAccountId
    && !accountOptions.some((option) => option.value === selectedValue)
  ) {
    const unavailable = document.createElement('option');
    unavailable.value = selectedValue;
    unavailable.textContent = `Account · ${String(selectedAccount?.label || selectedAccount?.provider || 'saved personal account')} · reconnect required`;
    accountOptions.unshift(unavailable);
  }
  accountSelect.replaceChildren(deploymentOption, ...accountOptions);
  accountSelect.value = Array.from(accountSelect.options).some((option) => option.value === selectedValue)
    ? selectedValue
    : 'legacy:';
  accountSelect.disabled = !supportedProviders.size || ['created', 'starting', 'queued', 'running', 'resuming', 'terminating', 'termination_failed', 'terminated'].includes(state);
  accountSelect.addEventListener('change', async () => {
    const priorValue = selectedValue;
    const [policy, accountId = ''] = String(accountSelect.value || '').split(':', 2);
    accountSelect.disabled = true;
    try {
      const pending = await postJson('/api/pending-changes', {
        change_type: 'workspace_provider_account',
        target_id: workerId,
        payload: { policy, ...(accountId ? { account_id: accountId } : {}) },
      });
      const changeId = encodeURIComponent(String(pending.change_id || ''));
      const token = encodeURIComponent(String(pending.confirmation_token || ''));
      if (!changeId || !token) throw new Error('GlassHive could not prepare the account review.');
      window.location.assign(`/confirm-change#change_id=${changeId}&token=${token}`);
    } catch (error) {
      accountSelect.value = priorValue;
      accountSelect.disabled = false;
      liveOutput.textContent = error.message;
    }
  });
  actions.appendChild(accountSelect);

  if (workspace.workspace_kind === 'ephemeral' || workspace.workspace_kind === 'legacy') {
    const keep = createButton('Keep as workspace');
    keep.addEventListener('click', async () => {
      await runWorkerMetadata(workerId, { workspace_kind: 'named' }, keep, refreshBootstrap);
    });
    actions.appendChild(keep);
  }

  const rename = createButton('Rename');
  rename.addEventListener('click', () => openRenameWorkspace(workspace, refreshBootstrap));
  actions.appendChild(rename);

  const toggle = createButton(state === 'completed' ? 'Continue' : workerActionForState(state) === 'resume' ? 'Resume' : 'Pause', 'workspace-run-toggle');
  toggle.dataset.workerActionToggle = 'true';
  toggle.dataset.action = workerActionForState(state);
  toggle.hidden = TERMINAL_ATTENTION_STATES.has(state) || ['terminating', 'termination_failed', 'terminated'].includes(state);
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
      syncTileSteerAvailability(tile, tile.dataset.displayState || state);
    }
  });

  tile.append(glass, body, actions, steerForm);
  syncTileSteerAvailability(tile, state);
  requestAnimationFrame(() => autoResizeTextarea(steerInput));
  return tile;
}

async function openWorkspaceSurface(workspace, button) {
  const workerId = String(workspace?.worker_id || '');
  const shouldResume = workspace?.workspace_kind === 'named'
    && (Boolean(workspace?.compute_released_at) || ['paused', 'idle', 'idle_terminated', 'stopped'].includes(rawWorkspaceState(workspace)));
  const originalText = button?.textContent || '';
  if (button) button.disabled = true;
  try {
    if (shouldResume) {
      if (button) button.textContent = 'Resuming…';
      await postJson(workerApiUrl(workerId, '/action/resume'));
    }
    window.location.href = String(workspace?.watch_url || '#');
  } catch (error) {
    const tile = Array.from(document.querySelectorAll('.workspace-tile')).find((item) => item.dataset.workerId === workerId);
    const output = tile?.querySelector('[data-worker-output]');
    if (output) output.textContent = error.message;
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
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

async function duplicateSavedWorkspace(workspace, button, refreshBootstrap) {
  const workerId = String(workspace.worker_id || '');
  if (!workerId) return;
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = 'Duplicating…';
  button.dataset.idempotencyKey ||= globalThis.crypto?.randomUUID?.()
    || `duplicate-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    await postJson(
      `/api/workspaces/${encodeURIComponent(workerId)}/duplicate`,
      { idempotency_key: button.dataset.idempotencyKey },
    );
    delete button.dataset.idempotencyKey;
    button.textContent = 'Duplicated';
    await refreshBootstrap();
  } catch (error) {
    const tile = Array.from(document.querySelectorAll('.workspace-tile')).find((item) => item.dataset.workerId === workerId);
    const output = tile?.querySelector('[data-worker-output]');
    if (output) output.textContent = `${error.message} Retry will use the same request key.`;
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = originalText;
    }, 900);
  }
}

function openSaveTemplate(workspace, refreshBootstrap) {
  const dialog = document.getElementById('save-template-dialog');
  const name = document.getElementById('save-template-name');
  const description = document.getElementById('save-template-description');
  const status = document.getElementById('save-template-status');
  if (!dialog || !name) return;
  saveTemplateContext = {
    workerId: String(workspace.worker_id || ''),
    refreshBootstrap,
  };
  name.value = `${workspaceTileTitle(workspace)} template`;
  if (description) description.value = '';
  if (status) status.textContent = '';
  dialog.showModal();
  name.focus();
  name.select();
}

function openRenameWorkspace(workspace, refreshBootstrap) {
  const dialog = document.getElementById('rename-workspace-dialog');
  const input = document.getElementById('rename-workspace-name');
  const status = document.getElementById('rename-workspace-status');
  if (!dialog || !input) return;
  renameWorkspaceContext = {
    workerId: String(workspace.worker_id || ''),
    refreshBootstrap,
  };
  input.value = workspaceTileTitle(workspace);
  const tagsInput = document.getElementById('rename-workspace-tags');
  if (tagsInput) tagsInput.value = (workspace.tags || []).join(', ');
  if (status) status.textContent = '';
  dialog.showModal();
  input.focus();
  input.select();
}

function renderWorkspaceHive(data, refreshBootstrap, viewPrefs = defaultHivePrefs) {
  const grid = document.getElementById('workspace-hive-grid');
  const empty = document.getElementById('workspace-hive-empty');
  const summary = document.getElementById('workspace-hive-summary');
  const loadMore = document.getElementById('workspace-load-more');
  const catalogStatus = document.getElementById('workspace-catalog-status');
  if (!grid || !empty || !summary) return;
  const prefs = { ...defaultHivePrefs, ...viewPrefs };
  renderWorkspaceTemplateOptions(data);

  const workspaces = uniqueWorkspaces(data?.existing_workspaces || []);
  const catalogUnavailable = data?.workspace_catalog_status === 'unavailable';
  const active = workspaces.filter((workspace) => isWorkspaceActive(workspace));
  const resumable = workspaces.filter((workspace) => !isWorkspaceActive(workspace) && isWorkspaceResumable(workspace));
  const inactive = workspaces.filter((workspace) => !isWorkspaceActive(workspace) && !isWorkspaceResumable(workspace));
  const sortFavorites = (items) => [...items].sort((left, right) => Number(Boolean(right.favorite)) - Number(Boolean(left.favorite)));
  const visible = prefs.showInactive ? sortFavorites([...active, ...resumable, ...inactive]) : sortFavorites(active);

  summary.textContent = prefs.showInactive
    ? `${active.length} active · ${resumable.length} retained · ${inactive.length} inactive · ${workspaces.length} loaded`
    : `${active.length} active · ${resumable.length + inactive.length} retained hidden · ${workspaces.length} loaded`;
  if (loadMore) loadMore.hidden = !data?.workspace_catalog_next_cursor;
  if (catalogStatus && !data?.workspace_catalog_loading) {
    catalogStatus.textContent = catalogUnavailable
      ? 'Your workspaces are temporarily unavailable. Refresh to retry; this does not mean they were removed.'
      : '';
  }
  empty.hidden = visible.length > 0;
  if (!visible.length) {
    empty.textContent = catalogUnavailable
      ? 'Workspace data is unavailable right now.'
      : prefs.showInactive
        ? 'No workspaces yet.'
        : 'No active workspaces right now. Turn on Inactive Workspaces to review completed or retained workspaces.';
  }

  const drafts = new Map(
    Array.from(document.querySelectorAll('.workspace-tile')).map((tile) => [
      tile.dataset.workerId,
      tile.querySelector('.workspace-steer textarea')?.value || '',
    ]),
  );
  const tiles = visible.map((workspace) => {
    const workerId = String(workspace.worker_id || '');
    return renderWorkspaceTile(workspace, refreshBootstrap, drafts.get(workerId) || '', prefs, data || {});
  });
  grid.replaceChildren(...tiles);
  refreshVisibleWorkspaceTiles(refreshBootstrap, { force: true }).catch(() => {});
}

function renderWorkspaceTemplateOptions(data) {
  const select = document.getElementById('workspace-template-select');
  const button = document.getElementById('workspace-template-start');
  const meta = document.getElementById('workspace-template-meta');
  if (!select) return;
  const selected = select.value;
  const templates = Array.isArray(data?.workspace_templates) ? data.workspace_templates : [];
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = templates.length ? 'Choose a saved template' : 'No saved templates yet';
  const options = templates.map((template) => {
    const option = document.createElement('option');
    option.value = String(template.template_id || '');
    option.textContent = `${String(template.name || 'Workspace template')} · v${Number(template.version || 1)} · ${workspaceProfileLabel(template.profile)}`;
    option.dataset.libraryCount = String((template.library_refs || []).length);
    return option;
  });
  select.replaceChildren(placeholder, ...options);
  if (options.some((option) => option.value === selected)) select.value = selected;
  select.disabled = !templates.length;
  if (button) button.disabled = !templates.length;
  const chosen = templates.find((template) => String(template.template_id || '') === select.value);
  if (meta) {
    const capabilityCount = (chosen?.library_refs || []).length;
    meta.textContent = chosen
      ? `Creates a fresh paused ${workspaceProfileLabel(chosen.profile)} workspace. ${capabilityCount ? `${capabilityCount} Library capability approval${capabilityCount === 1 ? '' : 's'} will be required.` : 'No capability approval is carried over.'}`
      : 'Templates create a fresh paused workspace. Connected capabilities require fresh approval.';
  }
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
  const tabs = Array.from(document.querySelectorAll('[data-view-tab]'));
  const viewRegistry = new Map(
    tabs.map((tab) => [String(tab.dataset.viewTab), document.getElementById(String(tab.getAttribute('aria-controls')))]),
  );
  const form = document.getElementById('launch-form');
  const select = document.getElementById('workspace-option');
  const help = document.getElementById('workspace-help');
  const launchSurface = document.getElementById('launch-surface');
  const workspaceType = document.getElementById('workspace-type');
  const workspaceTypeHelp = document.getElementById('workspace-type-help');
  const providerAccount = document.getElementById('provider-account-selection');
  const providerAccountPolicy = document.getElementById('provider-account-policy');
  const providerAccountHelp = document.getElementById('provider-account-help');
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
  const workspaceSearch = document.getElementById('workspace-search');
  const workspaceKindFilter = document.getElementById('workspace-kind-filter');
  const workspaceTagFilter = document.getElementById('workspace-tag-filter');
  const workspaceLoadMore = document.getElementById('workspace-load-more');
  const workspaceCatalogStatus = document.getElementById('workspace-catalog-status');
  const renameDialog = document.getElementById('rename-workspace-dialog');
  const renameForm = document.getElementById('rename-workspace-form');
  const renameInput = document.getElementById('rename-workspace-name');
  const renameTags = document.getElementById('rename-workspace-tags');
  const renameStatus = document.getElementById('rename-workspace-status');
  const cancelRename = document.getElementById('cancel-workspace-rename');
  const saveTemplateDialog = document.getElementById('save-template-dialog');
  const saveTemplateForm = document.getElementById('save-template-form');
  const saveTemplateName = document.getElementById('save-template-name');
  const saveTemplateDescription = document.getElementById('save-template-description');
  const saveTemplateStatus = document.getElementById('save-template-status');
  const cancelSaveTemplate = document.getElementById('cancel-save-template');
  const switchAccount = document.getElementById('switch-account');
  const localSignOut = document.getElementById('local-sign-out');
  const templateStartForm = document.getElementById('workspace-template-start-form');
  const templateSelect = document.getElementById('workspace-template-select');
  const templateInstanceName = document.getElementById('workspace-template-instance-name');
  const templateStartStatus = document.getElementById('workspace-template-start-status');
  const templateStartButton = document.getElementById('workspace-template-start');
  let bootstrap = null;
  let activeView = 'project';
  let hivePollTimer = 0;
  let catalogSearchTimer = 0;
  const catalogState = { items: [], nextCursor: null, loading: false, generation: 0 };
  const hivePrefs = () => ({
    showInactive: Boolean(inactiveToggle?.checked),
    showWatch: watchToggle ? Boolean(watchToggle.checked) : true,
    showStatus: statusToggle ? Boolean(statusToggle.checked) : true,
    search: String(workspaceSearch?.value || ''),
  });

  const workspaceViewData = () => ({
    ...(bootstrap || {}),
    existing_workspaces: catalogState.items,
    workspace_catalog_next_cursor: catalogState.nextCursor,
    workspace_catalog_loading: catalogState.loading,
    workspace_catalog_status: bootstrap?.bootstrap_sections?.workspace_catalog || 'ready',
  });

  const fetchCatalogPage = async ({ kind, search = '', tags = '', cursor = '' }) => {
    const query = new URLSearchParams({ kind, search, tags, limit: '25' });
    if (cursor) query.set('cursor', cursor);
    return getJson(`/api/workspaces?${query.toString()}`, 'GlassHive could not load workspaces.');
  };

  const refreshWorkspaceCatalog = async ({ append = false } = {}) => {
    if (!bootstrap || (append && catalogState.loading)) return;
    const generation = ++catalogState.generation;
    catalogState.loading = true;
    if (workspaceCatalogStatus) workspaceCatalogStatus.textContent = append ? 'Loading more…' : 'Loading workspaces…';
    if (workspaceLoadMore) workspaceLoadMore.disabled = true;
    try {
      const payload = await fetchCatalogPage({
        kind: String(workspaceKindFilter?.value || 'named'),
        search: String(workspaceSearch?.value || '').trim(),
        tags: String(workspaceTagFilter?.value || '').trim(),
        cursor: append ? String(catalogState.nextCursor || '') : '',
      });
      if (generation !== catalogState.generation) return;
      const incoming = (payload.items || []).map(decorateCatalogWorkspace);
      catalogState.items = append ? uniqueWorkspaces([...catalogState.items, ...incoming]) : incoming;
      catalogState.nextCursor = payload.next_cursor || null;
      renderWorkspaceHive(workspaceViewData(), refreshBootstrap, hivePrefs());
      if (workspaceCatalogStatus) workspaceCatalogStatus.textContent = '';
    } catch (error) {
      if (generation === catalogState.generation && workspaceCatalogStatus) workspaceCatalogStatus.textContent = error.message;
    } finally {
      if (generation !== catalogState.generation) return;
      catalogState.loading = false;
      if (workspaceLoadMore) workspaceLoadMore.disabled = false;
    }
  };

  const refreshBootstrap = async () => {
    const selectedValue = select.value;
    bootstrap = await loadBootstrap();
    renderCurrentUser(bootstrap.identity || {});
    let namedCatalog = {
      items: bootstrap.existing_workspaces || [],
      next_cursor: null,
    };
    if (bootstrap.bootstrap_sections?.workspace_catalog !== 'unavailable') {
      try {
        namedCatalog = await fetchCatalogPage({ kind: 'named' });
      } catch (_error) {
        bootstrap.bootstrap_sections ||= {};
        bootstrap.bootstrap_sections.workspace_catalog = 'unavailable';
      }
    }
    bootstrap.existing_workspaces = (namedCatalog.items || []).map(decorateCatalogWorkspace);
    renderWorkspaceOptions(select, bootstrap, selectedValue);
    renderLaunchProviderAccounts(
      providerAccount,
      providerAccountPolicy,
      providerAccountHelp,
      bootstrap,
      select.value,
    );
    syncPreferenceControls(bootstrap, { defaultWorker, codexEffort, claudeEffort, openclawEffort });
    if (
      String(workspaceKindFilter?.value || 'named') === 'named'
      && !String(workspaceSearch?.value || '').trim()
      && !String(workspaceTagFilter?.value || '').trim()
    ) {
      catalogState.items = [...bootstrap.existing_workspaces];
      catalogState.nextCursor = namedCatalog.next_cursor || null;
      renderWorkspaceHive(workspaceViewData(), refreshBootstrap, hivePrefs());
    } else {
      await refreshWorkspaceCatalog();
    }
    renderActivity(
      bootstrap.activity || bootstrap.existing_workspaces || [],
      bootstrap.bootstrap_sections?.activity || 'ready',
    );
    const degradedSections = Object.entries(bootstrap.bootstrap_sections || {})
      .filter(([, value]) => value !== 'ready')
      .map(([name]) => name.replaceAll('_', ' '));
    if (degradedSections.length && status) {
      status.textContent = `Some personal data is temporarily unavailable (${degradedSections.join(', ')}). Refresh to retry; empty lists do not mean your data was removed.`;
    }
    if (bootstrap.bootstrap_sections?.provider_accounts !== 'ready' && providerAccountHelp) {
      providerAccountHelp.textContent = 'Personal worker accounts could not be loaded. Refresh before choosing an account.';
    }
    if (bootstrap.bootstrap_sections?.workspace_templates !== 'ready' && templateStartStatus) {
      templateStartStatus.textContent = 'Saved templates could not be loaded. Refresh to retry.';
    }
    if (bootstrap.bootstrap_sections?.workspace_catalog !== 'ready' && workspaceCatalogStatus) {
      workspaceCatalogStatus.textContent = 'Your workspaces are temporarily unavailable. Refresh to retry; this does not mean they were removed.';
    }
    syncWorkspaceUI(select, bootstrap, button, help);
    return bootstrap;
  };

  function stopHivePolling() {
    if (!hivePollTimer) return;
    window.clearInterval(hivePollTimer);
    hivePollTimer = 0;
  }

  switchAccount?.addEventListener('click', async () => {
    switchAccount.disabled = true;
    try {
      await signOut('provider');
    } catch (error) {
      switchAccount.disabled = false;
      window.alert(error.message || 'GlassHive could not switch accounts.');
    }
  });

  localSignOut?.addEventListener('click', async () => {
    localSignOut.disabled = true;
    try {
      await signOut('local');
    } catch (error) {
      localSignOut.disabled = false;
      window.alert(error.message || 'GlassHive could not sign out.');
    }
  });

  function startHivePolling() {
    stopHivePolling();
    hivePollTimer = window.setInterval(() => {
      if (activeView === 'workspaces') refreshVisibleWorkspaceTiles(refreshBootstrap).catch(() => {});
    }, ACTIVE_TILE_REFRESH_MS);
  }

  function setActiveView(view, { updateHash = true } = {}) {
    activeView = viewRegistry.has(view) ? view : 'project';
    frame.dataset.activeView = activeView;
    for (const [name, panel] of viewRegistry.entries()) {
      if (panel) panel.hidden = name !== activeView;
    }
    for (const tab of tabs) {
      const selected = tab.dataset.viewTab === activeView;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    if (activeView === 'workspaces') {
      if (bootstrap) renderWorkspaceHive(workspaceViewData(), refreshBootstrap, hivePrefs());
      startHivePolling();
    } else {
      stopHivePolling();
    }
    if (activeView === 'connections' || activeView === 'library' || activeView === 'schedules') {
      refreshControlPlane().catch((error) => {
        const statusNode = document.getElementById('provider-account-status');
        if (statusNode) statusNode.textContent = error.message;
      });
    }
    if (updateHash) {
      const nextUrl = `${window.location.pathname}${window.location.search}${activeView === 'project' ? '' : `#${activeView}`}`;
      window.history.replaceState(null, '', nextUrl);
    }
  }

  window.addEventListener('glasshive:control-plane-updated', () => {
    refreshBootstrap().catch(() => {});
  });
  initializeControlPlane({ withAuth, postJson, patchJson, deleteJson, responseMessage, setView: setActiveView });

  try {
    await refreshBootstrap();
    if (launchSurface) launchSurface.value = String(bootstrap.default_launch_surface || 'desktop');
    if (workspaceType) renderWorkspaceTypeOptions(workspaceType, workspaceTypeHelp, bootstrap);
  } catch (error) {
    button.disabled = true;
    if (scheduleButton) scheduleButton.disabled = true;
    select.disabled = true;
    if (workspaceType) workspaceType.disabled = true;
    if (providerAccount) providerAccount.disabled = true;
    if (providerAccountPolicy) providerAccountPolicy.disabled = true;
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
      renderWorkspaceHive(workspaceViewData(), refreshBootstrap, hivePrefs());
    });
  }

  workspaceSearch?.addEventListener('input', () => {
    window.clearTimeout(catalogSearchTimer);
    catalogSearchTimer = window.setTimeout(() => refreshWorkspaceCatalog(), 250);
  });

  workspaceTagFilter?.addEventListener('input', () => {
    window.clearTimeout(catalogSearchTimer);
    catalogSearchTimer = window.setTimeout(() => refreshWorkspaceCatalog(), 250);
  });

  workspaceKindFilter?.addEventListener('change', () => refreshWorkspaceCatalog());
  workspaceLoadMore?.addEventListener('click', () => refreshWorkspaceCatalog({ append: true }));

  select.addEventListener('change', () => {
    if (!bootstrap) return;
    syncWorkspaceUI(select, bootstrap, button, help);
    renderLaunchProviderAccounts(
      providerAccount,
      providerAccountPolicy,
      providerAccountHelp,
      bootstrap,
      select.value,
    );
  });

  providerAccountPolicy?.addEventListener('change', () => {
    if (!bootstrap) return;
    renderLaunchProviderAccounts(
      providerAccount,
      providerAccountPolicy,
      providerAccountHelp,
      bootstrap,
      select.value,
    );
  });

  workspaceType?.addEventListener('change', () => {
    const selected = workspaceType.selectedOptions?.[0];
    if (workspaceTypeHelp) {
      workspaceTypeHelp.textContent = selected?.dataset.description || 'Runs on managed GlassHive workspace compute.';
    }
    if (bootstrap) syncWorkspaceUI(select, bootstrap, button, help);
  });

  renameDialog?.addEventListener('close', () => {
    renameWorkspaceContext = null;
    if (renameStatus) renameStatus.textContent = '';
  });
  cancelRename?.addEventListener('click', () => renameDialog?.close());

  renameForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const context = renameWorkspaceContext;
    const name = String(renameInput?.value || '').trim();
    const tags = String(renameTags?.value || '')
      .split(',')
      .map((tag) => tag.trim())
      .filter(Boolean);
    if (!context || !name) {
      if (renameStatus) renameStatus.textContent = 'Enter a workspace name.';
      renameInput?.focus();
      return;
    }
    const submit = renameForm.querySelector('button[type="submit"]');
    if (submit) submit.disabled = true;
    if (renameStatus) renameStatus.textContent = 'Saving…';
    try {
      await postJson(`/api/worker/${encodeURIComponent(context.workerId)}/metadata`, { name, tags });
      await context.refreshBootstrap();
      renameDialog.close();
    } catch (error) {
      if (renameStatus) renameStatus.textContent = error.message;
    } finally {
      if (submit) submit.disabled = false;
    }
  });

  saveTemplateDialog?.addEventListener('close', () => {
    saveTemplateContext = null;
    if (saveTemplateStatus) saveTemplateStatus.textContent = '';
  });
  cancelSaveTemplate?.addEventListener('click', () => saveTemplateDialog?.close());
  saveTemplateForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const context = saveTemplateContext;
    const name = String(saveTemplateName?.value || '').trim();
    if (!context || !name) {
      if (saveTemplateStatus) saveTemplateStatus.textContent = 'Enter a template name.';
      saveTemplateName?.focus();
      return;
    }
    const submit = saveTemplateForm.querySelector('button[type="submit"]');
    if (submit) submit.disabled = true;
    if (saveTemplateStatus) saveTemplateStatus.textContent = 'Saving immutable template…';
    try {
      await postJson(`/api/workspaces/${encodeURIComponent(context.workerId)}/templates`, {
        name,
        description: String(saveTemplateDescription?.value || '').trim(),
      });
      await context.refreshBootstrap();
      saveTemplateDialog.close();
      document.getElementById('workspace-template-panel')?.setAttribute('open', '');
      if (templateStartStatus) templateStartStatus.textContent = 'Template saved. It is ready to create a fresh workspace.';
    } catch (error) {
      if (saveTemplateStatus) saveTemplateStatus.textContent = error.message;
    } finally {
      if (submit) submit.disabled = false;
    }
  });

  const resetTemplateIdempotency = () => {
    if (templateStartButton) delete templateStartButton.dataset.idempotencyKey;
    if (bootstrap) renderWorkspaceTemplateOptions(bootstrap);
  };
  templateSelect?.addEventListener('change', resetTemplateIdempotency);
  templateInstanceName?.addEventListener('input', resetTemplateIdempotency);
  templateStartForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const templateId = String(templateSelect?.value || '');
    if (!templateId || !templateStartButton) {
      if (templateStartStatus) templateStartStatus.textContent = 'Choose a saved template first.';
      templateSelect?.focus();
      return;
    }
    templateStartButton.dataset.idempotencyKey ||= globalThis.crypto?.randomUUID?.()
      || `template-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    templateStartButton.disabled = true;
    templateStartButton.textContent = 'Creating…';
    if (templateStartStatus) templateStartStatus.textContent = 'Creating a fresh paused workspace…';
    try {
      const payload = await postJson(
        `/api/workspace-templates/${encodeURIComponent(templateId)}/instantiate`,
        {
          idempotency_key: templateStartButton.dataset.idempotencyKey,
          ...(String(templateInstanceName?.value || '').trim()
            ? { name: String(templateInstanceName.value).trim() }
            : {}),
        },
      );
      delete templateStartButton.dataset.idempotencyKey;
      if (templateInstanceName) templateInstanceName.value = '';
      await refreshBootstrap();
      const approvals = Array.isArray(payload.approvals_required) ? payload.approvals_required.length : 0;
      if (templateStartStatus) templateStartStatus.textContent = approvals
        ? `Workspace created and paused. ${approvals} Library approval${approvals === 1 ? '' : 's'} must be reviewed before use. Turn on Inactive Workspaces to find it.`
        : 'Workspace created and paused. Turn on Inactive Workspaces to find it.';
    } catch (error) {
      if (templateStartStatus) templateStartStatus.textContent = `${error.message} Retry will use the same request key.`;
    } finally {
      templateStartButton.disabled = !Array.isArray(bootstrap?.workspace_templates) || !bootstrap.workspace_templates.length;
      templateStartButton.textContent = 'Start workspace';
    }
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
      provider_account_policy: select.value.startsWith('new:') ? providerAccountPolicy?.value || null : null,
      provider_account_id: select.value.startsWith('new:') && providerAccountPolicy?.value !== 'legacy'
        ? providerAccount?.value || null
        : null,
      files,
    };
    try {
      const response = await fetch(withAuth('/api/launch'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(csrfToken ? { 'X-GlassHive-CSRF': csrfToken } : {}),
        },
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

  setActiveView(window.location.hash.replace(/^#/, '') || 'project', { updateHash: false });
  window.addEventListener('hashchange', () => {
    setActiveView(window.location.hash.replace(/^#/, '') || 'project', { updateHash: false });
  });
}

main();
