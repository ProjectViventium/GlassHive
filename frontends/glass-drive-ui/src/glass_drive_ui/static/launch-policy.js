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

export function shouldResumeOnWorkspaceOpen({ workspaceKind, displayedState }) {
  return String(workspaceKind || '') === 'named'
    && WORKSPACE_OPEN_RESUME_STATES.has(String(displayedState || '').trim().toLowerCase());
}
