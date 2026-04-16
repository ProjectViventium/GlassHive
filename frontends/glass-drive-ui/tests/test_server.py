from fastapi.testclient import TestClient

import glass_drive_ui.server as server_module
from glass_drive_ui.server import create_app


class FakeRuntimeClient:
    def __init__(self):
        self.desktop_actions = []
        self.launch_failures = []
        self.fail_assign = False

    def health(self):
        return {"status": "ok"}

    def list_projects(self):
        return [{"project_id": "prj_1", "title": "Alpha"}]

    def list_workers(self, project_id: str):
        return [{"worker_id": "wrk_1", "name": "Main Worker", "profile": "codex-cli", "state": "ready"}]

    def get_worker(self, worker_id: str):
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
        return {"project_id": "prj_new"}

    def create_worker(self, project_id: str, owner_id: str, profile: str):
        return {"worker_id": "wrk_new"}

    def assign_run(self, worker_id: str, instruction: str):
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
        return {"status": "queued"}

    def lifecycle(self, worker_id: str, action: str):
        return {"status": action}


def test_bootstrap_and_launch_flow():
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    boot = client.get('/api/bootstrap')
    assert boot.status_code == 200
    assert boot.json()['new_worker_options'][0]['value'] == 'new:codex-cli'
    assert boot.json()['default_launch_surface'] == 'desktop'

    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': 'Focus on resumable sandboxes',
        'worker_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_new')
    assert 'surface=desktop' in launch.json()['watch_url']


def test_watch_assets_render():
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    home = client.get('/')
    assert home.status_code == 200
    assert 'GlassHive' in home.text
    assert 'Glass Drive' not in home.text
    watch = client.get('/watch/wrk_1')
    assert watch.status_code == 200
    assert 'GlassHive' in watch.text
    assert 'Open project workspace' in watch.text
    assert 'Open worker details' in watch.text
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
        'worker_option': 'new:codex-cli',
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
        'worker_option': 'new:codex-cli',
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
        'worker_option': 'new:codex-cli',
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
        'worker_option': 'new:codex-cli',
    })
    assert launch.status_code == 502
    assert runtime.launch_failures == [{'worker_id': 'wrk_new', 'reason': 'assign failed'}]


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
