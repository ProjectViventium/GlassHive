const SUPPORT_COPY = {
  supported: 'Ready to connect',
  proof_required: 'Available when this deployment enables isolated Codex subscription homes',
  provider_permission_required: 'Requires an approved hosted Claude authentication agreement',
  unsupported_macos_host: 'Claude subscription isolation is not available for multi-user macOS host workers',
  isolated_substrate_required: 'This multi-user deployment has not enabled a dedicated worker isolation substrate. Use the managed connected-accounts route below.',
  secret_store_required: 'Requires this deployment\'s secure secret store',
  managed_connection_required: 'Connect this account through the deployment\'s managed connected-accounts page.',
};

let api = null;
let controlPlane = null;
let connectAi = null;
let workspaceCatalog = { items: [] };
let recurringSchedules = { items: [] };
let scheduleLoadError = '';
const scheduleOccurrences = new Map();
let editingScheduleId = '';
let activeSetupAccount = '';
let setupPollTimer = 0;
const copyResetTimers = new WeakMap();

function node(tag, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function emptyList(message) {
  const item = node('div', 'empty-state-compact');
  item.append(node('strong', '', message), node('span', '', 'Nothing is shared globally or across users.'));
  return item;
}

function statusChip(status) {
  const value = String(status || 'not connected').replaceAll('_', ' ');
  const chip = node('span', 'status-chip', value);
  chip.dataset.status = String(status || 'unknown');
  return chip;
}

function providerTimestamp(label, value) {
  const timestamp = Number(value || 0);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '';
  return `${label} ${new Date(timestamp * 1000).toLocaleString()}`;
}

function observedDuration(value) {
  const seconds = Math.max(0, Number(value || 0));
  if (!Number.isFinite(seconds)) return 'duration unavailable';
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h worker time`;
  if (seconds >= 60) return `${(seconds / 60).toFixed(1)}m worker time`;
  return `${seconds.toFixed(seconds >= 10 ? 0 : 1)}s worker time`;
}

function providerAccountDetails(account) {
  const provider = String(account.provider || 'AI').replaceAll('_', ' ');
  const method = String(account.auth_method || 'account').replaceAll('_', ' ');
  return `${provider} · ${method}`;
}

function providerAccountUsage(account) {
  const details = [
    providerTimestamp('Verified', account.last_verified_at),
    providerTimestamp('Last used', account.last_used_at),
  ];
  const observedRuns = Number(account.observed_runs || 0);
  if (observedRuns > 0) {
    const observedFailures = Math.max(0, Number(account.observed_failures || 0));
    details.push(
      `Observed by GlassHive: ${observedRuns} run${observedRuns === 1 ? '' : 's'} · `
      + `${observedFailures} failed · ${observedDuration(account.observed_duration_seconds)}`,
    );
  }
  const hasReportedTokens = account.observed_input_tokens != null || account.observed_output_tokens != null;
  if (hasReportedTokens) {
    const inputTokens = Math.max(0, Number(account.observed_input_tokens || 0));
    const outputTokens = Math.max(0, Number(account.observed_output_tokens || 0));
    details.push(`Tokens reported by worker: ${inputTokens.toLocaleString()} input · ${outputTokens.toLocaleString()} output`);
  }
  return details.filter(Boolean).join(' · ');
}

async function copyText(value, button, fallbackNode = null) {
  const text = String(value || '');
  if (!text) return false;
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.className = 'clipboard-fallback';
    document.body.append(textarea);
    textarea.select();
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
    textarea.remove();
  }
  if (!copied && fallbackNode) {
    const range = document.createRange();
    range.selectNodeContents(fallbackNode);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
  }
  if (button) {
    const previous = button.dataset.copyLabel || button.textContent;
    button.dataset.copyLabel = previous;
    const existingTimer = copyResetTimers.get(button);
    if (existingTimer) window.clearTimeout(existingTimer);
    button.textContent = copied ? 'Copied' : 'Selected';
    copyResetTimers.set(button, window.setTimeout(() => {
      button.textContent = previous;
      copyResetTimers.delete(button);
    }, 1800));
  }
  return copied;
}

function setProviderStatus(message = '') {
  const status = document.getElementById('provider-account-status');
  if (!status) return;
  status.textContent = String(message || '');
}

async function reconnectProviderAccount(account, button) {
  const accountId = encodeURIComponent(String(account.account_id || ''));
  button.disabled = true;
  button.textContent = 'Checking…';
  try {
    if (String(account.auth_method || '') === 'subscription') {
      const setup = await api.postJson(`/api/provider-accounts/${accountId}/setup`);
      activeSetupAccount = String(account.account_id || '');
      showSetup(setup);
      stopSetupPolling();
      setupPollTimer = window.setTimeout(pollSetup, 900);
    } else {
      const result = await api.postJson(`/api/provider-accounts/${accountId}/verify`);
      showSetup(result);
      await loadControlPlane();
      showSetup(result);
    }
  } catch (error) {
    setProviderStatus(error.message);
    button.disabled = false;
    button.textContent = String(account.auth_method || '') === 'subscription' ? 'Reconnect' : 'Reconnect & test';
  }
}

async function verifyProviderAccount(account, button) {
  button.disabled = true;
  button.textContent = 'Testing…';
  try {
    const result = await api.postJson(`/api/provider-accounts/${encodeURIComponent(String(account.account_id || ''))}/verify`);
    showSetup(result);
    await loadControlPlane();
    showSetup(result);
  } catch (error) {
    setProviderStatus(error.message);
    button.disabled = false;
    button.textContent = 'Test connection';
  }
}

function renderProviderAccounts() {
  const list = document.getElementById('provider-account-list');
  if (!list || !controlPlane) return;
  const accounts = controlPlane.provider_accounts || [];
  const addAccount = document.querySelector('#add-provider-account > summary');
  if (addAccount) addAccount.textContent = accounts.length ? 'Connect another account' : 'Connect an AI account';
  if (!accounts.length) {
    list.replaceChildren(node('p', 'connections-empty', 'No AI account connected yet.'));
    return;
  }
  const rows = accounts.map((account) => {
    const row = node('article', 'connection-row');
    row.dataset.accountId = String(account.account_id || '');
    const copy = node('div', 'connection-copy');
    copy.append(
      node('strong', '', String(account.label || account.provider || 'Worker account')),
      node('span', '', providerAccountDetails(account)),
    );
    if (account.reconnect_reason && account.status !== 'ready') {
      copy.append(node('span', 'connection-recovery', String(account.reconnect_reason)));
    }
    const actions = node('div', 'connection-actions connection-actions-primary');
    const brokerBacked = ['api_key', 'enterprise_route'].includes(String(account.auth_method || ''));
    actions.append(statusChip(
      brokerBacked && account.status === 'ready'
        ? 'linked · verifies on run'
        : (account.status || account.platform_support),
    ));
    if (account.status === 'connecting' && String(account.auth_method || '') === 'subscription') {
      const continueSetup = node('button', 'quiet-button', 'Continue setup');
      continueSetup.type = 'button';
      continueSetup.addEventListener('click', () => {
        activeSetupAccount = String(account.account_id || '');
        continueSetup.disabled = true;
        pollSetup();
      });
      actions.append(continueSetup);
    } else if (['disconnected', 'action_required', 'error', 'unavailable'].includes(String(account.status || ''))) {
      const reconnectLabel = String(account.auth_method || '') === 'subscription' ? 'Reconnect' : 'Reconnect & test';
      const reconnect = node('button', 'quiet-button', reconnectLabel);
      reconnect.type = 'button';
      reconnect.addEventListener('click', () => reconnectProviderAccount(account, reconnect));
      actions.append(reconnect);
    }
    if (brokerBacked && controlPlane?.manage_connections_url) {
      const manage = node('a', 'quiet-button', 'Manage account');
      manage.href = String(controlPlane.manage_connections_url);
      actions.append(manage);
    }
    const more = node('details', 'connection-more');
    const moreSummary = node('summary', '', 'More');
    moreSummary.setAttribute('aria-label', `More actions for ${String(account.label || account.provider || 'account')}`);
    const moreActions = node('div', 'connection-more-actions');
    const usage = providerAccountUsage(account);
    if (usage) moreActions.append(node('span', 'connection-more-note', usage));
    if (account.status === 'ready') {
      const test = node('button', 'text-button', 'Test connection');
      test.type = 'button';
      test.addEventListener('click', () => verifyProviderAccount(account, test));
      moreActions.append(test);
    }
    if (account.status !== 'disconnected') {
      const disconnect = node('button', 'text-button', 'Disconnect');
      disconnect.type = 'button';
      disconnect.addEventListener('click', async () => {
        const question = brokerBacked
          ? `Remove ${String(account.label || account.provider || 'this account')} from GlassHive? This does not delete the key or route in connected accounts.`
          : `Disconnect ${String(account.label || account.provider || 'this account')} and remove its isolated credentials?`;
        if (!window.confirm(question)) return;
        disconnect.disabled = true;
        disconnect.textContent = 'Disconnecting…';
        try {
          await api.postJson(`/api/provider-accounts/${encodeURIComponent(String(account.account_id || ''))}/disconnect`);
          await loadControlPlane();
        } catch (error) {
          disconnect.textContent = error.message;
          disconnect.disabled = false;
        }
      });
      moreActions.append(disconnect);
    }
    if (account.status === 'disconnected') {
      const forget = node('button', 'text-button danger-text-button', 'Forget');
      forget.type = 'button';
      forget.addEventListener('click', async () => {
        if (!window.confirm(`Forget ${String(account.label || account.provider || 'this account')} from GlassHive? Its disconnected metadata will be removed.`)) return;
        forget.disabled = true;
        forget.textContent = 'Forgetting…';
        try {
          await api.deleteJson(`/api/provider-accounts/${encodeURIComponent(String(account.account_id || ''))}`);
          await loadControlPlane();
        } catch (error) {
          setProviderStatus(error.message);
          forget.disabled = false;
          forget.textContent = 'Forget';
        }
      });
      moreActions.append(forget);
    }
    if (moreActions.childElementCount) {
      more.append(moreSummary, moreActions);
      actions.append(more);
    }
    row.append(copy, actions);
    return row;
  });
  list.replaceChildren(...rows);
}

function renderConnections() {
  const list = document.getElementById('data-connection-list');
  const manage = document.getElementById('manage-connections-link');
  const card = document.getElementById('connected-services-card');
  if (!list || !controlPlane) return;
  const connections = controlPlane.connections || [];
  if (!connections.length) {
    list.replaceChildren();
  } else {
    list.replaceChildren(...connections.map((connection) => {
      const row = node('article', 'connection-row');
      const copy = node('div', 'connection-copy');
      copy.append(
        node('strong', '', String(connection.label || connection.kind || 'Connected service')),
        node('span', '', String(connection.kind || connection.adapter || 'user-scoped connection').replaceAll('_', ' ')),
      );
      row.append(copy, statusChip(connection.status));
      return row;
    }));
  }
  const manageUrl = String(controlPlane?.manage_connections_url || '');
  if (card) card.hidden = !connections.length && !manageUrl;
  if (manage && manageUrl) {
    manage.href = manageUrl;
    manage.hidden = false;
  } else if (manage) {
    manage.hidden = true;
  }
}

function commandRow(label, command) {
  const row = node('article', 'command-row');
  const copy = node('div', 'command-copy');
  copy.append(node('strong', '', label), node('code', '', command));
  const button = node('button', 'quiet-button', 'Copy');
  button.type = 'button';
  button.addEventListener('click', () => copyText(command, button, copy.querySelector('code')));
  row.append(copy, button);
  return row;
}

function referenceRow(label, value) {
  const row = node('article', 'command-row');
  const copy = node('div', 'command-copy');
  copy.append(node('strong', '', label), node('code', '', value));
  row.append(copy, node('span', 'card-note', 'Registration reference'));
  return row;
}

function renderConnectAi() {
  const list = document.getElementById('connect-ai-commands');
  const source = document.getElementById('connect-ai-source');
  if (!list || !connectAi) return;
  const clients = connectAi.clients || {};
  const rows = [
    referenceRow('Codex · Registered callback', String(clients.codex?.callback_uri || '')),
    commandRow('Codex · 1. Add server', String(clients.codex?.add_command || '')),
    commandRow('Codex · 2. Sign in', String(clients.codex?.login_command || '')),
    referenceRow('Claude Code · Registered callback', String(clients.claude?.callback_uri || '')),
    commandRow('Claude Code · Add server', String(clients.claude?.add_command || '')),
  ].filter((row) => row.querySelector('code')?.textContent);
  rows.push(node('p', 'connect-login-note', String(connectAi.configuration_note || '')));
  if (clients.claude?.login_note) {
    rows.push(node('p', 'connect-login-note', String(clients.claude.login_note)));
  }
  if (connectAi.documentation_url) {
    const docs = node('a', 'connect-login-note', 'Client registration instructions');
    docs.href = String(connectAi.documentation_url);
    docs.target = '_blank';
    docs.rel = 'noreferrer';
    rows.push(docs);
  }
  list.replaceChildren(...rows);
  if (source) {
    const sourceLink = node('a', '', `${connectAi.source?.label || 'Source available'} · ${connectAi.source?.license || ''}`);
    sourceLink.href = String(connectAi.source?.repository_url || '#');
    sourceLink.target = '_blank';
    sourceLink.rel = 'noreferrer';
    source.replaceChildren(sourceLink);
  }
}

function compareLibraryVersions(left, right) {
  const parse = (value) => {
    const [main, prerelease = ''] = String(value || '0.0.0').split('-', 2);
    return { numbers: main.split('.').slice(0, 3).map((part) => Number(part) || 0), prerelease };
  };
  const a = parse(left);
  const b = parse(right);
  for (let index = 0; index < 3; index += 1) {
    if (a.numbers[index] !== b.numbers[index]) return a.numbers[index] > b.numbers[index] ? 1 : -1;
  }
  if (a.prerelease === b.prerelease) return 0;
  if (!a.prerelease) return 1;
  if (!b.prerelease) return -1;
  return a.prerelease.localeCompare(b.prerelease);
}

function renderLibrary() {
  const list = document.getElementById('library-list');
  const empty = document.getElementById('library-empty');
  if (!list || !controlPlane) return;
  const items = controlPlane.library || [];
  const libraryById = new Map(items.map((libraryItem) => [String(libraryItem.library_id || ''), libraryItem]));
  if (empty) empty.hidden = items.length > 0;
  const cards = items.map((item) => {
    const manifest = item.manifest || {};
    const activatable = manifest.activatable === true && item.activation_status === 'ready';
    const capabilityName = String(manifest.label || manifest.name || item.stable_id || 'Approved capability');
    const card = node('article', 'library-card');
    const head = node('div', 'library-card-head');
    head.append(node('span', 'library-kind', String(manifest.kind || 'Capability')), statusChip(item.status || 'available'));
    card.append(
      head,
      node('h2', '', capabilityName),
      node('p', '', String(manifest.description || 'An approved, versioned capability for compatible workspaces.')),
    );
    const meta = node('div', 'library-meta');
    meta.append(
      node('span', '', `Version ${String(item.version || '1')}`),
      node('span', '', (item.supported_profiles || []).join(', ') || 'Compatible workers'),
      node('span', '', `Permissions: ${(item.scopes || []).join(', ') || 'none requested'}`),
    );
    card.append(meta);
    const grant = node('div', 'library-grant');
    const select = node('select');
    select.setAttribute('aria-label', `Workspace for ${capabilityName}`);
    const placeholder = node(
      'option',
      '',
      workspaceCatalog.next_cursor ? 'Choose recent workspace · search Workspaces for more' : 'Choose workspace',
    );
    placeholder.value = '';
    select.appendChild(placeholder);
    for (const workspace of workspaceCatalog.items || []) {
      const option = node('option', '', String(workspace.name || workspace.title || workspace.worker_id || 'Workspace'));
      option.value = String(workspace.worker_id || '');
      select.appendChild(option);
    }
    const button = node('button', 'card-action', activatable ? 'Add to workspace' : 'Unavailable in this deployment');
    button.type = 'button';
    button.disabled = true;
    let activeGrant = null;
    let replacementGrant = null;
    const syncGrant = async () => {
      activeGrant = null;
      replacementGrant = null;
      if (!activatable || !select.value) {
        button.disabled = true;
        button.textContent = activatable ? 'Add to workspace' : 'Unavailable in this deployment';
        return;
      }
      button.disabled = true;
      button.textContent = 'Checking workspace…';
      try {
        const response = await fetch(api.withAuth(`/api/workspaces/${encodeURIComponent(select.value)}/capability-grants`));
        if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load workspace capabilities'));
        const payload = await response.json();
        const grants = payload.items || [];
        activeGrant = grants.find((grantItem) => grantItem.library_id === item.library_id) || null;
        replacementGrant = grants.find((grantItem) => {
          const installed = libraryById.get(String(grantItem.library_id || ''));
          return installed && installed.stable_id === item.stable_id && installed.library_id !== item.library_id;
        }) || null;
        if (activeGrant) {
          button.textContent = 'Remove from workspace';
          button.disabled = false;
        } else if (replacementGrant) {
          const installed = libraryById.get(String(replacementGrant.library_id || ''));
          const newer = compareLibraryVersions(item.version, installed?.version) > 0;
          button.textContent = newer ? 'Upgrade workspace' : 'Newer version already active';
          button.disabled = !newer;
        } else {
          button.textContent = 'Add to workspace';
          button.disabled = false;
        }
      } catch (error) {
        button.textContent = error.message;
      }
    };
    select.addEventListener('change', syncGrant);
    button.addEventListener('click', async () => {
      if (!select.value) {
        select.focus();
        return;
      }
      button.disabled = true;
      button.textContent = activeGrant ? 'Removing…' : 'Preparing review…';
      try {
        if (activeGrant) {
          if (!window.confirm(`Remove ${capabilityName} from ${workspaceName(select.value)}?`)) {
            button.disabled = false;
            button.textContent = 'Remove from workspace';
            return;
          }
          await api.deleteJson(
            `/api/workspaces/${encodeURIComponent(select.value)}/capability-grants/${encodeURIComponent(String(activeGrant.grant_id || ''))}`,
          );
          await syncGrant();
          return;
        }
        const requestedScopes = replacementGrant
          ? (item.scopes || []).filter((scope) => (replacementGrant.scopes || []).includes(scope))
          : (item.scopes || []);
        const pending = await api.postJson('/api/pending-changes', {
          change_type: replacementGrant ? 'library_upgrade' : 'library_enable',
          target_id: select.value,
          payload: {
            library_id: item.library_id,
            scopes: requestedScopes,
            ...(replacementGrant ? { replaces_grant_id: replacementGrant.grant_id } : {}),
          },
        });
        const changeId = encodeURIComponent(String(pending.change_id || ''));
        const token = encodeURIComponent(String(pending.confirmation_token || ''));
        if (!changeId || !token) throw new Error('GlassHive could not prepare the confirmation.');
        window.location.assign(`/confirm-change#change_id=${changeId}&token=${token}`);
      } catch (error) {
        button.textContent = error.message;
        window.setTimeout(() => {
          syncGrant();
        }, 2200);
      }
    });
    grant.append(select, button);
    card.append(grant);
    return card;
  });
  list.replaceChildren(...cards);
}

