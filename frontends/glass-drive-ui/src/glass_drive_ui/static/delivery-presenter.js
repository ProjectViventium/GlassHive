function boundedSummary(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  return text.length <= 600 ? text : `${text.slice(0, 597)}...`;
}

function actionModel(value, fallbackLabel = 'Delivery') {
  if (!value || typeof value !== 'object') return null;
  const openUrl = String(value.open_url || '').trim();
  const downloadUrl = String(value.download_url || '').trim();
  if (!openUrl && !downloadUrl) return null;
  return {
    label: String(value.path || value.label || value.workspace_path || fallbackLabel).trim() || fallbackLabel,
    contentType: String(value.content_type || '').trim().toLowerCase(),
    openUrl,
    downloadUrl,
  };
}

function referenceBasename(value) {
  if (!value || typeof value !== 'object') return '';
  for (const candidate of [value.path, value.label, value.workspace_path, value.browser_url]) {
    const text = String(candidate || '').trim().split(/[?#]/, 1)[0].replaceAll('\\', '/');
    const basename = text.split('/').filter(Boolean).pop() || '';
    if (basename) return basename;
  }
  return '';
}

function referencePaths(value) {
  if (!value || typeof value !== 'object') return [];
  const paths = [];
  for (const [candidate, structuredPath] of [
    [value.workspace_path, true],
    [value.path, true],
    [value.label, false],
  ]) {
    const normalized = String(candidate || '')
      .trim()
      .split(/[?#]/, 1)[0]
      .replaceAll('\\', '/')
      .replace(/^\.\//, '')
      .replace(/^\/+/, '');
    if (normalized && (structuredPath || normalized.includes('/'))) paths.push(normalized);
  }
  return [...new Set(paths)];
}

export function workspaceProgressModel({ runState, workerState, hasDeliverable = false }) {
  const run = String(runState || '').trim().toLowerCase();
  const worker = String(workerState || '').trim().toLowerCase();
  if (['created', 'starting', 'resuming'].includes(run) || (!run && ['created', 'starting', 'resuming'].includes(worker))) {
    return {
      label: 'Starting',
      panelTitle: 'Starting workspace',
      summary: 'GlassHive is preparing the worker. Next: work starts automatically.',
    };
  }
  if (run === 'queued') {
    return {
      label: 'Queued',
      panelTitle: 'Queued work',
      summary: 'This step is queued. Next: GlassHive starts it automatically.',
    };
  }
  if (run === 'running') {
    if (hasDeliverable) {
      return {
        label: 'Live preview',
        panelTitle: 'Live preview',
        summary: 'A preview is ready while the worker finishes. You can watch it live or send a follow-up.',
      };
    }
    return {
      label: 'Working',
      panelTitle: 'Work in progress',
      summary: 'The worker is working on your project. You can watch it live or send a follow-up.',
    };
  }
  if (run === 'completed') {
    return {
      label: 'Complete',
      panelTitle: hasDeliverable ? 'Delivered result' : 'Work complete',
      summary: 'Work complete. Open the result or send a follow-up.',
    };
  }
  if (run === 'failed') {
    return {
      label: 'Needs attention',
      panelTitle: 'Run needs attention',
      summary: 'The run stopped before completion. Open technical details, then send a corrected follow-up.',
    };
  }
  if (run === 'cancelled') {
    return {
      label: 'Cancelled',
      panelTitle: 'Run cancelled',
      summary: 'This run was cancelled. Send a new instruction when you are ready.',
    };
  }
  if (run === 'interrupted') {
    return {
      label: 'Interrupted',
      panelTitle: 'Run interrupted',
      summary: 'This run was interrupted. Send a follow-up to continue in the same workspace.',
    };
  }
  if (['paused', 'idle', 'idle_terminated', 'stopped', 'ready'].includes(worker)) {
    return {
      label: worker === 'paused' ? 'Paused' : 'Ready',
      panelTitle: worker === 'paused' ? 'Workspace paused' : 'Workspace ready',
      summary: worker === 'paused'
        ? 'The workspace is paused. Resume it to continue from the same state.'
        : 'The workspace is ready for the next instruction.',
    };
  }
  return {
    label: 'Workspace status',
    panelTitle: 'Workspace status',
    summary: 'GlassHive is checking the workspace. The status updates automatically.',
  };
}

export function workspaceDeliveryModel(data) {
  const state = String(data?.latest_run?.state || '').trim().toLowerCase();
  const summary = boundedSummary(data?.latest_output);
  if (state !== 'completed') {
    return { available: false, state, summary, primary: null, artifacts: [] };
  }

  const declaredPrimary = actionModel(data?.deliverable, 'Delivered result');
  const seen = new Set();
  const artifacts = [];
  const artifactReferences = new Map();
  for (const item of Array.isArray(data?.artifacts?.items) ? data.artifacts.items : []) {
    const action = actionModel(item, 'Delivered file');
    if (!action) continue;
    const key = `${action.label}\u0000${action.openUrl}\u0000${action.downloadUrl}`;
    if (seen.has(key)) continue;
    seen.add(key);
    artifacts.push(action);
    artifactReferences.set(action, referencePaths(item));
  }
  const intendedPaths = new Set(referencePaths(data?.deliverable));
  const intendedBasename = referenceBasename(data?.deliverable);
  const primary = declaredPrimary
    || artifacts.find((artifact) => artifactReferences.get(artifact).some((path) => intendedPaths.has(path)))
    || artifacts.find((artifact) => referenceBasename(artifact) === intendedBasename)
    || artifacts[0]
    || null;
  return {
    available: Boolean(summary || primary || artifacts.length),
    state,
    summary: summary || (primary ? `${primary.label} is ready.` : 'Workspace completed.'),
    primary,
    artifacts,
  };
}
