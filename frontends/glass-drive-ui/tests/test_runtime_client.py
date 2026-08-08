from __future__ import annotations

import uuid

import glass_drive_ui.runtime_client as runtime_client_module
from glass_drive_ui.runtime_client import RuntimeClient


def test_runtime_client_refreshes_dynamic_headers_for_every_upstream_request(monkeypatch):
    captured_headers: list[dict[str, str]] = []

    class SyntheticResponse:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class SyntheticHttpClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            _ = method, url
            captured_headers.append(dict(kwargs.get("headers") or {}))
            return SyntheticResponse()

    monkeypatch.setattr(runtime_client_module.httpx, "Client", SyntheticHttpClient)
    scoped = RuntimeClient(
        "https://runtime.example.invalid",
        headers={"X-WPR-Token": "service-token"},
    ).with_headers_factory(
        lambda: {"X-GlassHive-User-Assertion": uuid.uuid4().hex}
    )

    scoped.get_preferences()
    scoped.list_activity()

    assert len(captured_headers) == 2
    assert all(headers["X-WPR-Token"] == "service-token" for headers in captured_headers)
    assert (
        captured_headers[0]["X-GlassHive-User-Assertion"]
        != captured_headers[1]["X-GlassHive-User-Assertion"]
    )