function renderLibraryRequestWorkspaceOptions() {
  const select = document.getElementById('library-request-workspace');
  if (!select) return;
  const selected = select.value;
  const workspaces = workspaceCatalog.items || [];
  const placeholder = node('option', '', workspaces.length
    ? (workspaceCatalog.next_cursor ? 'Choose recent workspace · search Workspaces for more' : 'Choose a saved workspace')
    : 'Create a saved workspace first');
  placeholder.value = '';
  const options = workspaces.map((workspace) => {
    const option = node('option', '', String(workspace.name || workspace.title || workspace.worker_id || 'Saved workspace'));
    option.value = String(workspace.worker_id || '');
    return option;
  });
  select.replaceChildren(placeholder, ...options);
  if (options.some((option) => option.value === selected)) select.value = selected;
  select.disabled = !options.length;
}

async function submitLibraryRequest(event) {
  event.preventDefault();
  const workspaceId = document.getElementById('library-request-workspace')?.value || '';
  const requestText = document.getElementById('library-request-text')?.value.trim() || '';
  const status = document.getElementById('library-request-status');
  if (!workspaceId) {
    if (status) status.textContent = 'Choose a saved workspace.';
    document.getElementById('library-request-workspace')?.focus();
    return;
  }
  if (!requestText) {
    if (status) status.textContent = 'Describe the capability or paste its source.';
    document.getElementById('library-request-text')?.focus();
    return;
  }
  if (status) status.textContent = 'Sending your request to the workspace…';
  try {
    await api.postJson(`/api/workspace/${encodeURIComponent(workspaceId)}/message`, {
      message: requestText,
    });
    if (status) status.textContent = 'Setup request started. Opening the workspace so you can review progress and finish any sign-in.';
    const workspace = (workspaceCatalog.items || []).find((item) => String(item.worker_id || '') === workspaceId);
    const watchUrl = String(workspace?.watch_url || api.withAuth(`/watch/${encodeURIComponent(workspaceId)}?surface=desktop`));
    window.location.assign(watchUrl);
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}

function workspaceName(workerId) {
  const workspace = (workspaceCatalog.items || []).find((item) => String(item.worker_id || '') === String(workerId || ''));
  return String(workspace?.name || workspace?.title || workerId || 'Saved workspace');
}

function formatDateTime(value) {
  if (!value) return 'Not scheduled';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed);
}

