"""Scoped namespace, model, deployment, pod, and source discovery."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from .kubernetes import kubernetes
from .monitoring import MonitoringError, monitoring
from .scopes import sign_scope
from .settings import settings


def _overrides() -> list[dict[str, Any]]:
    raw = os.getenv("MODEL_SCOPE_OVERRIDES_JSON", "[]")
    metadata_warning = None
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MODEL_SCOPE_OVERRIDES_JSON is invalid JSON.") from exc
    if not isinstance(values, list):
        raise RuntimeError("MODEL_SCOPE_OVERRIDES_JSON must be a list.")
    return [item for item in values if isinstance(item, dict)]


def _override_for(namespace: str, model: str, serving_value: str | None, observed_pods: list[str]) -> dict[str, Any] | None:
    for item in _overrides():
        if item.get("namespace") != namespace or item.get("model_name") != model:
            continue
        expected_serving = item.get("serving_value")
        if expected_serving and expected_serving != serving_value:
            continue
        pod_regex = str(item.get("pod_regex", ""))
        if not pod_regex or len(pod_regex) > 512 or not pod_regex.startswith("^") or not pod_regex.endswith("$"):
            continue
        try:
            compiled = re.compile(pod_regex)
        except re.error:
            continue
        if observed_pods and any(not compiled.fullmatch(pod) for pod in observed_pods):
            continue
        deployment = str(item.get("deployment", ""))
        if not deployment:
            continue
        return {"deployment": deployment, "pod_regex": pod_regex, "source": "explicit_config"}
    return None


async def discover_workloads(
    namespace: str,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    if namespace not in settings.allowed_namespaces:
        raise PermissionError("Namespace is outside the configured allowlist.")
    start_ts, end_ts = start.timestamp(), end.timestamp()
    selector = f'{{__name__=~"vllm:.*",namespace={json.dumps(namespace)}}}'
    try:
        series_task = monitoring.series(
            selector,
            start=start_ts,
            end=end_ts,
            limit=settings.max_discovery_series,
        )
        k8s_task = kubernetes.workload_metadata(namespace)
        series, workload_data = await asyncio.gather(series_task, k8s_task)
    except MonitoringError:
        raise
    except RuntimeError:
        metadata_warning = "Kubernetes owner metadata was unavailable; discovered series remain unresolved until ownership can be confirmed."
        series = await monitoring.series(
            selector,
            start=start_ts,
            end=end_ts,
            limit=settings.max_discovery_series,
        )
        workload_data = {"pods": [], "deployments": [], "pod_to_deployment": {}, "pod_labels": {}}

    pod_to_deployment: dict[str, str] = workload_data["pod_to_deployment"]
    rows: list[dict[str, str]] = []
    for labels in series:
        model_name = labels.get("model_name")
        pod = labels.get("pod")
        if not model_name or not pod:
            continue
        serving_value = labels.get("llm_isvc_name") or labels.get("inference_service") or ""
        rows.append(
            {
                "model_name": model_name,
                "pod": pod,
                "deployment": pod_to_deployment.get(pod, ""),
                "serving_label": "llm_isvc_name" if labels.get("llm_isvc_name") else ("inference_service" if labels.get("inference_service") else ""),
                "serving_value": serving_value,
                "job": labels.get("job", ""),
                "instance": labels.get("instance", ""),
                "engine": labels.get("engine", ""),
                "prometheus": labels.get("prometheus", ""),
            }
        )

    candidates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row["deployment"]:
            candidates[(row["model_name"], row["serving_label"], row["serving_value"])].add(row["deployment"])

    # Historical pod series can be attributed to a physical Deployment only when
    # the observed serving identity maps to exactly one owner in current metadata.
    for row in rows:
        if not row["deployment"]:
            matching = candidates[(row["model_name"], row["serving_label"], row["serving_value"])]
            if len(matching) == 1:
                row["deployment"] = next(iter(matching))

    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["model_name"], row["deployment"], row["serving_label"], row["serving_value"])
        scope = grouped.setdefault(
            key,
            {
                "namespace": namespace,
                "model_name": row["model_name"],
                "deployment": row["deployment"],
                "serving_label": row["serving_label"] or None,
                "serving_value": row["serving_value"] or None,
                "pod_names": set(),
                "job_values": set(),
                "instance_values": set(),
                "engine_values": set(),
                "source_values": set(),
            },
        )
        scope["pod_names"].add(row["pod"])
        for field, target in (("job", "job_values"), ("instance", "instance_values"), ("engine", "engine_values"), ("prometheus", "source_values")):
            if row[field]:
                scope[target].add(row[field])

    discovered: list[dict[str, Any]] = []
    for raw_scope in grouped.values():
        pod_names = sorted(raw_scope["pod_names"])
        override = _override_for(namespace, raw_scope["model_name"], raw_scope["serving_value"], pod_names)
        deployment = raw_scope["deployment"] or (override or {}).get("deployment", "")
        resolved = bool(deployment and pod_names and (raw_scope["deployment"] or override))
        value = {
            **raw_scope,
            "deployment": deployment,
            "deployment_resolved": resolved,
            "deployment_source": "kubernetes_owner_reference" if raw_scope["deployment"] else ((override or {}).get("source") or "unresolved"),
            "pod_names": pod_names,
            "job_values": sorted(raw_scope["job_values"]),
            "instance_values": sorted(raw_scope["instance_values"]),
            "engine_values": sorted(raw_scope["engine_values"]),
            "source_values": sorted(raw_scope["source_values"]),
            "duplicate_scrape_paths": len(raw_scope["job_values"]) > 1 or len(raw_scope["source_values"]) > 1,
            "pod_regex": (override or {}).get("pod_regex"),
        }
        # Sets are discovery internals and do not enter the browser response.
        for key in ("job_values", "instance_values", "engine_values", "source_values"):
            value[key] = list(value[key])
        value["model_id"] = sign_scope(value) if resolved else None
        discovered.append(value)

    discovered.sort(key=lambda item: (item["model_name"].casefold(), item["deployment"].casefold()))
    warnings: list[str] = []
    if len(series) >= settings.max_discovery_series:
        warnings.append("Model discovery reached its series limit; narrow the date range.")
    if not series:
        warnings.append("No vLLM series were retained in this namespace and date range.")
    if any(item["duplicate_scrape_paths"] for item in discovered):
        warnings.append("Multiple scrape jobs or sources were observed; select one job before querying to avoid duplicate series.")
    if metadata_warning:
        warnings.append(metadata_warning)
    if any(not item["deployment_resolved"] for item in discovered):
        warnings.append("Some model series lack verified Deployment ownership and are shown as unresolved.")
    return {
        "namespace": namespace,
        "models": discovered,
        "series_discovered": len(series),
        "state": "available" if discovered else "no_observations",
        "warnings": warnings,
    }
