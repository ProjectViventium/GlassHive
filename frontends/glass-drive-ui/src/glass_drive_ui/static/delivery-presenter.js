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
