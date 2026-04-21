from fastapi.testclient import TestClient

import glass_drive_ui.server as server_module
from glass_drive_ui.server import create_app


class FakeRuntimeClient:
    def __init__(self):
        self.desktop_actions = []
        self.launch_failures = []
        self.fail_assign = False
        self.duplicate_requests = []
        self.create_project_requests = []
        self.create_worker_requests = []
        self.assign_requests = []
        self.get_worker_requests = []
        self.message_requests = []
        self.steer_requests = []

    def health(self):
        return {"status": "ok"}

    def list_projects(self):
        return [{"project_id": "prj_1", "title": "Alpha"}]

    def list_workers(self, project_id: str):
        return [
            {"worker_id": "wrk_1", "name": "Main Worker", "profile": "codex-cli", "state": "ready"},
            {"worker_id": "wrk_dead", "name": "Old Worker", "profile": "codex-cli", "state": "terminated"},
        ]

    def get_worker(self, worker_id: str):
        self.get_worker_requests.append(worker_id)
        return {"worker_id": worker_id, "project_id": "prj_1", "profile": "codex-cli"}

    def get_project(self, project_id: str):
        return {"project_id": project_id, "title": "Alpha"}

    def worker_live(self, worker_id: str):
        return {
            "worker": {"worker_id": worker_id, "name": "Main Worker", "project_id": "prj_1", "profile": "codex-cli", "state": "ready"},
            "runtime_details": {"view_url": "http://127.0.0.1:60812/?autoconnect=1"},
            "latest_output": "OK",
            "deliverable": {
                "kind": "webpage",
                "browser_url": "file:///workspace/project/index.html",
                "label": "index.html",
                "preferred_surface": "desktop",
            },
        }

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str):
        self.create_project_requests.append(
            {
                "owner_id": owner_id,
                "title": title,
                "goal": goal,
                "default_worker_profile": default_worker_profile,
            }
        )
        return {"project_id": "prj_new"}

    def create_worker(self, project_id: str, owner_id: str, profile: str):
        self.create_worker_requests.append({"project_id": project_id, "owner_id": owner_id, "profile": profile})
        return {"worker_id": "wrk_new"}

    def duplicate_worker(self, project_id: str, source_worker_id: str, owner_id: str):
        self.duplicate_requests.append(
            {"project_id": project_id, "source_worker_id": source_worker_id, "owner_id": owner_id}
        )
        return {"worker_id": "wrk_dup"}

    def assign_run(self, worker_id: str, instruction: str):
        self.assign_requests.append({"worker_id": worker_id, "instruction": instruction})
        if self.fail_assign:
            raise RuntimeError("assign failed")
        return {"run_id": "run_1"}

    def launch_failed(self, worker_id: str, reason: str):
        self.launch_failures.append({"worker_id": worker_id, "reason": reason})
        return {"worker_id": worker_id, "state": "failed", "last_error": reason}

    def desktop_action(self, worker_id: str, action: str, url: str | None = None, run_id: str | None = None):
        self.desktop_actions.append({"worker_id": worker_id, "action": action, "url": url, "run_id": run_id})
        return {"status": "launched", "action": action}

    def message(self, worker_id: str, message: str):
        self.message_requests.append({"worker_id": worker_id, "message": message})
        return {"status": "queued"}

    def steer(self, worker_id: str, message: str):
        self.steer_requests.append({"worker_id": worker_id, "message": message})
        return {"run_id": "run_steer", "worker_id": worker_id, "project_id": "prj_1", "instruction": message, "state": "queued", "queued_at": "2026-04-17T00:00:00+00:00", "started_at": None, "ended_at": None, "output_text": "", "error_text": ""}

    def lifecycle(self, worker_id: str, action: str):
        return {"status": action}


def test_bootstrap_and_launch_flow():
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    boot = client.get('/api/bootstrap')
    assert boot.status_code == 200
    assert boot.json()['new_workspace_options'][0]['value'] == 'new:codex-cli'
    assert boot.json()['default_launch_surface'] == 'desktop'
    assert len(boot.json()['existing_workspaces']) == 1

    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': 'Focus on resumable sandboxes',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_new')
    assert 'surface=desktop' in launch.json()['watch_url']


