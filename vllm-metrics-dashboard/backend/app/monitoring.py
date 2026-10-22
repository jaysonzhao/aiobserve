"""Small async client for the Prometheus-compatible Thanos HTTP API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from .settings import settings


class MonitoringError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, state: str = "query_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.state = state


class MonitoringClient:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    def _token(self) -> str | None:
        try:
            token = Path(settings.prometheus_token_file).read_text(encoding="utf-8").strip()
            return token or None
        except OSError:
            return None

    def _verify(self) -> bool | str:
        ca = Path(settings.prometheus_ca_file)
        return str(ca) if ca.is_file() else True

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not path.startswith("/api/v1/"):
            raise ValueError("Only Prometheus API paths are allowed.")
        headers = {"Accept": "application/json"}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = settings.prometheus_url + path
        async with self._semaphore:
            try:
                async with httpx.AsyncClient(
                    verify=self._verify(), timeout=settings.query_timeout_seconds, follow_redirects=False
                ) as client:
                    response = await client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                raise MonitoringError("Monitoring request timed out.", state="query_error") from exc
            except httpx.RequestError as exc:
                raise MonitoringError("Monitoring endpoint is unavailable.", state="query_error") from exc

        if response.status_code in {401, 403}:
            raise MonitoringError("Monitoring access was denied.", status_code=response.status_code, state="not_authorized")
        if response.status_code >= 500:
            raise MonitoringError("Monitoring returned a server error.", status_code=response.status_code)
        if response.status_code >= 400:
            raise MonitoringError("Monitoring rejected the query.", status_code=response.status_code)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise MonitoringError("Monitoring returned invalid JSON.") from exc
        if payload.get("status") != "success":
            raise MonitoringError(str(payload.get("error", "Monitoring query failed.")))
        return payload

    async def query(self, promql: str, *, at: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"query": promql}
        if at is not None:
            params["time"] = at
        return await self.get("/api/v1/query", params)

    async def query_range(self, promql: str, *, start: float, end: float, step: int) -> dict[str, Any]:
        return await self.get(
            "/api/v1/query_range",
            {"query": promql, "start": start, "end": end, "step": step},
        )

    async def series(self, selector: str, *, start: float, end: float, limit: int) -> list[dict[str, str]]:
        payload = await self.get(
            "/api/v1/series",
            {"match[]": selector, "start": start, "end": end, "limit": limit},
        )
        return payload.get("data", [])

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self.query("up")
            return {"state": "available", "series": len(payload.get("data", {}).get("result", []))}
        except MonitoringError as exc:
            return {"state": exc.state, "reason": str(exc)}


monitoring = MonitoringClient()