function intervalLabel(seconds) {
  const value = Number(seconds || 0);
  if (value > 0 && value % 86400 === 0) {
    const days = value / 86400;
    return `Every ${days} day${days === 1 ? '' : 's'}`;
  }
  if (value > 0 && value % 3600 === 0) {
    const hours = value / 3600;
    return `Every ${hours} hour${hours === 1 ? '' : 's'}`;
  }
  return `Every ${value} seconds`;
}

function recurrenceLabel(schedule) {
  const kind = String(schedule.recurrence_type || 'interval');
  if (kind === 'once') return `Once · ${formatDateTime(schedule.next_occurrence_at || schedule.next_run_at)}`;
  if (kind === 'daily') {
    return `Daily at ${schedule.local_time || '—'} · ${schedule.timezone_name || 'UTC'}`;
  }
  if (kind === 'cron') return `Cron ${schedule.cron_expression || '—'} · ${schedule.timezone_name || 'UTC'}`;
  if (kind === 'rfc5545') return `RFC 5545 · ${schedule.timezone_name || 'UTC'}`;
  return intervalLabel(schedule.interval_seconds);
}

function renderScheduleWorkspaceOptions() {
  const select = document.getElementById('recurring-schedule-workspace');
  if (!select) return;
  const selected = select.value;
  const placeholder = node('option', '', (workspaceCatalog.items || []).length
    ? (workspaceCatalog.next_cursor ? 'Choose recent workspace · search Workspaces for more' : 'Choose a saved workspace')
    : 'Create a saved workspace first');
  placeholder.value = '';
  const options = (workspaceCatalog.items || []).map((workspace) => {
    const option = node('option', '', String(workspace.name || workspace.title || workspace.worker_id || 'Saved workspace'));
    option.value = String(workspace.worker_id || '');
    return option;
  });
  select.replaceChildren(placeholder, ...options);
  if (options.some((option) => option.value === selected)) select.value = selected;
  select.disabled = !options.length;
}

