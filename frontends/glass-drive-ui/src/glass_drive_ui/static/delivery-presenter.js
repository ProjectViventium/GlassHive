function boundedSummary(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  return text.length <= 600 ? text : `${text.slice(0, 597)}...`;
}

function actionModel(value, fallbackLabel = 'Delivery') {
  if (!value || typeof value !== 'object') return null;
  const openUrl = String(value.open_url || value.browser_url || '').trim();
  const downloadUrl = String(value.download_url || '').trim();
  if (!openUrl && !downloadUrl) return null;
  return {
    label: String(value.path || value.label || value.workspace_path || fallbackLabel).trim() || fallbackLabel,
    contentType: String(value.content_type || '').trim().toLowerCase(),
    openUrl,
    downloadUrl,
  };
}

export function workspaceDeliveryModel(data) {
  const state = String(data?.latest_run?.state || '').trim().toLowerCase();
  const summary = boundedSummary(data?.latest_output);
  if (state !== 'completed') {
    return { available: false, state, summary, primary: null, artifacts: [] };
  }

  const primary = actionModel(data?.deliverable, 'Delivered result');
  const seen = new Set();
  const artifacts = [];
  for (const item of Array.isArray(data?.artifacts?.items) ? data.artifacts.items : []) {
    const action = actionModel(item, 'Delivered file');
    if (!action) continue;
    const key = `${action.label}\u0000${action.openUrl}\u0000${action.downloadUrl}`;
    if (seen.has(key)) continue;
    seen.add(key);
    artifacts.push(action);
  }
  return {
    available: Boolean(summary || primary || artifacts.length),
    state,
    summary: summary || (primary ? `${primary.label} is ready.` : 'Workspace completed.'),
    primary,
    artifacts,
  };
}
