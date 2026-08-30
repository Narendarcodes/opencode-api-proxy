import importlib
import os

from fastapi.testclient import TestClient

os.environ.setdefault("ZEN_API_KEY", "test-key")

import main

main = importlib.reload(main)

client = TestClient(main.app)


def test_missing_model_returns_clean_validation_error():
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["type"] == "validation_error"
    assert payload["error"]["message"] == "Missing required field: model"


def test_model_unavailable_is_classified_cleanly(monkeypatch):
    class FakeResponse:
        status_code = 400
        text = '{"error":{"message":"Model is unavailable","type":"error","code":"model_unavailable"}}'
        headers = {"content-type": "application/json"}

        def json(self):
            return {"error": {"message": "Model is unavailable", "type": "error", "code": "model_unavailable"}}

    async def fake_post(self, url, headers=None, json=None):
        return FakeResponse()

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "muse-spark-1.2-contributor-free",
            "messages": [{"role": "user", "content": "Reply with exactly: TEST OK"}],
            "stream": False,
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "model_unavailable"
    assert payload["error"]["message"] == "Model is unavailable"
    assert response.headers.get("x-request-id")


def test_authentication_failure_is_classified(monkeypatch):
    class FakeResponse:
        status_code = 401
        text = '{"error":{"message":"Invalid API key","type":"auth_error"}}'
        headers = {"content-type": "application/json"}

        def json(self):
            return {"error": {"message": "Invalid API key", "type": "auth_error"}}

    async def fake_post(self, url, headers=None, json=None):
        return FakeResponse()

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mimo-v2.5-free",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        },
    )
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["type"] == "authentication_error"
    assert payload["error"]["code"] == "authentication_error"


def test_streaming_response_is_sse(monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self):
            yield b"data: {\"ok\":true}\n\n"
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            return None

    async def fake_send(self, request, stream=False):
        return FakeResponse()

    monkeypatch.setattr(main.httpx.AsyncClient, "send", fake_send)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mimo-v2.5-free",
            "messages": [{"role": "user", "content": "Reply with exactly: STREAM TEST OK"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in response.text


def test_successful_model_request_returns_upstream_json(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = '{"id":"chatcmpl-1","object":"chat.completion","choices":[{"message":{"role":"assistant","content":"TEST OK"}}]}'
        headers = {"content-type": "application/json"}

        def json(self):
            return {"id": "chatcmpl-1", "object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "TEST OK"}}]}

    async def fake_post(self, url, headers=None, json=None):
        return FakeResponse()

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mimo-v2.5-free",
            "messages": [{"role": "user", "content": "Reply with exactly: TEST OK"}],
            "stream": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "TEST OK"
