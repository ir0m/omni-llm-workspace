from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniws.config import Settings
from omniws.events import message_chunk, thought_chunk, usage_update
from omniws.main import create_app
from omniws.providers.base import ModelInfo, ProviderError
from omniws.providers.registry import Registry

PASSWORD = "correct horse"


class FakeProvider:
    """Replies with the given chunks; remembers what it was asked."""

    kind = "api"

    def __init__(self, id: str = "fake", chunks: list[str] | None = None, delay: float = 0.0,
                 fail: ProviderError | None = None, thinking: str | None = None) -> None:
        self.id = id
        self.label = f"Fake {id}"
        self.chunks = chunks if chunks is not None else ["こんにちは", "、世界"]
        self.delay = delay
        self.fail = fail
        self.thinking = thinking
        self.requests = []

    async def list_models(self):
        return [ModelInfo(id="m1", name="Model 1", context_window=1000)]

    async def stream(self, request, model=None):
        self.requests.append(request)
        if self.thinking:
            yield thought_chunk(self.thinking)
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield message_chunk(chunk)
        if self.fail:
            raise self.fail
        yield usage_update(input_tokens=10, output_tokens=5, context_window=model.context_window if model else None)


def make_settings(tmp_path: Path, **values) -> Settings:
    defaults = {"omni_allowed_hosts": "testserver", "omni_data_dir": tmp_path / "data", "omni_password": PASSWORD}
    return Settings(_env_file=None, **{**defaults, **values})


@pytest.fixture
def providers():
    return {"fake": FakeProvider("fake"), "other": FakeProvider("other", chunks=["別モデル"])}


@pytest.fixture
def client(tmp_path, providers):
    settings = make_settings(tmp_path)
    app = create_app(settings, web_dist=Path("/nonexistent"), registry=Registry(settings, providers))
    with TestClient(app) as test_client:
        assert test_client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
        yield test_client


def read_events(client: TestClient, run_id: str, headers: dict | None = None) -> list[tuple[int, str, dict]]:
    events, current = [], {}
    with client.stream("GET", f"/api/runs/{run_id}/events", headers=headers or {}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("id: "):
                current["id"] = int(line[4:])
            elif line.startswith("event: "):
                current["event"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])
            elif line == "" and current:
                events.append((current["id"], current["event"], current["data"]))
                current = {}
    return events
