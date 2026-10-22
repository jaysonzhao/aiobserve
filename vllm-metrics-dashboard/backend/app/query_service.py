"""Bounded, scoped metric queries and immutable snapshot construction."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .discovery import discover_workloads
from .monitoring import MonitoringError, monitoring
from .promql import QueryUnavailable, compile_query, metric_selector_for_capability, promql_string
from .registry import BY_ID, DEFAULT_QUERY_IDS, HISTOGRAM_COUNT_METRICS, METRICS
from .scale_gate import (
    DEFAULT_POLICY as DEFAULT_SCALE_GATE_POLICY,
    INPUT_METRICS as SCALE_GATE_INPUTS,
    build_gate_queries,
    evaluate_workload,
    policy_id as scale_gate_policy_id,
)
from .settings import settings
from .time_utils import daily_intervals


def _finite_value(raw: str) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _matrix_series(result: dict[str, Any], deployment: str, model_name: str) -> dict[str, Any]:
    points = []
    for timestamp, raw in result.get("values", []):
        points.append({"timestamp": float(timestamp), "raw": str(raw), "value": _finite_value(str(raw))})
    labels = {key: value for key, value in result.get("metric", {}).items()}
    return {"labels": labels, "deployment": deployment, "model_name": model_name, "points": points}


def _vector_series(result: dict[str, Any], deployment: str, model_name: str) -> dict[str, Any]:
    timestamp, raw = result.get("value", [None, "NaN"])
    return {
        "labels": {key: value for key, value in result.get("metric", {}).items()},
        "deployment": deployment,
        "model_name": model_name,
        "timestamp": float(timestamp) if timestamp is not None else None,
        "raw": str(raw),
        "value": _finite_value(str(raw)),
    }


def _coverage(series: list[dict[str, Any]], step: int, end: datetime) -> dict[str, Any]:
    all_points = [point for item in series for point in item.get("points", []) if point.get("value") is not None]
    if not all_points:
        return {"points": 0, "series": len(series), "latest_timestamp": None, "gaps": 0, "freshness_seconds": None}
    timestamps = sorted({point["timestamp"] for point in all_points})
    gaps = sum(1 for left, right in zip(timestamps, timestamps[1:]) if right - left > step * 1.5)
    latest = max(timestamps)
    return {
        "points": len(all_points),
        "series": len(series),
        "latest_timestamp": latest,
        "gaps": gaps,
        "freshness_seconds": max(0, int(end.timestamp() - latest)),
    }


async def _scale_gate_inputs(
    scope: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
    policy: dict[str, Any],
    selected_filters: dict[str, str | None],
) -> dict[str, Any]:
    """Capture fixed-policy, per-pod gate inputs on its own evaluation grid."""
    step = int(policy["evaluation_step_seconds"])
    latency_window = int(str(policy["latency_window"]).removesuffix("m")) * 60
    queue_window = int(str(policy["queue_window"]).removesuffix("m")) * 60
    warmup = max(latency_window, queue_window) + int(policy["hold_seconds"])
    first = math.floor((start.timestamp() - warmup) / step) * step
    last = math.floor(end.timestamp() / step) * step
    point_count = max(0, int((last - first) / step) + 1)
    empty = {
        name: {"state": "not_queried", "reason": None, "query": None, "series": []}
        for name in SCALE_GATE_INPUTS
    }
    if point_count == 0:
        reason = "The selected range has no complete scale-gate evaluation points."
        return {"inputs": {name: {**item, "state": "no_observations", "reason": reason} for name, item in empty.items()}, "step_seconds": step, "warmup_seconds": warmup, "range_start": first, "range_end": last}
    if point_count > settings.max_scale_gate_points:
        reason = f"The fixed {step}-second gate grid exceeds the configured {settings.max_scale_gate_points:,}-point limit. Narrow the selected range or choose a coarser evaluation step."
        return {"inputs": {name: {**item, "state": "query_error", "reason": reason} for name, item in empty.items()}, "step_seconds": step, "warmup_seconds": warmup, "range_start": first, "range_end": last}

    try:
        queries = build_gate_queries(scope, policy, selected_filters)
    except (QueryUnavailable, ValueError, AssertionError) as exc:
        reason = str(exc)
        return {
            "inputs": {name: {**item, "state": "query_error", "reason": reason} for name, item in empty.items()},
            "step_seconds": step, "warmup_seconds": warmup, "range_start": first, "range_end": last,
        }

    async def run(name: str) -> tuple[str, dict[str, Any]]:
        query = queries[name]
        try:
            payload = await monitoring.query_range(query, start=first, end=last, step=step)
            raw_results = payload.get("data", {}).get("result", [])
            if len(raw_results) > settings.max_series_per_chart:
                return name, {"state": "query_error", "reason": "Gate input exceeded the configured series limit.", "query": query, "series": []}
            series = [_matrix_series(item, scope["deployment"], scope["model_name"]) for item in raw_results]
            if any(len(item["points"]) > settings.max_scale_gate_points for item in series):
                return name, {"state": "query_error", "reason": "Gate input exceeded the configured point limit.", "query": query, "series": []}
            state = "valid" if series else "no_observations"
            reason = None if series else "No samples were returned for this gate input in the selected range."
            return name, {"state": state, "reason": reason, "query": query, "series": series}
        except MonitoringError as exc:
            return name, {"state": exc.state, "reason": str(exc), "query": query, "series": []}
        except (QueryUnavailable, ValueError, AssertionError) as exc:
            return name, {"state": "query_error", "reason": str(exc), "query": query, "series": []}

    values = await asyncio.gather(*(run(name) for name in SCALE_GATE_INPUTS))
    return {
        "inputs": dict(values), "step_seconds": step, "warmup_seconds": warmup,
        "range_start": first, "range_end": last,
    }


def _empty_snapshot(
    *, namespace: str, model_id: str, identity: dict[str, str] | None, start: datetime, end: datetime,
    zone: Any, ids: list[int], view_scope: str, window: str, percentile: int,
    selected_filters: dict[str, str | None], revision: str | None, discovery: dict[str, Any],
    scale_gate_policy: dict[str, Any],
) -> dict[str, Any]:
    from .time_utils import choose_step

    reason = "No matching vLLM observations were discovered in the selected namespace and date range."
    metrics: dict[str, dict[str, Any]] = {}
    for metric_id in ids:
        spec = BY_ID[metric_id]
        metrics[str(metric_id)] = {
            **spec.public("no_observations", reason),
            "summary_kind": "selected_range_percentile" if spec.kind == "histogram" else "end_value",
            "summary_unit": spec.unit,
            "scope": view_scope,
            "series": [], "summary": [], "observations": [], "coverage": [], "queries": [],
            "warnings": [], "scope_states": [], "selected_range_total": [],
            "scale_gate_series": [], "scale_gate_observations": [],
            "scale_gate_queries": [], "scale_gate_reasons": [],
        }
    models = []
    if identity:
        models.append({
            "deployment": identity["deployment"], "model_name": identity["model_name"],
            "serving_label": identity["serving_label"] or None,
            "serving_value": identity["serving_value"] or None, "pods": [],
        })
    return {
        "filters": {
            "namespace": namespace, "model_id": model_id, "models": models, "selectors": selected_filters,
            "range": {
                "start_utc": start.isoformat().replace("+00:00", "Z"),
                "end_utc": end.isoformat().replace("+00:00", "Z"),
                "start_local": start.astimezone(zone).isoformat(),
                "end_local": end.astimezone(zone).isoformat(),
                "timezone": str(zone), "step_seconds": choose_step(start, end),
                "window": window, "percentile": percentile, "scope": view_scope,
            },
        },
        "registry_version": "1.1.0",
        "scale_gate": {
            "policy_id": scale_gate_policy_id(scale_gate_policy),
            "policy_version": "four-metric-review-gate-v1",
            "policy": scale_gate_policy,
            "scope": "per_pod",
            "inputs": [],
            "workloads": [],
            "warmup_available": False,
        },
        "adapter": {"metric_prefix": "vllm:", "namespace_label": "namespace", "model_label": "model_name", "pod_label": "pod", "instance_label": "instance"},
        "metrics": metrics,
        "daily_tokens": {"state": "no_observations", "rows": [], "warnings": ["No local-day token observations were discovered for this selection."]},
        "warnings": discovery.get("warnings", []), "revision": revision,
        "created_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "expires_in_seconds": settings.snapshot_ttl_seconds,
        "evidence_note": "Metrics explain observed workload behavior; they do not automatically scale or purchase resources.",
    }


def _group_labels(view_scope: str) -> str:
    return "namespace, model_name" if view_scope == "service" else "namespace, model_name, pod, instance"


def _count_query(
    spec_metric: str,
    scope: dict[str, Any],
    view_scope: str,
    duration_seconds: int,
    selected_filters: dict[str, str | None] | None = None,
) -> str:
    count_name = HISTOGRAM_COUNT_METRICS[spec_metric]
    metric = "vllm:" + count_name
    from .promql import selector

    scoped = selector(metric, scope, optional=selected_filters)
    return f"sum by ({_group_labels(view_scope)}) (increase({scoped}[{duration_seconds}s]))"


async def _availability(spec: Any, scope: dict[str, Any], start: datetime, end: datetime) -> str:
    try:
        metric_names = (spec.metric, *spec.metric_aliases)
        selectors = [
            metric_selector_for_capability(replace(spec, metric=name), scope)
            for name in metric_names if name
        ] or [metric_selector_for_capability(spec, scope)]
        for exact in selectors:
            found = await monitoring.series(exact, start=start.timestamp(), end=end.timestamp(), limit=1)
            if found:
                return "exported"
        now = datetime.now(UTC).timestamp()
        for exact in selectors:
            recent = await monitoring.series(exact, start=now - 30 * 86400, end=now, limit=1)
            if recent:
                return "exported"
        return "not_exported"
    except MonitoringError as exc:
        if exc.state == "not_authorized":
            return "not_authorized"
        return "query_error"
    except (QueryUnavailable, ValueError):
        return "unknown"


async def _run_one(
    spec: Any,
    scope: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
    step: int,
    view_scope: str,
    window: str,
    percentile: int,
    selected_filters: dict[str, str | None],
    http_options: dict[str, str],
) -> dict[str, Any]:
    base = {
        "id": spec.id,
        "name": spec.name,
        "unit": spec.unit,
        "series": [],
        "summary": [],
        "observations": [],
        "coverage": {"points": 0, "series": 0, "latest_timestamp": None, "gaps": 0, "freshness_seconds": None},
        "query": None,
        "summary_query": None,
        "warnings": [],
        "scale_gate_series": [],
        "scale_gate_observations": [],
        "scale_gate_query": None,
        "scale_gate_observation_query": None,
        "scale_gate_reason": None,
    }
    try:
        query = compile_query(
            spec,
            scope,
            view_scope=view_scope,
            window=window,
            percentile=percentile,
            http_handler_regex=http_options["handler_regex"],
            http_method_label=http_options["method_label"],
            http_handler_label=http_options["handler_label"],
            http_status_label=http_options["status_label"],
            selected_filters=selected_filters,
        )
        range_payload = await monitoring.query_range(query, start=start.timestamp(), end=end.timestamp(), step=step)
        raw_results = range_payload.get("data", {}).get("result", [])
        effective_spec = spec
        if not raw_results and spec.metric_aliases:
            for alias in spec.metric_aliases:
                candidate = replace(spec, metric=alias)
                candidate_query = compile_query(
                    candidate,
                    scope,
                    view_scope=view_scope,
                    window=window,
                    percentile=percentile,
                    http_handler_regex=http_options["handler_regex"],
                    http_method_label=http_options["method_label"],
                    http_handler_label=http_options["handler_label"],
                    http_status_label=http_options["status_label"],
                    selected_filters=selected_filters,
                )
                candidate_payload = await monitoring.query_range(
                    candidate_query, start=start.timestamp(), end=end.timestamp(), step=step
                )
                candidate_results = candidate_payload.get("data", {}).get("result", [])
                if candidate_results:
                    effective_spec = candidate
                    query = candidate_query
                    range_payload = candidate_payload
                    raw_results = candidate_results
                    break
        if len(raw_results) > settings.max_series_per_chart:
            return {**base, "query": query, "state": "query_error", "reason": "Series limit exceeded; narrow the selected workload or pod filter."}
        series = [_matrix_series(item, scope["deployment"], scope["model_name"]) for item in raw_results]
        if any(len(item["points"]) > settings.max_points_per_series for item in series):
            return {**base, "query": query, "state": "query_error", "reason": "Point limit exceeded; use a coarser time range."}

        summary_query = compile_query(
            effective_spec,
            scope,
            view_scope=view_scope,
            window=window,
            percentile=percentile,
            selected_range_seconds=max(1, int((end - start).total_seconds())),
            summary=effective_spec.kind in {"histogram", "counter_rate"},
            http_handler_regex=http_options["handler_regex"],
            http_method_label=http_options["method_label"],
            http_handler_label=http_options["handler_label"],
            http_status_label=http_options["status_label"],
            selected_filters=selected_filters,
        )
        if effective_spec.kind == "counter_increase":
            # The card keeps its specified rolling window. The full-selection
            # increase is exposed separately as a companion statistic.
            summary_query = query
        summary_payload = await monitoring.query(summary_query, at=end.timestamp())
        summary_results = summary_payload.get("data", {}).get("result", [])
        summary = [_vector_series(item, scope["deployment"], scope["model_name"]) for item in summary_results]

        selected_range_total: list[dict[str, Any]] = []
        if effective_spec.kind == "counter_increase":
            range_total_query = compile_query(
                effective_spec,
                scope,
                view_scope=view_scope,
                window=window,
                percentile=percentile,
                selected_range_seconds=max(1, int((end - start).total_seconds())),
                summary=True,
                http_handler_regex=http_options["handler_regex"],
                http_method_label=http_options["method_label"],
                http_handler_label=http_options["handler_label"],
                http_status_label=http_options["status_label"],
                selected_filters=selected_filters,
            )
            total_payload = await monitoring.query(range_total_query, at=end.timestamp())
            selected_range_total = [
                _vector_series(item, scope["deployment"], scope["model_name"])
                for item in total_payload.get("data", {}).get("result", [])
            ]
            base["selected_range_query"] = range_total_query

        observations: list[dict[str, Any]] = []
        observation_query = None
        if effective_spec.kind == "histogram" and effective_spec.metric in HISTOGRAM_COUNT_METRICS:
            observation_query = _count_query(
                effective_spec.metric, scope, view_scope, max(1, int((end - start).total_seconds())), selected_filters
            )
            observation_payload = await monitoring.query(observation_query, at=end.timestamp())
            observations = [
                _vector_series(item, scope["deployment"], scope["model_name"])
                for item in observation_payload.get("data", {}).get("result", [])
            ]

        coverage = _coverage(series, step, end)
        warnings = list(range_payload.get("warnings", [])) + list(range_payload.get("infos", []))
        if effective_spec.kind == "histogram" and not observations:
            warnings.append("No matching histogram observations were returned for the selected interval.")
        if effective_spec.kind == "histogram" and any((item.get("value") or 0) < 30 for item in observations):
            warnings.append("Some histogram series have fewer than 30 observations; percentile estimates are low-volume.")
        if spec.id == 22:
            warnings.append("Combined input/output token rate is usage accounting, not a uniform compute-capacity unit.")
        if spec.id == 13:
            warnings.append("This is a conservative upper bound; per-engine peaks can occur at different times.")
        if spec.id in {34, 35}:
            warnings.append("Rolling 24-hour increase; use Daily tokens for Hong Kong calendar-day estimates.")

        if not series:
            availability = await _availability(effective_spec, scope, start, end)
            if availability == "not_exported":
                state, reason = "not_exported", "No metric series were found in the selected range or recent 30-day retention."
            elif availability == "not_authorized":
                state, reason = "not_authorized", "Monitoring metadata access was denied."
            elif availability == "query_error":
                state, reason = "query_error", "Could not determine whether this metric is exported."
            else:
                state, reason = "no_observations", "No observations were returned for this interval; missing data is not zero."
            return {
                **base,
                "query": query,
                "summary_query": summary_query,
                "observation_query": observation_query,
                "state": state,
                "reason": reason,
                "summary": summary,
                "observations": observations,
                "selected_range_total": selected_range_total,
                "coverage": coverage,
                "warnings": warnings,
            }

        if coverage["points"] == 0:
            return {
                **base,
                "query": query,
                "summary_query": summary_query,
                "observation_query": observation_query,
                "state": "no_observations",
                "reason": "The returned series contained no finite samples for this interval.",
                "series": series,
                "summary": summary,
                "observations": observations,
                "selected_range_total": selected_range_total,
                "coverage": coverage,
                "warnings": warnings,
            }

        latest = coverage.get("latest_timestamp")
        stale_after = max(300, settings.scrape_interval_seconds * 3)
        state = "valid"
        if coverage["points"] and all(point.get("value") == 0 for item in series for point in item["points"] if point.get("value") is not None):
            state = "valid_zero"
        if latest is not None and end.timestamp() - latest > stale_after:
            state = "stale"
            warnings.append("Latest sample is older than the configured freshness window.")
        return {
            **base,
            "query": query,
            "summary_query": summary_query,
            "observation_query": observation_query,
            "state": state,
            "reason": None,
            "series": series,
            "summary": summary,
            "observations": observations,
            "selected_range_total": selected_range_total,
            "coverage": coverage,
            "warnings": warnings,
        }
    except QueryUnavailable as exc:
        state = "configuration_required" if "configure" in str(exc).lower() else "unavailable"
        return {**base, "state": state, "reason": str(exc)}
    except MonitoringError as exc:
        return {**base, "state": exc.state, "reason": str(exc)}
    except (ValueError, AssertionError) as exc:
        return {**base, "state": "unavailable", "reason": str(exc)}


async def _daily_tokens(
    scopes: list[dict[str, Any]], start: datetime, end: datetime, zone: Any, view_scope: str, http_options: dict[str, str], selected_filters: dict[str, str | None]
) -> dict[str, Any]:
    from .promql import compile_query

    intervals = daily_intervals(start, end, zone)
    rows: list[dict[str, Any]] = []
    requests: list[tuple[dict[str, Any], dict[str, Any], str, datetime]] = []
    for interval in intervals:
        interval_end = datetime.fromisoformat(str(interval["end_utc"]).replace("Z", "+00:00"))
        duration = int(interval["duration_seconds"])
        for scope in scopes:
            for metric_id in (34, 35):
                spec = BY_ID[metric_id]
                try:
                    query = compile_query(
                        spec,
                        scope,
                        view_scope="service",
                        selected_range_seconds=duration,
                        summary=True,
                        http_handler_regex=http_options["handler_regex"],
                        http_method_label=http_options["method_label"],
                        http_handler_label=http_options["handler_label"],
                        http_status_label=http_options["status_label"],
                        selected_filters=selected_filters,
                    )
                    requests.append((interval, scope, query, interval_end))
                except (QueryUnavailable, ValueError) as exc:
                    rows.append({
                        "interval": interval,
                        "deployment": scope["deployment"],
                        "metric_id": metric_id,
                        "metric": spec.name,
                        "unit": spec.unit,
                        "state": "unavailable",
                        "reason": str(exc),
                        "estimate": None,
                        "query": None,
                    })

    async def run_one(item: tuple[dict[str, Any], dict[str, Any], str, datetime]) -> list[dict[str, Any]]:
        interval, scope, query, at = item
        metric_id = 34 if "prompt_tokens_total" in query else 35
        spec = BY_ID[metric_id]
        try:
            payload = await monitoring.query(query, at=at.timestamp())
            values = payload.get("data", {}).get("result", [])
            total = sum(value for item in values if (value := _finite_value(item.get("value", [None, "NaN"])[1])) is not None)
            state = "valid" if values else "no_observations"
            return [{
                "interval": interval,
                "deployment": scope["deployment"],
                "metric_id": metric_id,
                "metric": spec.name,
                "unit": spec.unit,
                "state": state,
                "reason": None if values else "No counter samples for this local-day interval; not zero.",
                "estimate": total if values else None,
                "query": query,
            }]
        except MonitoringError as exc:
            return [{
                "interval": interval,
                "deployment": scope["deployment"],
                "metric_id": metric_id,
                "metric": spec.name,
                "unit": spec.unit,
                "state": exc.state,
                "reason": str(exc),
                "estimate": None,
                "query": query,
            }]

    if len(requests) > 256:
        return {"state": "query_error", "rows": [], "warnings": ["Daily token request exceeds the 31-day workload budget."]}
    completed = await asyncio.gather(*(run_one(item) for item in requests))
    for result in completed:
        rows.extend(result)
    rows.sort(key=lambda row: (row["interval"]["local_date"], row["deployment"], row["metric_id"]))
    return {
        "state": "available" if any(row["state"] in {"valid", "valid_zero"} for row in rows) else "no_observations",
        "rows": rows,
        "warnings": ["Daily totals estimate Prometheus counter increases; they are not billing values."],
    }


async def build_snapshot(
    *,
    namespace: str,
    model_id: str,
    start: datetime,
    end: datetime,
    zone: Any,
    metric_ids: list[int] | None,
    view_scope: str,
    window: str,
    percentile: int,
    scale_gate_policy: dict[str, Any] | None,
    selected_filters: dict[str, str | None],
    revision: str | None,
) -> dict[str, Any]:
    from .scopes import decode_scope_id

    gate_policy = {**DEFAULT_SCALE_GATE_POLICY, **(scale_gate_policy or {})}
    discovery = await discover_workloads(namespace, start=start, end=end)
    resolved = [scope for scope in discovery["models"] if scope.get("deployment_resolved")]
    ids = list(dict.fromkeys(metric_ids or DEFAULT_QUERY_IDS))
    if not ids or any(metric_id not in BY_ID for metric_id in ids):
        raise ValueError("Metric IDs must be selected from the 41-entry catalog.")
    if len(ids) > len(METRICS):
        raise ValueError("Too many metric IDs were requested.")
    identity: dict[str, str] | None = None
    if model_id == "all":
        selected_scopes = resolved
    else:
        identity = decode_scope_id(model_id)
        if identity["namespace"] != namespace:
            raise ValueError("The model identity is outside the requested namespace.")
        selected_scopes = [
            scope for scope in discovery["models"]
            if scope.get("deployment_resolved") and scope.get("model_id") == model_id
            and scope.get("namespace") == identity["namespace"]
        ]
        if not selected_scopes:
            return _empty_snapshot(
                namespace=namespace, model_id=model_id, identity=identity, start=start, end=end, zone=zone,
                ids=ids, view_scope=view_scope, window=window, percentile=percentile,
                selected_filters=selected_filters, revision=revision, discovery=discovery,
                scale_gate_policy=gate_policy,
            )
    if not selected_scopes and model_id == "all":
        return _empty_snapshot(
            namespace=namespace, model_id=model_id, identity=None, start=start, end=end, zone=zone,
            ids=ids, view_scope=view_scope, window=window, percentile=percentile,
            selected_filters=selected_filters, revision=revision, discovery=discovery,
            scale_gate_policy=gate_policy,
        )
    for key, value in selected_filters.items():
        if not value:
            continue
        def values_for(scope: dict[str, Any]) -> list[str]:
            if key == "pod":
                return scope.get("pod_names", [])
            if key == "prometheus":
                return scope.get("source_values", [])
            return scope.get(f"{key}_values", [])
        if not any(value in values_for(scope) for scope in selected_scopes):
            raise ValueError(f"Selected {key} is not part of the resolved workload.")
        selected_scopes = [scope for scope in selected_scopes if value in values_for(scope)]
    if not selected_scopes:
        raise ValueError("The selected filters do not match a resolved workload.")

    if not selected_filters.get("job"):
        duplicate = next((scope for scope in selected_scopes if len(scope.get("job_values", [])) > 1), None)
        if duplicate:
            raise ValueError("Select one scrape job to avoid duplicate paths for this workload.")
    if not selected_filters.get("prometheus"):
        duplicate_source = next((scope for scope in selected_scopes if len(scope.get("source_values", [])) > 1), None)
        if duplicate_source:
            raise ValueError("Select one monitoring source to avoid duplicate scrape paths for this workload.")

    gate_captures = await asyncio.gather(*(
        _scale_gate_inputs(
            scope, start=start, end=end, policy=gate_policy, selected_filters=selected_filters
        )
        for scope in selected_scopes
    ))
    gate_workloads = []
    for scope, capture in zip(selected_scopes, gate_captures, strict=True):
        evaluated = evaluate_workload(
            deployment=scope["deployment"], model_name=scope["model_name"],
            expected_pods=[selected_filters["pod"]] if selected_filters.get("pod") else scope.get("pod_names", []),
            inputs=capture["inputs"],
            policy=gate_policy, start=start.timestamp(), end=end.timestamp(),
        )
        gate_workloads.append({
            **evaluated,
            "inputs": capture["inputs"],
            "step_seconds": capture["step_seconds"],
            "warmup_seconds": capture["warmup_seconds"],
            "range_start": capture["range_start"],
            "range_end": capture["range_end"],
        })
    duration = (end - start).total_seconds()
    from .time_utils import choose_step

    step = choose_step(start, end)
    http_options = {
        "handler_regex": settings.http_handler_regex,
        "method_label": settings.http_method_label,
        "handler_label": settings.http_handler_label,
        "status_label": settings.http_status_label,
    }
    tasks = [
        (spec, scope)
        for spec in (BY_ID[metric_id] for metric_id in ids)
        for scope in selected_scopes
    ]
    results = await asyncio.gather(*(
        _run_one(
            spec,
            scope,
            start=start,
            end=end,
            step=step,
            view_scope=view_scope,
            window=window,
            percentile=percentile,
            selected_filters=selected_filters,
            http_options=http_options,
        )
        for spec, scope in tasks
    ))

    metric_results: dict[str, dict[str, Any]] = {}
    for (spec, scope), result in zip(tasks, results, strict=True):
        entry = metric_results.setdefault(
            str(spec.id),
            {
                "id": spec.id,
                "name": spec.name,
                "category": spec.category,
                "unit": spec.unit,
                "kind": spec.kind,
                "family": spec.family,
                "aggregation": spec.aggregation,
                "window": spec.window if spec.window != "5m" else window,
                "summary_kind": (
                    "selected_range_percentile" if spec.kind == "histogram"
                    else "selected_range_increase" if spec.kind == "counter_rate"
                    else "selected_range_total" if spec.kind == "counter_increase"
                    else "end_value"
                ),
                "summary_unit": ("events" if spec.id == 18 else spec.unit.removesuffix("/s")) if spec.kind == "counter_rate" else spec.unit,
                "scope": view_scope,
                "description": spec.description,
                "state": result["state"],
                "reason": result.get("reason"),
                "series": [],
                "summary": [],
                "observations": [],
                "coverage": [],
                "queries": [],
                "warnings": [],
                "scope_states": [],
                "selected_range_total": [],
                "scale_gate_series": [],
                "scale_gate_observations": [],
                "scale_gate_queries": [],
                "scale_gate_reasons": [],
            },
        )
        entry["series"].extend(result.get("series", []))
        entry["summary"].extend(result.get("summary", []))
        entry["observations"].extend(result.get("observations", []))
        entry["coverage"].append({"deployment": scope["deployment"], **result.get("coverage", {})})
        entry["warnings"].extend(result.get("warnings", []))
        entry["selected_range_total"].extend(result.get("selected_range_total", []))
        entry["scale_gate_series"].extend(result.get("scale_gate_series", []))
        entry["scale_gate_observations"].extend(result.get("scale_gate_observations", []))
        if result.get("scale_gate_query") or result.get("scale_gate_observation_query"):
            entry["scale_gate_queries"].append({
                "deployment": scope["deployment"],
                "promql": result.get("scale_gate_query"),
                "observation_promql": result.get("scale_gate_observation_query"),
            })
        if result.get("scale_gate_reason"):
            entry["scale_gate_reasons"].append({
                "deployment": scope["deployment"],
                "reason": result["scale_gate_reason"],
            })
        entry["queries"].append({
            "deployment": scope["deployment"],
            "promql": result.get("query"),
            "summary_promql": result.get("summary_query"),
            "selected_range_promql": result.get("selected_range_query"),
            "observation_promql": result.get("observation_query"),
            "scale_gate_promql": result.get("scale_gate_query"),
            "scale_gate_observation_promql": result.get("scale_gate_observation_query"),
        })
        entry["scope_states"].append({
            "deployment": scope["deployment"],
            "state": result["state"],
            "reason": result.get("reason"),
        })
    for entry in metric_results.values():
        states = [item["state"] for item in entry["scope_states"]]
        if any(state in {"valid", "valid_zero", "stale"} for state in states):
            entry["state"] = "stale" if all(state == "stale" for state in states) else ("valid_zero" if all(state == "valid_zero" for state in states) else "valid")
            entry["reason"] = None
        elif states:
            entry["state"] = states[0] if len(set(states)) == 1 else "unavailable"
            entry["reason"] = "; ".join(sorted({item["reason"] for item in entry["scope_states"] if item.get("reason")})) or None
        entry["warnings"] = list(dict.fromkeys(entry["warnings"]))
        if entry["state"] == "valid" and any(state not in {"valid", "valid_zero"} for state in states):
            entry["warnings"].append("Some selected deployments had unavailable or empty results.")

    daily = await _daily_tokens(selected_scopes, start, end, zone, view_scope, http_options, selected_filters)
    frozen_range = {
        "start_utc": start.isoformat().replace("+00:00", "Z"),
        "end_utc": end.isoformat().replace("+00:00", "Z"),
        "start_local": start.astimezone(zone).isoformat(),
        "end_local": end.astimezone(zone).isoformat(),
        "timezone": str(zone),
        "step_seconds": step,
        "window": window,
        "percentile": percentile,
        "scope": view_scope,
    }
    snapshot = {
        "filters": {
            "namespace": namespace,
            "model_id": model_id,
            "models": [
                {
                    "deployment": scope["deployment"],
                    "model_name": scope["model_name"],
                    "serving_label": scope.get("serving_label"),
                    "serving_value": scope.get("serving_value"),
                    "pods": scope["pod_names"],
                }
                for scope in selected_scopes
            ],
            "range": frozen_range,
            "selectors": selected_filters,
        },
        "registry_version": "1.1.0",
        "scale_gate": {
            "policy_id": scale_gate_policy_id(gate_policy),
            "policy_version": "four-metric-review-gate-v1",
            "policy": gate_policy,
            "scope": "per_pod",
            "inputs": [
                {"deployment": item["deployment"], "model_name": item["model_name"], "inputs": item["inputs"]}
                for item in gate_workloads
            ],
            "workloads": gate_workloads,
            "warmup_available": True,
        },
        "adapter": {
            "metric_prefix": "vllm:",
            "namespace_label": "namespace",
            "model_label": "model_name",
            "pod_label": "pod",
            "instance_label": "instance",
            "deployment_identity_label": "llm_isvc_name when observed; physical Deployment from Kubernetes owner references",
        },
        "metrics": metric_results,
        "daily_tokens": daily,
        "warnings": list(discovery.get("warnings", [])) + list(daily.get("warnings", [])),
        "revision": revision,
        "created_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "expires_in_seconds": settings.snapshot_ttl_seconds,
        "evidence_note": "Metrics explain observed workload behavior; they do not automatically scale or purchase resources.",
    }
    return snapshot
