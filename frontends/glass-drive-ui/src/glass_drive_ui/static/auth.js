const oidcButton = document.querySelector('#oidc-login');
const status = document.querySelector('#auth-status');
const pageParams = new URLSearchParams(window.location.search);
const requestedReturnTo = String(pageParams.get('return_to') || '/');
const returnTo = requestedReturnTo.startsWith('/')
  && !requestedReturnTo.startsWith('//')
  && !requestedReturnTo.includes('\\')
  ? requestedReturnTo
  : '/';
const errorMessages = {
  access_denied: 'Your organization did not approve this sign-in. Try another approved account or ask an administrator for access.',
  account_not_authorized: 'This account does not have an approved GlassHive role. Ask an administrator to assign access, then retry.',
  account_not_registered: 'This account is not registered for GlassHive. Ask an administrator to approve it, then retry.',
  callback_invalid: 'That sign-in response was incomplete. Start sign-in again.',
  cancelled: 'Sign-in was cancelled. Retry when you are ready, or choose another account at your identity provider.',
  identity_invalid: 'Your organization did not provide the stable identity GlassHive requires. Ask an administrator to review the identity configuration.',
  provider_configuration: 'GlassHive cannot use the organization sign-in configuration. Ask an administrator to review it.',
  provider_unavailable: 'Your organization’s sign-in service is temporarily unavailable. Wait a moment, then retry.',
  sign_in_failed: 'GlassHive could not complete sign-in. Retry, or ask an administrator if it continues.',
  state_expired: 'That sign-in attempt expired or was already used. Start a new sign-in.',
  state_invalid: 'GlassHive could not verify that sign-in attempt. Start a new sign-in from this page.',
  token_invalid: 'GlassHive could not verify the organization sign-in response. Retry, or ask an administrator to review the app registration.',
};

async function initialize() {
  const [configResponse, sessionResponse] = await Promise.all([
    fetch('/auth/config'),
    fetch('/auth/session'),
  ]);
  if (!configResponse.ok || !sessionResponse.ok) throw new Error('GlassHive sign-in is unavailable.');
  const config = await configResponse.json();
  const session = await sessionResponse.json();
  if (session.authenticated) {
    window.location.replace(returnTo);
    return;
  }
  if (!config.oidc) throw new Error('Sign-in is provided by the configured external identity gateway.');
  if (config.provider_email_login || config.email_login) {
    oidcButton.textContent = 'Continue with email or organization';
  }
  oidcButton.href = `/auth/oidc/start?return_to=${encodeURIComponent(returnTo)}`;
  oidcButton.hidden = false;

  if (config.principal_enrollment === false) {
    status.textContent = 'New access must be provisioned by an administrator.';
  }

  const authError = String(pageParams.get('auth_error') || '');
  if (errorMessages[authError]) status.textContent = errorMessages[authError];
  else if (pageParams.get('provider_logout') === 'unavailable') {
    status.textContent = 'You are signed out of GlassHive. Your organization did not expose account switching here; choose Continue to sign in again.';
  } else if (pageParams.has('logged_out')) {
    status.textContent = 'You are signed out of GlassHive.';
  }
}

initialize().catch((error) => {
  status.textContent = error.message || 'GlassHive sign-in is unavailable.';
});