function renderOccurrenceHistory(container, items) {
  if (!items.length) {
    container.replaceChildren(emptyList('No occurrence has run yet'));
    return;
  }
  container.replaceChildren(...items.map((occurrence) => {
    const row = node('div', 'schedule-history-row');
    const outcome = String(occurrence.outcome && occurrence.outcome !== 'pending'
      ? occurrence.outcome
      : (occurrence.state || 'pending'));
    row.append(
      node('span', '', formatDateTime(occurrence.scheduled_for)),
      statusChip(outcome),
    );
    const error = String(occurrence.last_error || '').trim();
    if (error) {
      const recovery = ['action_required', 'failed'].includes(outcome)
        ? 'Reconnect the affected account or connection, then use Run now or resume this schedule.'
        : 'Open the workspace status for details before retrying.';
      row.append(node('span', 'schedule-history-detail', `${error} ${recovery}`));
    }
    return row;
  }));
}

async function toggleOccurrenceHistory(definitionId, container, button) {
  if (!container.hidden) {
    container.hidden = true;
    button.textContent = 'View history';
    return;
  }
  container.hidden = false;
  button.textContent = 'Hide history';
  if (scheduleOccurrences.has(definitionId)) {
    renderOccurrenceHistory(container, scheduleOccurrences.get(definitionId));
    return;
  }
  container.replaceChildren(node('span', 'schedule-card-meta', 'Loading occurrence history…'));
  try {
    const response = await fetch(api.withAuth(`/api/recurring-schedules/${encodeURIComponent(definitionId)}/occurrences?limit=20`));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load occurrence history'));
    const payload = await response.json();
    const items = payload.items || [];
    scheduleOccurrences.set(definitionId, items);
    renderOccurrenceHistory(container, items);
  } catch (error) {
    container.replaceChildren(emptyList(error.message));
  }
}

async function deactivateSchedule(schedule, button) {
  const name = workspaceName(schedule.worker_id);
  if (!window.confirm(`Pause future occurrences for ${name}? You can resume it later and its history will be kept.`)) return;
  button.disabled = true;
  button.textContent = 'Pausing…';
  const status = document.getElementById('recurring-schedule-status');
  try {
    await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`, { enabled: false });
    if (status) status.textContent = 'Schedule paused. Its previous occurrences are still available.';
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = error.message;
    button.disabled = false;
    button.textContent = 'Pause';
  }
}

async function resumeSchedule(schedule, button) {
  button.disabled = true;
  button.textContent = 'Resuming…';
  const status = document.getElementById('recurring-schedule-status');
  try {
    await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`, { enabled: true });
    if (status) status.textContent = 'Schedule resumed with the same owner and immutable history.';
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = error.message;
    button.disabled = false;
    button.textContent = 'Resume schedule';
  }
}

function datetimeLocalValue(value) {
  const match = String(value || '').match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/);
  return match ? match[1] : '';
}

function resetScheduleEditor(message = '') {
  editingScheduleId = '';
  const form = document.getElementById('recurring-schedule-form');
  form?.reset();
  const timezone = document.getElementById('recurring-schedule-timezone');
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (timezone && browserTimezone) timezone.value = browserTimezone;
  const workspace = document.getElementById('recurring-schedule-workspace');
  if (workspace) workspace.disabled = !(workspaceCatalog.items || []).length;
  const submit = document.getElementById('recurring-schedule-submit');
  if (submit) submit.textContent = 'Create schedule';
  const cancel = document.getElementById('recurring-schedule-cancel-edit');
  if (cancel) cancel.hidden = true;
  const title = document.getElementById('recurring-schedule-form-title');
  if (title) title.textContent = 'Create recurring work';
  const status = document.getElementById('recurring-schedule-status');
  if (status) status.textContent = message;
  syncRecurringScheduleFields();
}