def test_watch_assets_render():
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    home = client.get('/')
    assert home.status_code == 200
    assert 'GlassHive' in home.text
    assert 'Workspace' in home.text
    assert 'Glass Drive' not in home.text
    watch = client.get('/watch/wrk_1')
    assert watch.status_code == 200
    assert 'GlassHive' in watch.text
    assert 'Workspace live view' in watch.text
    assert 'Open project workspace' in watch.text
    assert 'Open worker details' in watch.text
    assert 'Send redirects now' in watch.text
    assert 'Hold Send or Cmd/Ctrl+Enter to queue instead' in watch.text
    assert 'Glass Drive' not in watch.text
    desktop = client.get('/desktop/wrk_1')
    assert desktop.status_code == 200
    assert 'GlassHive Desktop' in desktop.text
    live = client.get('/api/worker/wrk_1/live')
    assert live.status_code == 200
    assert live.json()['runtime_details']['view_url'].startswith('http://127.0.0.1:60812')
    assert live.json()['project_title'] == 'Alpha'


def test_launch_uses_desktop_surface_for_browser_projects():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Create a hello world landing page and verify it renders in the browser',
        'success_criteria': 'The page is visible and renders HELLO WORLD',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert 'surface=desktop' in launch.json()['watch_url']
    assert runtime.desktop_actions == [
        {'worker_id': 'wrk_new', 'action': 'terminal', 'url': None, 'run_id': 'run_1'},
    ]


def test_launch_preopens_browser_for_explicit_external_navigation():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Open the browser to https://example.com and inspect the page',
        'success_criteria': 'The page is visible and the title is captured',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert 'surface=desktop' in launch.json()['watch_url']
    assert runtime.desktop_actions == [
        {'worker_id': 'wrk_new', 'action': 'browser', 'url': 'https://example.com', 'run_id': None},
        {'worker_id': 'wrk_new', 'action': 'terminal', 'url': None, 'run_id': 'run_1'},
    ]


def test_browser_action_accepts_explicit_url():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/action/browser', json={'url': 'file:///workspace/project/index.html'})
    assert response.status_code == 200
    assert runtime.desktop_actions[-1] == {
        'worker_id': 'wrk_1',
        'action': 'browser',
        'url': 'file:///workspace/project/index.html',
        'run_id': None,
    }


def test_launch_respects_explicit_terminal_surface_override():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': '',
        'workspace_option': 'new:codex-cli',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert 'surface=terminal' in launch.json()['watch_url']
    assert runtime.desktop_actions == []


def test_launch_failure_marks_new_worker_failed():
    runtime = FakeRuntimeClient()
    runtime.fail_assign = True
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 502
    assert runtime.launch_failures == [{'worker_id': 'wrk_new', 'reason': 'assign failed'}]


def test_launch_duplicate_workspace_uses_runtime_duplicate_endpoint():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Branch the existing workspace for a parallel experiment',
        'success_criteria': 'The experiment starts in a duplicated workspace',
        'context': '',
        'workspace_option': 'duplicate:wrk_1',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_dup')
    assert runtime.duplicate_requests == [
        {'project_id': 'prj_new', 'source_worker_id': 'wrk_1', 'owner_id': 'demo-owner'},
    ]


def test_launch_open_workspace_reuses_existing_worker():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Resume the existing workspace for another task',
        'success_criteria': 'The same workspace starts a new run',
        'context': '',
        'workspace_option': 'open:wrk_1',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_1')
    assert runtime.get_worker_requests == ['wrk_1']
    assert runtime.create_project_requests == []
    assert runtime.create_worker_requests == []
    assert runtime.duplicate_requests == []
    assert runtime.assign_requests[0]['worker_id'] == 'wrk_1'


def test_launch_accepts_legacy_worker_option_fallback():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Resume through the legacy worker option fallback',
        'success_criteria': 'The same workspace starts a new run',
        'context': '',
        'worker_option': 'open:wrk_1',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_1')
    assert runtime.create_project_requests == []
    assert runtime.assign_requests[0]['worker_id'] == 'wrk_1'


def test_novnc_proxy_uses_worker_view_origin(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "ok";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            assert url == 'http://127.0.0.1:60812/core/rfb.js'
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    response = client.get('/novnc/wrk_1/core/rfb.js')
    assert response.status_code == 200
    assert response.text == 'export default "ok";'


def test_worker_steer_endpoint_uses_runtime_steer():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/steer', json={'message': 'Redirect to the new plan now.'})
    assert response.status_code == 200
    assert runtime.steer_requests == [{'worker_id': 'wrk_1', 'message': 'Redirect to the new plan now.'}]


def test_worker_message_endpoint_uses_runtime_queue_message():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/message', json={'message': 'Queue this after the current run finishes.'})
    assert response.status_code == 200
    assert runtime.message_requests == [{'worker_id': 'wrk_1', 'message': 'Queue this after the current run finishes.'}]
