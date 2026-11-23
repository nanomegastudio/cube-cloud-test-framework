"""Small stand-ins for Cube's HTTP API, used only by the framework's own unit tests."""
from __future__ import annotations

from typing import Any, Callable

from semtest.cube import CubeClient


class _Resp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    """Answers POST /v1/load with `handler(query) -> response body`, recording each query."""

    def __init__(self, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.handler = handler
        self.queries: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str], timeout: int):
        self.queries.append(json["query"])
        return _Resp(self.handler(json["query"]))


def fake_cube(handler: Callable[[dict[str, Any]], dict[str, Any]],
              row_limit: int = 50000) -> CubeClient:
    client = CubeClient("https://cube.invalid/cubejs-api", "test-secret", row_limit=row_limit)
    client.session = FakeSession(handler)  # type: ignore[assignment]
    return client
