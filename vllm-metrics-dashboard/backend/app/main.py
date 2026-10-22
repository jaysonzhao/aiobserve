"""Same-origin API and static application server."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .auth import authorize_namespace, request_actor
from .discovery import discover_workloads
from .exports import export_snapshot
from .identity import Actor
from .monitoring import MonitoringError, monitoring
from .query_service import build_snapshot
from .registry import TOP10_IDS, catalog
from .scale_gate import DEFAULT_POLICY as DEFAULT_SCALE_GATE_POLICY, POLICY_VERSION
from .scopes import decode_scope_id
from .settings import settings
from .snapshots import snapshots
from .time_utils import TimeRangeError, parse_bounds

app = FastAPI(title="vLLM Metrics Dashboard", version="1.1.0", docs_url=None, redoc_url=None)


@app.get("/api/v1/namespaces")
async def get_namespaces(actor: Actor = Depends(request_actor)) -> dict[str, Any]:
    authorized: list[str] = []
    for namespace in settings.allowed_namespaces:
        try:
            await authorize_namespace(actor, namespace)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        authorized.append(namespace)
    return {"namespaces": authorized}


class ScaleGatePolicyRequest(BaseModel):
    ttft_target_seconds: float = Field(default=3.0, gt=0, le=3600)
    tpot_target_seconds_per_token: float = Field(default=0.15, gt=0, le=3600)
    waiting_target_requests: float = Field(default=5.0, ge=0, le=1_000_000)
    kv_target_fraction: float = Field(default=0.90, ge=0, le=1)
    latency_window: Literal["1m", "2m", "5m", "10m", "15m"] = "5m"
    queue_window: Literal["1m", "2m", "5m", "10m", "15m"] = "5m"
    hold_seconds: int = Field(default=300, ge=0, le=3600)
    evaluation_step_seconds: Literal[30, 60, 120, 300] = 30
    ttft_min_observations: int = Field(default=30, ge=1, le=1_000_000)
    tpot_min_observations: int = Field(default=30, ge=1, le=1_000_000)
    max_sample_age_seconds: int = Field(default=60, ge=1, le=3600)


class QueryBody(BaseModel):
    namespace: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    model_id: str = Field(min_length=1, max_length=4096)
    start: str
    end: str
    timezone: str = Field(default=settings.default_timezone, max_length=100)
    metric_ids: list[int] = Field(default_factory=list, max_length=41)
    scope: Literal["pod", "service"] = "pod"
    window: Literal["1m", "2m", "5m", "10m", "15m", "30m", "1h", "24h"] = "5m"
    percentile: Literal[50, 90, 95, 99] = 95
    scale_gate_policy: ScaleGatePolicyRequest = Field(default_factory=ScaleGatePolicyRequest)
    selectors: dict[str, str | None] = Field(default_factory=dict)
    revision: str | None = Field(default=None, max_length=120)

    @field_validator("selectors")
    @classmethod
    def validate_selectors(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        allowed = {"job", "pod", "instance", "engine", "prometheus"}
        if any(key not in allowed for key in value):
            raise ValueError("Only job, pod, instance, and engine selectors are supported.")
        if any(item is not None and len(item) > 253 for item in value.values()):
            raise ValueError("Selector values may not exceed 253 characters.")
        return value


class ExportBody(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=200)
    format: Literal["csv", "json", "xlsx", "pdf"]


def _owner(actor: Actor) -> str:
    return actor.user


def _check_request_header(value: str | None) -> None:
    if value != "vllm-metrics-dashboard":
        raise HTTPException(status_code=403, detail="Same-origin request header is required.")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    token = Path(settings.prometheus_token_file)
    ca = Path(settings.prometheus_ca_file)
    try:
        ready = bool(settings.allowed_namespaces and token.is_file() and token.stat().st_size and ca.is_file())
    except OSError:
        ready = False
    return JSONResponse({"status": "ready" if ready else "not_ready"}, status_code=200 if ready else 503)


@app.get("/api/v1/catalog")
async def get_catalog(actor: Actor = Depends(request_actor)) -> dict[str, Any]:
    return {
        "metrics": catalog(),
        "top10_ids": list(TOP10_IDS),
        "registry_version": "1.1.0",
        "scale_gate": {
            "policy_version": POLICY_VERSION,
            "policy_defaults": DEFAULT_SCALE_GATE_POLICY,
            "scope": "per_pod",
            "calibration_status": "example_uncalibrated",
        },
    }


@app.get("/api/v1/filters")
async def get_filters(
    namespace: str = Query(min_length=1, max_length=63),
    start: str = Query(min_length=1, max_length=80),
    end: str = Query(min_length=1, max_length=80),
    timezone: str = Query(default=settings.default_timezone, max_length=100),
    actor: Actor = Depends(request_actor),
) -> dict[str, Any]:
    await authorize_namespace(actor, namespace)
    try:
        start_dt, end_dt, zone = parse_bounds(start, end, timezone)
        discovered = await discover_workloads(namespace, start=start_dt, end=end_dt)
    except TimeRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MonitoringError as exc:
        code = 403 if exc.state == "not_authorized" else 502
        raise HTTPException(status_code=code, detail=f"Model discovery unavailable ({exc.state}).") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Model discovery failed.") from exc
    models = []
    for model in discovered["models"]:
        models.append({key: model.get(key) for key in (
            "model_id", "model_name", "deployment", "deployment_resolved", "deployment_source",
            "pod_names", "job_values", "instance_values", "engine_values", "source_values", "duplicate_scrape_paths",
        )})
    return {
        "namespace": namespace,
        "timezone": str(zone),
        "namespaces": [namespace],
        "models": models,
        "series_discovered": discovered["series_discovered"],
        "state": discovered["state"],
        "warnings": discovered["warnings"],
    }


@app.post("/api/v1/query")
async def query_dashboard(
    body: QueryBody,
    x_requested_with: str | None = Header(default=None),
    actor: Actor = Depends(request_actor),
) -> dict[str, Any]:
    _check_request_header(x_requested_with)
    await authorize_namespace(actor, body.namespace)
    try:
        start, end, zone = parse_bounds(body.start, body.end, body.timezone)
        if body.model_id != "all":
            identity = decode_scope_id(body.model_id)
            if identity["namespace"] != body.namespace:
                raise HTTPException(status_code=403, detail="Model identity is outside the authorized namespace.")
        snapshot_data = await build_snapshot(
            namespace=body.namespace,
            model_id=body.model_id,
            start=start,
            end=end,
            zone=zone,
            metric_ids=body.metric_ids or None,
            view_scope=body.scope,
            window=body.window,
            percentile=body.percentile,
            scale_gate_policy=body.scale_gate_policy.model_dump(),
            selected_filters={key: value for key, value in body.selectors.items() if value},
            revision=body.revision,
        )
    except TimeRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except MonitoringError as exc:
        code = 403 if exc.state == "not_authorized" else 502
        raise HTTPException(status_code=code, detail=f"Monitoring access failed ({exc.state}).") from exc
    entry = snapshots.create_snapshot(_owner(actor), snapshot_data)
    return {"snapshot_id": entry.id, **snapshot_data}


@app.post("/api/v1/daily-tokens")
async def daily_tokens(
    body: QueryBody,
    x_requested_with: str | None = Header(default=None),
    actor: Actor = Depends(request_actor),
) -> dict[str, Any]:
    _check_request_header(x_requested_with)
    await authorize_namespace(actor, body.namespace)
    try:
        start, end, zone = parse_bounds(body.start, body.end, body.timezone)
        result = await build_snapshot(
            namespace=body.namespace, model_id=body.model_id, start=start, end=end, zone=zone,
            metric_ids=[34, 35], view_scope=body.scope, window=body.window, percentile=body.percentile,
            scale_gate_policy=body.scale_gate_policy.model_dump(),
            selected_filters={key: value for key, value in body.selectors.items() if value}, revision=body.revision,
        )
    except TimeRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except MonitoringError as exc:
        code = 403 if exc.state == "not_authorized" else 502
        raise HTTPException(status_code=code, detail=f"Monitoring access failed ({exc.state}).") from exc
    return {"filters": result["filters"], "daily_tokens": result["daily_tokens"], "warnings": result["warnings"]}


@app.post("/api/v1/exports")
async def create_export(
    body: ExportBody,
    x_requested_with: str | None = Header(default=None),
    actor: Actor = Depends(request_actor),
) -> dict[str, Any]:
    _check_request_header(x_requested_with)
    try:
        snapshot = snapshots.get_snapshot(body.snapshot_id, _owner(actor)).data
    except KeyError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    await authorize_namespace(actor, snapshot["filters"]["namespace"])
    try:
        filename, media_type, content = export_snapshot(snapshot, body.format)
        export = snapshots.create_export(_owner(actor), snapshot["filters"]["namespace"], filename, media_type, content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"export_id": export.id, "filename": filename, "media_type": media_type, "size_bytes": len(content)}


@app.get("/api/v1/exports/{export_id}")
async def download_export(export_id: str, actor: Actor = Depends(request_actor)) -> Response:
    try:
        export = snapshots.get_export(export_id, _owner(actor))
    except KeyError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    await authorize_namespace(actor, export.namespace)
    disposition = f"attachment; filename*=UTF-8''{quote(export.filename)}"
    return Response(export.content, media_type=export.media_type, headers={"Content-Disposition": disposition})


@app.get("/api/v1/status")
async def get_status(actor: Actor = Depends(request_actor)) -> dict[str, Any]:
    authorized: list[str] = []
    for namespace in settings.allowed_namespaces:
        try:
            await authorize_namespace(actor, namespace)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        authorized.append(namespace)
    return {"monitoring": await monitoring.health(), "authorized_namespaces": authorized}


dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if dist.is_dir():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        index = dist / "index.html"
        return FileResponse(index)
