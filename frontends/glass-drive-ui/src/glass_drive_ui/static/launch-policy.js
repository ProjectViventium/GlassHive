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
