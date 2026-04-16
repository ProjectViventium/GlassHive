async function loadBootstrap() {
  const response = await fetch('/api/bootstrap');
  if (!response.ok) throw new Error('Failed to load workspace options');
  return response.json();
}

function renderLaunchSurfaceOptions(select, data) {
  const options = [];
  for (const item of data.launch_surface_options || []) {
    const option = document.createElement('option');
    option.value = String(item.value || '');
    option.textContent = String(item.label || item.value || '');
    options.push(option);
  }
  select.replaceChildren(...options);
  select.value = String(data.default_launch_surface || 'desktop');
}

function renderWorkspaceOptions(select, data) {
  const existing = data.existing_workspaces || [];
  const groups = [];

  if (existing.length) {
    const openGroup = document.createElement('optgroup');
    openGroup.label = 'Open workspace';
    for (const workspace of existing) {
      const option = document.createElement('option');
      option.value = `open:${String(workspace.worker_id || '')}`;
      option.textContent = `${String(workspace.workspace_label || workspace.worker_id || 'Workspace')} · ${String(workspace.profile || '')} · ${String(workspace.state || '')}`;
      openGroup.appendChild(option);
    }
    groups.push(openGroup);

    const duplicateGroup = document.createElement('optgroup');
    duplicateGroup.label = 'Duplicate workspace';
    for (const workspace of existing) {
      const option = document.createElement('option');
      option.value = `duplicate:${String(workspace.worker_id || '')}`;
      option.textContent = `${String(workspace.workspace_label || workspace.worker_id || 'Workspace')} · ${String(workspace.profile || '')} · ${String(workspace.state || '')}`;
      duplicateGroup.appendChild(option);
    }
    groups.push(duplicateGroup);
  }

  const newGroup = document.createElement('optgroup');
  newGroup.label = 'New workspace';
  for (const item of data.new_workspace_options || []) {
    const option = document.createElement('option');
    option.value = String(item.value || '');
    option.textContent = String(item.label || item.value || '');
    newGroup.appendChild(option);
  }
  groups.push(newGroup);

  select.replaceChildren(...groups);
  select.value = String(data.default_workspace_option || 'new:codex-cli');
}

function findWorkspace(existing, workerId) {
  return (existing || []).find((item) => item.worker_id === workerId) || null;
}

function workspaceMeta(selectValue, data) {
  const existing = data.existing_workspaces || [];
  if ((selectValue || '').startsWith('open:')) {
    const workspace = findWorkspace(existing, selectValue.split(':', 2)[1]);
    const label = workspace?.workspace_label || 'Selected workspace';
    return {
      buttonText: 'Open workspace',
      statusText: 'Opening workspace…',
      help: `Reuses ${label}. If it is paused, GlassHive resumes it automatically before the new run starts.`,
    };
  }
  if ((selectValue || '').startsWith('duplicate:')) {
    const workspace = findWorkspace(existing, selectValue.split(':', 2)[1]);
    const label = workspace?.workspace_label || 'Selected workspace';
    return {
      buttonText: 'Duplicate workspace',
      statusText: 'Duplicating workspace…',
      help: `Creates a new workspace using the files and project context from ${label}. Browser sessions do not copy.`,
    };
  }

  const profile = (selectValue || '').split(':', 2)[1] || 'codex-cli';
  const profileLabel = {
    'codex-cli': 'Codex',
    'claude-code': 'Claude Code',
    'openclaw-general': 'OpenClaw',
  }[profile] || profile;
  return {
    buttonText: 'New workspace',
    statusText: 'Creating workspace…',
    help: `Creates a fresh ${profileLabel} workspace with a clean browser profile and new project files.`,
  };
}

function syncWorkspaceUI(select, data, button, help) {
  const meta = workspaceMeta(select.value, data);
  button.textContent = meta.buttonText;
  help.textContent = meta.help;
  return meta;
}

async function main() {
  const form = document.getElementById('launch-form');
  const select = document.getElementById('workspace-option');
  const help = document.getElementById('workspace-help');
  const launchSurface = document.getElementById('launch-surface');
  const status = document.getElementById('launch-status');
  const button = document.getElementById('launch-button');
  let bootstrap = null;

  try {
    bootstrap = await loadBootstrap();
    renderWorkspaceOptions(select, bootstrap);
    renderLaunchSurfaceOptions(launchSurface, bootstrap);
    syncWorkspaceUI(select, bootstrap, button, help);
  } catch (error) {
    status.textContent = error.message;
  }

  select.addEventListener('change', () => {
    if (!bootstrap) return;
    syncWorkspaceUI(select, bootstrap, button, help);
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    button.disabled = true;
    const meta = bootstrap
      ? syncWorkspaceUI(select, bootstrap, button, help)
      : { statusText: 'Launching workspace…' };
    status.textContent = meta.statusText;
    const payload = {
      description: document.getElementById('description').value.trim(),
      success_criteria: document.getElementById('success_criteria').value.trim(),
      context: document.getElementById('context').value.trim(),
      workspace_option: select.value,
      launch_surface: launchSurface.value,
    };
    try {
      const response = await fetch('/api/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || 'Launch failed');
      }
      const data = await response.json();
      window.location.href = data.watch_url;
    } catch (error) {
      button.disabled = false;
      status.textContent = error.message;
    }
  });
}

main();