function editSchedule(schedule) {
  editingScheduleId = String(schedule.definition_id || '');
  const setValue = (id, value) => {
    const field = document.getElementById(id);
    if (field) field.value = value == null ? '' : String(value);
  };
  setValue('recurring-schedule-workspace', schedule.worker_id);
  setValue('recurring-schedule-instruction', schedule.instruction);
  setValue('recurring-schedule-type', schedule.recurrence_type || 'daily');
  setValue('recurring-schedule-local-time', schedule.local_time || '09:00');
  setValue('recurring-schedule-timezone', schedule.timezone_name || 'UTC');
  setValue('recurring-schedule-dst-policy', schedule.dst_policy || 'next_valid_earliest');
  setValue('recurring-schedule-starts-at', datetimeLocalValue(schedule.starts_at));
  setValue('recurring-schedule-ends-at', datetimeLocalValue(schedule.ends_at));
  setValue('recurring-schedule-cron', schedule.cron_expression || '');
  setValue('recurring-schedule-rrule', schedule.rrule || '');
  const intervalSeconds = Number(schedule.interval_seconds || 3600);
  const intervalUnit = intervalSeconds % 86400 === 0 ? 'days' : 'hours';
  setValue('recurring-schedule-interval-unit', intervalUnit);
  setValue('recurring-schedule-interval-value', intervalUnit === 'days' ? intervalSeconds / 86400 : intervalSeconds / 3600);
  setValue('recurring-schedule-overlap-policy', schedule.overlap_policy || 'skip');
  setValue('recurring-schedule-misfire-grace', Number(schedule.misfire_grace_seconds || 0) / 60);
  setValue('recurring-schedule-catch-up-policy', schedule.catch_up_policy || 'skip');
  setValue('recurring-schedule-catch-up-limit', schedule.max_catch_up_occurrences || 1);
  setValue('recurring-schedule-jitter', schedule.jitter_seconds || 0);
  const enabled = document.getElementById('recurring-schedule-enabled');
  if (enabled) enabled.checked = Boolean(schedule.enabled);
  const workspace = document.getElementById('recurring-schedule-workspace');
  if (workspace) workspace.disabled = true;
  const submit = document.getElementById('recurring-schedule-submit');
  if (submit) submit.textContent = 'Save changes';
  const cancel = document.getElementById('recurring-schedule-cancel-edit');
  if (cancel) cancel.hidden = false;
  const title = document.getElementById('recurring-schedule-form-title');
  if (title) title.textContent = 'Edit recurring work';
  const status = document.getElementById('recurring-schedule-status');
  if (status) status.textContent = 'Editing this definition will not change its occurrence history.';
  syncRecurringScheduleFields();
  document.getElementById('schedule-create-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function runScheduleNow(schedule, button) {
  button.disabled = true;
  button.textContent = 'Requesting…';
  const status = document.getElementById('recurring-schedule-status');
  button.dataset.idempotencyKey ||= globalThis.crypto?.randomUUID?.()
    || `run-now-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    const result = await api.postJson(
      `/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}/run-now`,
      { idempotency_key: button.dataset.idempotencyKey },
    );
    if (status) status.textContent = result.status === 'owner_action_required'
      ? String(result.message || 'The configured dispatch owner must run this schedule.')
      : 'Run requested. It will use the same private workspace and connected capabilities.';
    scheduleOccurrences.delete(String(schedule.definition_id || ''));
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = `${error.message} No duplicate occurrence was created; retry is safe after the issue is fixed.`;
    button.disabled = false;
    button.textContent = 'Run now';
  }
}

async function retireSchedule(schedule, button) {
  const name = workspaceName(schedule.worker_id);
  if (!window.confirm(`Remove this schedule for ${name}? It cannot be resumed, but its occurrence history will be kept.`)) return;
  button.disabled = true;
  button.textContent = 'Removing…';
  const status = document.getElementById('recurring-schedule-status');
  try {
    await api.deleteJson(`/api/recurring-schedules/${encodeURIComponent(schedule.definition_id)}`);
    if (editingScheduleId === schedule.definition_id) resetScheduleEditor();
    if (status) status.textContent = 'Schedule removed. Its immutable occurrence history is still available.';
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = error.message;
    button.disabled = false;
    button.textContent = 'Remove';
  }
}

function renderSchedules() {
  renderScheduleWorkspaceOptions();
  const list = document.getElementById('schedule-list');
  if (!list) return;
  const delegated = controlPlane?.recurrence_owner === 'viventium_cortex';
  const createCard = document.getElementById('schedule-create-card');
  const newAction = document.getElementById('new-schedule-action');
  if (createCard) createCard.hidden = false;
  if (newAction) newAction.hidden = false;
  let ownerNotice = null;
  if (delegated) {
    ownerNotice = node('article', 'schedule-card');
    ownerNotice.append(
      node('strong', '', 'Viventium Cortex is the single dispatch owner'),
      node('span', 'schedule-card-meta', 'Create and inspect schedules here or through MCP. GlassHive saves the shared definition, while only Viventium dispatches occurrences and re-checks user access and connected capabilities at fire time.'),
    );
    const ownerUrl = String(controlPlane?.recurrence_owner_url || '');
    if (ownerUrl) {
      const link = node('a', 'card-action', 'Open Viventium');
      link.href = ownerUrl;
      ownerNotice.append(link);
    }
  }
  if (scheduleLoadError) {
    list.replaceChildren(...[ownerNotice, emptyList(scheduleLoadError)].filter(Boolean));
    return;
  }
  const schedules = [...(recurringSchedules.items || [])].sort((left, right) => {
    if (Boolean(left.active) !== Boolean(right.active)) return left.active ? -1 : 1;
    return String(left.next_run_at || '').localeCompare(String(right.next_run_at || ''));
  });
  if (!schedules.length) {
    list.replaceChildren(...[ownerNotice, emptyList('No recurring work yet')].filter(Boolean));
    return;
  }
  const cards = schedules.map((schedule) => {
    const card = node('article', 'schedule-card');
    const head = node('div', 'schedule-card-head');
    const copy = node('div', 'schedule-card-copy');
    copy.append(
      node('strong', '', String(schedule.instruction || 'Recurring workspace task')),
      node('span', '', workspaceName(schedule.worker_id)),
    );
    const retired = Boolean(schedule.retired_at);
    head.append(copy, statusChip(retired ? 'retired' : (schedule.active ? 'active' : 'paused')));
    const meta = node('div', 'schedule-card-meta');
    meta.append(
      node('span', '', recurrenceLabel(schedule)),
      node('span', '', schedule.active ? `Next: ${formatDateTime(schedule.next_run_at)}` : (retired ? 'Retired · history retained' : 'Paused · no future occurrences')),
      node('span', '', schedule.owner_action === 'dispatch_via_viventium_cortex'
        ? 'Dispatch owner: Viventium Cortex'
        : 'Dispatch owner: GlassHive'),
    );
    if (schedule.last_occurrence_at) {
      meta.append(node('span', '', `Last occurrence: ${formatDateTime(schedule.last_occurrence_at)}`));
    }
    if (schedule.last_outcome || schedule.last_error) {
      meta.append(node(
        'span',
        'schedule-history-detail',
        `Latest result: ${String(schedule.last_outcome || 'needs attention').replaceAll('_', ' ')}${schedule.last_error ? ` · ${String(schedule.last_error)}` : ''}`,
      ));
    }
    const actions = node('div', 'schedule-card-actions');
    const historyButton = node('button', 'quiet-button', 'View history');
    historyButton.type = 'button';
    const history = node('div', 'schedule-history');
    history.hidden = true;
    historyButton.addEventListener('click', () => toggleOccurrenceHistory(String(schedule.definition_id || ''), history, historyButton));
    actions.append(historyButton);
    if (!retired) {
      const runNowButton = node('button', 'quiet-button', 'Run now');
      runNowButton.type = 'button';
      runNowButton.addEventListener('click', () => runScheduleNow(schedule, runNowButton));
      const editButton = node('button', 'quiet-button', 'Edit');
      editButton.type = 'button';
      editButton.addEventListener('click', () => editSchedule(schedule));
      actions.append(runNowButton, editButton);
    }
    if (schedule.active && !retired) {
      const stopButton = node('button', 'quiet-button', 'Pause');
      stopButton.type = 'button';
      stopButton.addEventListener('click', () => deactivateSchedule(schedule, stopButton));
      actions.append(stopButton);
    } else if (!retired) {
      const resumeButton = node('button', 'quiet-button', 'Resume schedule');
      resumeButton.type = 'button';
      resumeButton.addEventListener('click', () => resumeSchedule(schedule, resumeButton));
      actions.append(resumeButton);
    }
    if (!retired) {
      const retireButton = node('button', 'quiet-button', 'Remove');
      retireButton.type = 'button';
      retireButton.addEventListener('click', () => retireSchedule(schedule, retireButton));
      actions.append(retireButton);
    }
    card.append(head, meta, actions, history);
    return card;
  });
  list.replaceChildren(...[ownerNotice, ...cards].filter(Boolean));
}

async function loadSchedules() {
  try {
    const response = await fetch(api.withAuth('/api/recurring-schedules?include_inactive=true'));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load recurring schedules'));
    recurringSchedules = await response.json();
    scheduleLoadError = '';
  } catch (error) {
    recurringSchedules = { items: [] };
    scheduleLoadError = error.message;
  }
  renderSchedules();
}

function syncRecurringScheduleFields() {
  const kind = document.getElementById('recurring-schedule-type')?.value || 'daily';
  const intervalField = document.getElementById('recurring-schedule-interval-field');
  const dailyField = document.getElementById('recurring-schedule-daily-field');
  const onceField = document.getElementById('recurring-schedule-once-field');
  const timezoneField = document.getElementById('recurring-schedule-timezone-field');
  const cronField = document.getElementById('recurring-schedule-cron-field');
  const rruleField = document.getElementById('recurring-schedule-rrule-field');
  const startsAt = document.getElementById('recurring-schedule-starts-at');
  const catchUpPolicy = document.getElementById('recurring-schedule-catch-up-policy')?.value || 'skip';
  const catchUpLimitField = document.getElementById('recurring-schedule-catch-up-limit-field');
  const advanced = document.querySelector('.schedule-advanced');
  if (intervalField) intervalField.hidden = kind !== 'interval';
  if (dailyField) dailyField.hidden = kind !== 'daily';
  if (onceField) onceField.hidden = kind !== 'once';
  if (timezoneField) timezoneField.hidden = kind === 'interval';
  if (cronField) cronField.hidden = kind !== 'cron';
  if (rruleField) rruleField.hidden = kind !== 'rfc5545';
  if (startsAt) startsAt.required = kind === 'once';
  if (catchUpLimitField) catchUpLimitField.hidden = catchUpPolicy !== 'bounded';
  if (advanced) advanced.hidden = false;
}

async function submitRecurringSchedule(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById('recurring-schedule-status');
  const workerId = document.getElementById('recurring-schedule-workspace')?.value || '';
  const instruction = document.getElementById('recurring-schedule-instruction')?.value.trim() || '';
  const recurrenceType = document.getElementById('recurring-schedule-type')?.value || 'daily';
  if (!workerId) {
    if (status) status.textContent = 'Choose a saved workspace first.';
    document.getElementById('recurring-schedule-workspace')?.focus();
    return;
  }
  if (!instruction) {
    if (status) status.textContent = 'Describe what the workspace should do.';
    document.getElementById('recurring-schedule-instruction')?.focus();
    return;
  }
  const intervalValue = Number(document.getElementById('recurring-schedule-interval-value')?.value || 1);
  const intervalUnit = document.getElementById('recurring-schedule-interval-unit')?.value || 'hours';
  const intervalSeconds = Math.round(intervalValue * (intervalUnit === 'days' ? 86400 : 3600));
  const localTime = document.getElementById('recurring-schedule-local-time')?.value || '09:00';
  const timezoneName = recurrenceType === 'interval'
    ? 'UTC'
    : (document.getElementById('recurring-schedule-timezone')?.value.trim() || 'UTC');
  const startsAt = document.getElementById('recurring-schedule-starts-at')?.value || '';
  const endsAt = document.getElementById('recurring-schedule-ends-at')?.value || '';
  const cronExpression = document.getElementById('recurring-schedule-cron')?.value.trim() || '';
  const rrule = document.getElementById('recurring-schedule-rrule')?.value.trim() || '';
  if (recurrenceType === 'once' && !startsAt) {
    if (status) status.textContent = 'Choose when this one-time occurrence should run.';
    document.getElementById('recurring-schedule-starts-at')?.focus();
    return;
  }
  if (recurrenceType === 'cron' && !cronExpression) {
    if (status) status.textContent = 'Enter a structured cron expression.';
    document.getElementById('recurring-schedule-cron')?.focus();
    return;
  }
  if (recurrenceType === 'rfc5545' && !rrule) {
    if (status) status.textContent = 'Enter an RFC 5545 recurrence rule.';
    document.getElementById('recurring-schedule-rrule')?.focus();
    return;
  }
  const scheduleText = recurrenceLabel({
    recurrence_type: recurrenceType,
    interval_seconds: intervalSeconds,
    local_time: localTime,
    timezone_name: timezoneName,
    cron_expression: cronExpression,
    rrule,
    next_run_at: startsAt,
  });
  button.disabled = true;
  if (status) status.textContent = editingScheduleId ? 'Saving changes…' : 'Creating schedule…';
  try {
    const payload = {
      instruction,
      recurrence_type: recurrenceType,
      interval_seconds: recurrenceType === 'interval' ? intervalSeconds : null,
      local_time: recurrenceType === 'daily' ? localTime : '',
      timezone_name: timezoneName,
      dst_policy: document.getElementById('recurring-schedule-dst-policy')?.value || 'next_valid_earliest',
      cron_expression: recurrenceType === 'cron' ? cronExpression : '',
      rrule: recurrenceType === 'rfc5545' ? rrule : '',
      starts_at: startsAt || (editingScheduleId ? '' : null),
      ends_at: endsAt || (editingScheduleId ? '' : null),
      enabled: Boolean(document.getElementById('recurring-schedule-enabled')?.checked),
      overlap_policy: document.getElementById('recurring-schedule-overlap-policy')?.value || 'skip',
      misfire_grace_seconds: Math.round(Number(document.getElementById('recurring-schedule-misfire-grace')?.value || 0) * 60),
      catch_up_policy: document.getElementById('recurring-schedule-catch-up-policy')?.value || 'skip',
      max_catch_up_occurrences: Number(document.getElementById('recurring-schedule-catch-up-limit')?.value || 1),
      jitter_seconds: Number(document.getElementById('recurring-schedule-jitter')?.value || 0),
      schedule_text: scheduleText,
    };
    if (editingScheduleId) {
      await api.patchJson(`/api/recurring-schedules/${encodeURIComponent(editingScheduleId)}`, payload);
    } else {
      await api.postJson(`/api/workspace/${encodeURIComponent(workerId)}/recurring-schedules`, payload);
    }
    const completedEdit = Boolean(editingScheduleId);
    resetScheduleEditor(controlPlane?.recurrence_owner === 'viventium_cortex'
      ? 'Schedule saved. Viventium Cortex remains its only dispatch owner.'
      : (completedEdit ? 'Schedule changes saved.' : 'Schedule created. GlassHive is its dispatch owner.'));
    scheduleOccurrences.clear();
    await loadSchedules();
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderSupportHint() {
  const provider = document.getElementById('provider-account-provider')?.value || 'codex';
  const method = document.getElementById('provider-account-method')?.value || 'subscription';
  const submit = document.querySelector('#provider-account-form button[type="submit"]');
  if (!controlPlane || method !== 'subscription') {
    const option = (controlPlane?.provider_options || []).find((item) => item.provider === provider);
    const supported = provider === 'codex' && option?.inference_broker_support === 'supported';
    setProviderStatus(!supported
      ? (provider === 'claude'
        ? 'Claude API and enterprise routes are not exposed by the reviewed OpenAI inference broker.'
        : 'Use Manage connected accounts first; this deployment has not enabled the user-scoped inference broker.')
      : (method === 'api_key'
        ? 'Connect your OpenAI key in Manage connected accounts, then add its private GlassHive reference here. The key never enters GlassHive.'
        : 'Add the approved enterprise OpenAI route as a private GlassHive reference for this account.'));
    if (submit) {
      submit.textContent = supported ? 'Add connected account' : 'Connection unavailable';
      submit.disabled = !supported;
    }
    return;
  }
  const option = (controlPlane.provider_options || []).find((item) => item.provider === provider);
  setProviderStatus(option?.subscription_support === 'supported'
    ? ''
    : (SUPPORT_COPY[option?.subscription_support] || 'This connection is not available.'));
  if (submit) {
    submit.textContent = provider === 'claude' ? 'Connect Claude' : 'Connect Codex';
    submit.disabled = option?.subscription_support !== 'supported';
  }
}

function showSetup(payload) {
  const panel = document.getElementById('provider-setup-panel');
  const instructions = document.getElementById('provider-setup-instructions');
  const setupGuidance = document.getElementById('provider-setup-guidance');
  const setupLink = document.getElementById('provider-setup-link');
  const setupCode = document.getElementById('provider-setup-code');
  const setupCodeRow = document.getElementById('provider-setup-code-row');
  const setupHelp = document.getElementById('provider-setup-help');
  const technical = document.getElementById('provider-setup-technical');
  const restart = document.getElementById('restart-provider-setup');
  const provider = String(payload.provider || 'codex');
  const setupUrl = String(payload.setup_url || '');
  const code = String(payload.setup_code || '');
  const helpUrl = String(payload.help_url || '');
  const rawInstructions = String(payload.instructions || payload.message || '').trim();
  const waitingForGuidance = !payload.complete && !rawInstructions;
  const needsFallback = !payload.complete
    && !waitingForGuidance
    && (!setupUrl || (provider === 'codex' && !code));
  const accountId = String(payload.account_id || activeSetupAccount || '');
  const accountRow = [...document.querySelectorAll('.connection-row')]
    .find((candidate) => candidate.dataset.accountId === accountId);
  const accountChip = accountRow?.querySelector('.status-chip');
  const accountAction = accountRow?.querySelector('.connection-actions-primary > button');
  const accountMore = accountRow?.querySelector('.connection-more');
  if (instructions) instructions.textContent = rawInstructions || 'Preparing sign-in…';
  if (panel) panel.hidden = Boolean(payload.complete);
  if (setupLink) {
    setupLink.href = setupUrl || '#';
    const providerName = provider === 'claude' ? 'Claude' : provider === 'codex' ? 'Codex' : 'provider';
    setupLink.textContent = `Open ${providerName} sign-in`;
    setupLink.hidden = !setupUrl;
  }
  if (setupCode) setupCode.textContent = code;
  if (setupCodeRow) setupCodeRow.hidden = !code;
  if (setupHelp) {
    setupHelp.href = helpUrl || '#';
    setupHelp.textContent = 'Open ChatGPT security settings';
    setupHelp.hidden = !helpUrl;
  }
  if (setupGuidance) setupGuidance.textContent = needsFallback
    ? 'Use the provider instructions below.'
    : waitingForGuidance
      ? 'Starting secure sign-in…'
      : (provider === 'codex' ? 'Open sign-in, then enter the code.' : 'Open sign-in to continue.');
  if (technical && needsFallback) {
    technical.open = true;
    technical.dataset.autoOpened = 'true';
  } else if (technical?.dataset.autoOpened === 'true') {
    technical.open = false;
    delete technical.dataset.autoOpened;
  }
  if (!payload.complete && accountChip) {
    accountChip.textContent = 'connecting';
    accountChip.dataset.status = 'connecting';
  }
  if (!payload.complete && accountAction) accountAction.hidden = true;
  if (accountMore) accountMore.hidden = !payload.complete;
  if (restart) restart.hidden = !activeSetupAccount || Boolean(payload.complete);
  setProviderStatus(payload.status === 'ready'
    ? 'Connected.'
    : payload.complete
      ? String(payload.message || 'Sign-in was not completed. Try again.')
      : (needsFallback ? 'Sign-in details changed. Follow the technical details below.' : ''));
}

async function cancelActiveSetup({ reload = true } = {}) {
  if (!activeSetupAccount) return null;
  const accountId = activeSetupAccount;
  const payload = await api.postJson(`/api/provider-accounts/${encodeURIComponent(accountId)}/setup/cancel`);
  activeSetupAccount = '';
  stopSetupPolling();
  showSetup(payload);
  if (reload) await loadControlPlane();
  return { accountId, payload };
}

async function restartActiveSetup(button) {
  if (!activeSetupAccount) return;
  button.disabled = true;
  button.textContent = 'Restarting…';
  try {
    const cancelled = await cancelActiveSetup({ reload: false });
    if (!cancelled?.accountId) return;
    const payload = await api.postJson(`/api/provider-accounts/${encodeURIComponent(cancelled.accountId)}/setup`);
    activeSetupAccount = cancelled.accountId;
    showSetup(payload);
    setupPollTimer = window.setTimeout(pollSetup, 900);
  } catch (error) {
    await loadControlPlane().catch(() => {});
    setProviderStatus(error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Restart sign-in';
  }
}

function stopSetupPolling() {
  if (setupPollTimer) window.clearTimeout(setupPollTimer);
  setupPollTimer = 0;
}

async function pollSetup() {
  if (!activeSetupAccount) return;
  try {
    const response = await fetch(api.withAuth(`/api/provider-accounts/${encodeURIComponent(activeSetupAccount)}/setup`));
    if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not check provider sign-in'));
    const payload = await response.json();
    showSetup(payload);
    if (payload.complete) {
      activeSetupAccount = '';
      stopSetupPolling();
      await loadControlPlane();
      showSetup(payload);
      return;
    }
  } catch (error) {
    setProviderStatus(error.message);
  }
  setupPollTimer = window.setTimeout(pollSetup, 1500);
}

async function loadWorkspaceChoices() {
  const query = new URLSearchParams({ kind: 'named,legacy', limit: '100' });
  const response = await fetch(api.withAuth(`/api/workspaces?${query.toString()}`));
  if (!response.ok) throw new Error(await api.responseMessage(response, 'Could not load personal workspaces'));
  return response.json();
}

async function loadControlPlane() {
  const [controlResponse, connectResponse, workspacePayload, scheduleResponse] = await Promise.all([
    fetch(api.withAuth('/api/control-plane')),
    fetch(api.withAuth('/api/connect-ai')),
    loadWorkspaceChoices(),
    fetch(api.withAuth('/api/recurring-schedules?include_inactive=true')),
  ]);
  if (!controlResponse.ok) throw new Error(await api.responseMessage(controlResponse, 'Could not load connections'));
  if (!connectResponse.ok) throw new Error(await api.responseMessage(connectResponse, 'Could not load AI connection instructions'));
  controlPlane = await controlResponse.json();
  connectAi = await connectResponse.json();
  workspaceCatalog = workspacePayload;
  if (scheduleResponse.ok) {
    recurringSchedules = await scheduleResponse.json();
    scheduleLoadError = '';
  } else {
    recurringSchedules = { items: [] };
    scheduleLoadError = await api.responseMessage(scheduleResponse, 'Could not load recurring schedules');
  }
  renderProviderAccounts();
  renderConnections();
  renderConnectAi();
  renderLibrary();
  renderLibraryRequestWorkspaceOptions();
  renderSchedules();
  renderSupportHint();
  window.dispatchEvent(new CustomEvent('glasshive:control-plane-updated'));
}

async function submitProviderAccount(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const provider = document.getElementById('provider-account-provider').value;
  const method = document.getElementById('provider-account-method').value;
  const option = (controlPlane?.provider_options || []).find((item) => item.provider === provider);
  if (
    (method === 'subscription' && option?.subscription_support !== 'supported')
    || (method !== 'subscription' && (provider !== 'codex' || option?.inference_broker_support !== 'supported'))
  ) {
    setProviderStatus(SUPPORT_COPY[option?.subscription_support] || 'This account route is not available.');
    return;
  }
  button.disabled = true;
  setProviderStatus('Creating setup…');
  try {
    const payload = {
      provider: document.getElementById('provider-account-provider').value,
      auth_method: document.getElementById('provider-account-method').value,
      label: document.getElementById('provider-account-label').value.trim(),
      make_default: document.getElementById('provider-account-default').checked,
    };
    const created = await api.postJson('/api/provider-accounts', payload);
    form.reset();
    const disclosure = document.getElementById('add-provider-account');
    if (disclosure) disclosure.open = false;
    await loadControlPlane();
    if (created.platform_support === 'supported' && created.auth_method === 'subscription') {
      const setup = await api.postJson(`/api/provider-accounts/${encodeURIComponent(created.account_id)}/setup`);
      activeSetupAccount = String(created.account_id || '');
      showSetup(setup);
      stopSetupPolling();
      setupPollTimer = window.setTimeout(pollSetup, 900);
    } else {
      setProviderStatus(created.auth_method !== 'subscription'
        ? 'Connected-account reference added. GlassHive will verify it when this workspace runs.'
        : (SUPPORT_COPY[created.platform_support]
          || (created.status === 'ready' ? 'Account ready.' : 'Complete sign-in before using this account.')));
    }
  } catch (error) {
    setProviderStatus(error.message);
  } finally {
    button.disabled = false;
  }
}

export function renderActivity(events = [], availability = 'ready') {
  const list = document.getElementById('activity-list');
  if (!list) return;
  if (availability !== 'ready') {
    list.replaceChildren(emptyList('Activity is temporarily unavailable—refresh to retry'));
    return;
  }
  const ordered = [...events].sort((left, right) => String(right.created_at || right.last_activity_at || right.updated_at || '').localeCompare(String(left.created_at || left.last_activity_at || left.updated_at || '')));
  if (!ordered.length) {
    list.replaceChildren(emptyList('No workspace activity yet'));
    return;
  }
  list.replaceChildren(...ordered.slice(0, 40).map((event) => {
    const eventType = String(event.event_type || event.state || 'workspace.activity');
    const activityLabels = {
      'worker.created': 'Workspace created',
      'worker.ready': 'Workspace ready',
      'worker.metadata_updated': 'Workspace details updated',
      'worker.metadata.updated': 'Workspace details updated',
      'worker.duplicated': 'Workspace duplicated',
      'run.queued': 'Work queued',
      'run.started': 'Work started',
      'run.completed': 'Work completed',
      'run.failed': 'Work needs attention',
    };
    const eventLabel = activityLabels[eventType] || eventType.replaceAll('.', ' ').replaceAll('_', ' ');
    const row = node('article', 'activity-row');
    const copy = node('div', 'connection-copy');
    copy.append(
      node('strong', '', String(event.workspace_name || event.name || event.title || 'Workspace')),
      node('span', '', `${eventLabel} · ${formatDateTime(event.created_at || event.updated_at || event.last_activity_at)}`),
    );
    row.append(copy, statusChip(eventType));
    return row;
  }));
}

export async function refreshControlPlane() {
  if (!api) return;
  await loadControlPlane();
}

export function initializeControlPlane(dependencies) {
  api = dependencies;
  document.getElementById('library-request-form')?.addEventListener('submit', submitLibraryRequest);
  document.getElementById('provider-account-form')?.addEventListener('submit', submitProviderAccount);
  document.getElementById('provider-account-provider')?.addEventListener('change', renderSupportHint);
  document.getElementById('provider-account-method')?.addEventListener('change', renderSupportHint);
  document.getElementById('add-provider-account')?.addEventListener('toggle', (event) => {
    if (event.currentTarget.open) renderSupportHint();
    else if (document.getElementById('provider-setup-panel')?.hidden) setProviderStatus('');
  });
  document.getElementById('refresh-connections')?.addEventListener('click', () => loadControlPlane().catch((error) => {
    setProviderStatus(error.message);
  }));
  document.getElementById('new-schedule-action')?.addEventListener('click', () => {
    dependencies.setView('schedules');
    document.getElementById('recurring-schedule-instruction')?.focus();
  });
  document.getElementById('recurring-schedule-form')?.addEventListener('submit', submitRecurringSchedule);
  document.getElementById('recurring-schedule-cancel-edit')?.addEventListener('click', () => resetScheduleEditor('Edit cancelled.'));
  document.getElementById('recurring-schedule-type')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('recurring-schedule-catch-up-policy')?.addEventListener('change', syncRecurringScheduleFields);
  document.getElementById('refresh-schedules')?.addEventListener('click', () => loadSchedules());
  document.getElementById('copy-provider-setup-code')?.addEventListener('click', (event) => {
    const codeNode = document.getElementById('provider-setup-code');
    copyText(codeNode?.textContent || '', event.currentTarget, codeNode);
  });
  document.getElementById('restart-provider-setup')?.addEventListener('click', (event) => {
    restartActiveSetup(event.currentTarget);
  });
  const timezone = document.getElementById('recurring-schedule-timezone');
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (timezone && browserTimezone) timezone.value = browserTimezone;
  syncRecurringScheduleFields();
  document.getElementById('cancel-provider-setup')?.addEventListener('click', async () => {
    if (!activeSetupAccount) return;
    try {
      await cancelActiveSetup();
    } catch (error) {
      setProviderStatus(error.message);
    }
  });
}
