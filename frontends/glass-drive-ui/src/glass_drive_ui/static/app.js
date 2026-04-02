async function loadBootstrap() {
  const response = await fetch('/api/bootstrap');
  if (!response.ok) throw new Error('Failed to load worker options');
  return response.json();
}

function renderWorkerOptions(select, data) {
  const opts = [];
  for (const item of data.new_worker_options) {
    opts.push(`<option value="${item.value}">${item.label}</option>`);
  }
  if (data.existing_workers.length) {
    opts.push('<optgroup label="Existing workers">');
    for (const worker of data.existing_workers) {
      const label = `${worker.name} · ${worker.profile} · ${worker.project_title}`;
      opts.push(`<option value="existing:${worker.worker_id}">${label}</option>`);
    }
    opts.push('</optgroup>');
  }
  select.innerHTML = opts.join('');
  select.value = data.default_worker_option;
}

async function main() {
  const form = document.getElementById('launch-form');
  const select = document.getElementById('worker-option');
  const status = document.getElementById('launch-status');
  const button = document.getElementById('launch-button');
  try {
    const data = await loadBootstrap();
    renderWorkerOptions(select, data);
  } catch (error) {
    status.textContent = error.message;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = 'Launching worker…';
    const payload = {
      description: document.getElementById('description').value.trim(),
      success_criteria: document.getElementById('success_criteria').value.trim(),
      context: document.getElementById('context').value.trim(),
      worker_option: select.value,
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
