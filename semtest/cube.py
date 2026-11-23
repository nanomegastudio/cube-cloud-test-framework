"""Minimal client for the Cube Cloud REST API and Control Plane API.

Every data call runs AS a persona: the client mints a short-lived HS256 JWT whose payload is the
persona's security context. There is deliberately no "default token" path, so no test can
silently fall through to an admin or fallback context.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import requests


# Cube's default ceiling on rows per query (CUBEJS_DB_QUERY_LIMIT). A query with no `limit` gets
# CUBEJS_DB_QUERY_DEFAULT_LIMIT (10,000 by default) applied silently, which is why rows_all()
# always sets one explicitly.
DEFAULT_ROW_LIMIT = 50000


class CubeError(AssertionError):
    """Cube answered with an error (raised as AssertionError so pytest reports it as a failure)."""


class TruncatedResult(CubeError):
    """Cube returned as many rows as the row limit allows, so the result is probably cut off.

    Raised instead of returning partial rows, so a cut-off result is reported as what it is rather
    than as missing rows, a parity mismatch or a fan-out "SHRANK".
    """


def jsonable(obj: Any) -> Any:
    """YAML turns 2026-10-01 into a date object; Cube wants the string. Round-trip via JSON."""
    return json.loads(json.dumps(obj, default=str))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class CubeClient:
    """Talks to one Cube environment (production or a branch staging environment).

    Args:
        api_url: The REST base URL ending in /cubejs-api, from Cube Cloud's
            Overview -> API credentials -> REST API (use the branch's URL for staging).
        api_secret: The deployment's CUBEJS_API_SECRET, used only to sign test tokens.
        token_ttl: Lifetime of minted tokens in seconds.
        timeout: Total seconds to keep retrying Cube's asynchronous "Continue wait".
    """

    def __init__(self, api_url: str, api_secret: str, token_ttl: int = 900,
                 timeout: int = 300, row_limit: int = DEFAULT_ROW_LIMIT) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_secret = api_secret
        self.token_ttl = token_ttl
        self.timeout = timeout
        self.row_limit = row_limit
        self.session = requests.Session()

    # ------------------------------------------------------------------ tokens

    def mint_token(self, claims: dict[str, Any], *, secret: str | None = None,
                   expired: bool = False) -> str:
        """Mint an HS256 JWT carrying `claims` as the security context, with iat/exp set."""
        now = int(time.time())
        payload = {"iat": now, "exp": now - 60 if expired else now + self.token_ttl, **claims}
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        key = (secret if secret is not None else self.api_secret).encode()
        sig = hmac.new(key, f"{header}.{body}".encode(), hashlib.sha256).digest()
        return f"{header}.{body}.{_b64url(sig)}"

    def _headers(self, claims: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": self.mint_token(claims), "Content-Type": "application/json"}

    # ------------------------------------------------------------------ endpoints

    def raw_get(self, path: str, headers: dict[str, str] | None = None) -> requests.Response:
        """Unauthenticated/hand-built GET, for testing how Cube treats bad credentials."""
        return self.session.get(f"{self.api_url}/v1/{path}", headers=headers or {}, timeout=60)

    def meta(self, claims: dict[str, Any], extended: bool = False) -> dict[str, Any]:
        """GET /v1/meta as a persona. The result is already filtered by that persona's access."""
        url = f"{self.api_url}/v1/meta" + ("?extended=true" if extended else "")
        resp = self.session.get(url, headers=self._headers(claims), timeout=60)
        if resp.status_code != 200:
            raise CubeError(f"/meta HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def sql(self, query: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
        """GET /v1/sql: the SQL Cube WOULD run, without running it (no BigQuery cost).

        Returns the `sql` block: `sql` is [sql_text, params]; `preAggregations` lists any
        rollups the query was routed to.
        """
        resp = self.session.get(f"{self.api_url}/v1/sql",
                                params={"query": json.dumps(jsonable(query))},
                                headers=self._headers(claims), timeout=90)
        if resp.status_code != 200:
            raise CubeError(f"/sql HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()["sql"]

    def load(self, query: dict[str, Any], claims: dict[str, Any],
             cache: str | None = "must-revalidate") -> dict[str, Any]:
        """POST /v1/load as a persona, retrying "Continue wait" until `timeout`.

        `cache="must-revalidate"` stops a stale in-memory result from masking a fresh failure.
        """
        body: dict[str, Any] = {"query": jsonable(query)}
        if cache:
            body["cache"] = cache
        deadline = time.time() + self.timeout
        while True:
            resp = self.session.post(f"{self.api_url}/v1/load", json=body,
                                     headers=self._headers(claims), timeout=120)
            data = resp.json() if resp.headers.get("content-type", "").startswith(
                "application/json") else {"error": resp.text}
            if str(data.get("error", "")).lower() == "continue wait":
                if time.time() > deadline:
                    raise CubeError(f"'Continue wait' exceeded {self.timeout}s for {query}")
                time.sleep(3)
                continue
            if resp.status_code != 200 or "error" in data:
                raise CubeError(f"/load HTTP {resp.status_code}: {str(data)[:800]}\nquery={query}")
            return data

    def rows(self, query: dict[str, Any], claims: dict[str, Any]) -> list[dict[str, Any]]:
        """Shortcut: the data rows of a /load response, exactly as Cube returned them.

        Use this only when a partial answer is fine (a `limit: 1` probe). To compare or total a
        result, use rows_all(), which refuses a cut-off result.
        """
        return self.load(query, claims).get("data", [])

    def rows_all(self, query: dict[str, Any], claims: dict[str, Any]) -> list[dict[str, Any]]:
        """All the rows a query should return, or TruncatedResult if Cube cut them off.

        A query that sets its own `limit` is a deliberate top-N and is returned as is. Otherwise
        `limit` is set to `self.row_limit` (rather than letting Cube apply its silent default)
        and a result that fills it is treated as truncated. A result of exactly row_limit rows is
        reported too, because it cannot be told apart from a cut-off one.
        """
        if query.get("limit") is not None:
            return self.rows(query, claims)
        rows = self.rows({**query, "limit": self.row_limit}, claims)
        if len(rows) >= self.row_limit:
            raise TruncatedResult(
                f"Cube returned {len(rows):,} rows, which is the row limit ({self.row_limit:,}), "
                f"so the result is probably cut off. This is not a data mismatch. Narrow the "
                f"query (a shorter dateRange or a filter), or raise suite.yml › row_limit if "
                f"your deployment's CUBEJS_DB_QUERY_LIMIT allows it.\nquery={query}")
        return rows


class ControlPlane:
    """Cube Cloud Control Plane API (deployment-level operations).

    Args:
        host: https://<tenant>.cubecloud.dev (or your custom domain).
        api_key: A Cube Cloud API key, sent as "Authorization: Bearer <key>".
        deployment_id: Numeric deployment id.
    """

    def __init__(self, host: str, api_key: str, deployment_id: str) -> None:
        self.host = host.rstrip("/")
        self.deployment_id = deployment_id
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def validate(self, branch: str | None = None) -> dict[str, Any]:
        """Compile the data model for a branch (or production) and return {valid, errors}."""
        params = {"branchName": branch} if branch else {}
        resp = requests.get(
            f"{self.host}/build/api/v1/deployments/{self.deployment_id}/data-model/validate",
            params=params, headers=self.headers, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def enable_staging(self, branch: str) -> None:
        """Keep a shared branch's staging environment always active (so CI can query it)."""
        resp = requests.put(
            f"{self.host}/build/api/v1/deployments/{self.deployment_id}/branches/staging-environment",
            json={"branchName": branch, "enabled": True}, headers=self.headers, timeout=60)
        resp.raise_for_status()
