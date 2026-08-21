export function preferredProviderAccountId(readyAccounts, currentAccount) {
  const currentId = String(currentAccount || '').trim();
  if (currentId && readyAccounts.some((account) => String(account.account_id || '') === currentId)) {
    return currentId;
  }
  const defaultAccount = readyAccounts.find((account) => Boolean(account.is_default));
  if (defaultAccount) return String(defaultAccount.account_id || '');
  return readyAccounts.length === 1 ? String(readyAccounts[0].account_id || '') : '';
}

export function credentialPolicyTransition({
  currentPolicy,
  savedPersonalPolicy,
  forcedLegacy,
  supportsPersonalAccounts,
}) {
  const personalPolicy = forcedLegacy
    ? String(savedPersonalPolicy || 'personal_required')
    : String(currentPolicy || 'personal_required');
  if (!supportsPersonalAccounts) {
    return { value: 'legacy', savedPersonalPolicy: personalPolicy, forcedLegacy: true };
  }
  return { value: personalPolicy, savedPersonalPolicy: '', forcedLegacy: false };
}

function workerProfileLabel(profile) {
  return {
    'codex-cli': 'Codex',
    'claude-code': 'Claude Code',
    'openclaw-general': 'OpenClaw',
  }[profile] || profile || 'Worker';
}

export function workerAccountSummary({ workspaceValue, accountId, policy, data }) {
  const value = String(workspaceValue || '');
  if (value.startsWith('open:') || value.startsWith('duplicate:')) {
    const duplicate = value.startsWith('duplicate:');
    const workerId = value.split(':', 2)[1] || '';
    const workspace = (data?.existing_workspaces || []).find(
      (item) => String(item?.worker_id || '') === workerId,
    );
    const profile = workerProfileLabel(String(workspace?.profile || ''));
    const readiness = workspace?.provider_readiness || {};
    let route = `${profile} · Saved workspace account`;
    if (readiness.readiness === 'unavailable') route = `${profile} · Account status unavailable`;
    else if (readiness.fallback || readiness.policy === 'legacy') route = `${profile} · Organization account`;
    else if (readiness.label) route = `${profile} · ${String(readiness.label)}`;
    else if (readiness.readiness === 'action_required') route = `${profile} · Account needs attention`;
    return duplicate && String(readiness.account_id || '')
      ? `${route} · Reapproval required after copy`
      : route;
  }

  const profileId = value.startsWith('new:') ? value.split(':', 2)[1] || '' : '';
  const profile = workerProfileLabel(profileId);
  if (policy === 'legacy') return `${profile} · Organization account`;
  if (data?.bootstrap_sections && data.bootstrap_sections.provider_accounts !== 'ready') {
    return `${profile} · Account status unavailable`;
  }

  const account = (data?.provider_accounts || []).find(
    (item) => String(item?.account_id || '') === String(accountId || ''),
  );
  if (!account) {
    return policy === 'personal_preferred'
      ? `${profile} · Organization account · No ready personal account`
      : `${profile} · Personal account required`;
  }
  const label = String(account.label || account.provider || 'Personal account');
  return policy === 'personal_preferred'
    ? `${profile} · ${label} · Organization fallback allowed`
    : `${profile} · ${label}`;
}

const WORKSPACE_OPEN_RESUME_STATES = new Set(['paused', 'idle', 'idle_terminated', 'stopped']);
const WORKSPACE_LIFECYCLE_RESUME_STATES = new Set([
  'ready',
  'paused',
  'idle',
  'idle_terminated',
  'stopped',
  'completed',
  'retained',
]);
const WORKSPACE_LIFECYCLE_DISABLED_STATES = new Set([
  'created',
  'starting',
  'terminating',
  'termination_failed',
  'terminated',
]);
const WORKSPACE_LIFECYCLE_HIDDEN_STATES = new Set([
  'terminating',
  'termination_failed',
  'terminated',
]);

export function shouldResumeOnWorkspaceOpen({ workspaceKind, renderedState, fallbackState }) {
  const displayedState = String(renderedState || fallbackState || '').trim().toLowerCase();
  return String(workspaceKind || '') === 'named'
    && WORKSPACE_OPEN_RESUME_STATES.has(displayedState);
}

export function workspaceSetupAction(profile) {
  const normalized = String(profile || '').trim().toLowerCase();
  if (normalized === 'codex-cli') return 'codex';
  if (normalized === 'claude-code') return 'claude';
  if (normalized.startsWith('openclaw')) return 'openclaw';
  return 'terminal';
}

export function workspaceLifecycleControl(state) {
  const normalized = String(state || '').trim().toLowerCase();
  const action = WORKSPACE_LIFECYCLE_RESUME_STATES.has(normalized) ? 'resume' : 'pause';
  return {
    action,
    label: normalized === 'completed' ? 'Continue' : action === 'resume' ? 'Resume' : 'Pause',
    hidden: WORKSPACE_LIFECYCLE_HIDDEN_STATES.has(normalized),
    disabled: WORKSPACE_LIFECYCLE_DISABLED_STATES.has(normalized),
  };
}
