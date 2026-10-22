"""Four-input, per-pod capacity review gate and its PromQL inputs."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .promql import selector
from .registry import HISTOGRAM_COUNT_METRICS

POLICY_VERSION = "four-metric-review-gate-v1"
DEFAULT_POLICY: dict[str, Any] = {
    "ttft_target_seconds": 3.0,
    "tpot_target_seconds_per_token": 0.15,
    "waiting_target_requests": 5.0,
    "kv_target_fraction": 0.90,
    "latency_window": "5m",
    "queue_window": "5m",
    "hold_seconds": 300,
    "evaluation_step_seconds": 30,
    "ttft_min_observations": 30,
    "tpot_min_observations": 30,
    "max_sample_age_seconds": 60,
}

INPUT_METRICS = (
    "ttft", "ttft_observations", "ttft_age",
    "tpot", "tpot_observations", "tpot_age",
    "waiting", "waiting_age", "kv", "kv_age",
)


def policy_id(policy: dict[str, Any]) -> str:
    payload = json.dumps({"version": POLICY_VERSION, **policy}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_gate_queries(
    scope: dict[str, Any], policy: dict[str, Any], selected_filters: dict[str, str | None]
) -> dict[str, str]:
    """Build fixed-P95 and per-pod expressions using trusted label names."""
    group = "namespace, model_name, pod"

    def raw(name: str) -> str:
        return selector("vllm:" + name, scope, optional=selected_filters)

    def histogram(metric_name: str, window: str) -> str:
        bucket = raw(metric_name + "_bucket")
        return (
            "histogram_quantile(0.95, "
            f"sum by (le, {group}) (rate({bucket}[{window}])))"
        )

    def count(metric_name: str, window: str) -> str:
        count_name = HISTOGRAM_COUNT_METRICS[metric_name]
        return f"sum by ({group}) (increase({raw(count_name)}[{window}]))"

    def age(metric_name: str, *, bucket: bool = False) -> str:
        selected = raw(metric_name + "_bucket" if bucket else metric_name)
        return f"max by ({group}) (time() - timestamp({selected}))"

    return {
        "ttft": histogram("time_to_first_token_seconds", policy["latency_window"]),
        "ttft_observations": count("time_to_first_token_seconds", policy["latency_window"]),
        "ttft_age": age("time_to_first_token_seconds", bucket=True),
        "tpot": histogram("request_time_per_output_token_seconds", policy["latency_window"]),
        "tpot_observations": count("request_time_per_output_token_seconds", policy["latency_window"]),
        "tpot_age": age("request_time_per_output_token_seconds", bucket=True),
        "waiting": f"sum by ({group}) (avg_over_time({raw('num_requests_waiting')}[{policy['queue_window']}]))",
        "waiting_age": age("num_requests_waiting"),
        "kv": f"max by ({group}) ({raw('kv_cache_usage_perc')})",
        "kv_age": age("kv_cache_usage_perc"),
    }


def _round_step(value: float, step: int) -> int:
    return int(round(value / step) * step)


def _input_maps(inputs: dict[str, dict[str, Any]], step: int) -> dict[str, dict[str, dict[int, float]]]:
    maps: dict[str, dict[str, dict[int, float]]] = {}
    for name in INPUT_METRICS:
        by_pod: dict[str, dict[int, float]] = {}
        for series in inputs.get(name, {}).get("series", []):
            pod = str(series.get("labels", {}).get("pod") or "")
            if not pod:
                continue
            target = by_pod.setdefault(pod, {})
            for point in series.get("points", []):
                value = point.get("value")
                if value is None:
                    continue
                stamp = _round_step(float(point["timestamp"]), step)
                target[stamp] = float(value)
        maps[name] = by_pod
    return maps


def evaluate_workload(
    *,
    deployment: str,
    model_name: str,
    expected_pods: list[str],
    inputs: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    start: float,
    end: float,
) -> dict[str, Any]:
    """Evaluate one workload's inputs on a fixed grid, independently per pod."""
    step = int(policy["evaluation_step_seconds"])
    first_visible = math.ceil(start / step) * step
    last_visible = math.floor(end / step) * step
    maps = _input_maps(inputs, step)
    observed_pods = {pod for values in maps.values() for pod in values}
    pods = sorted(set(expected_pods) | observed_pods)
    results: list[dict[str, Any]] = []

    for pod in pods:
        timestamps = sorted({stamp for values in maps.values() for stamp in values.get(pod, {})})
        if not timestamps or first_visible > last_visible:
            source_reasons = [
                f"{name}: {capture.get('reason') or capture.get('state')}"
                for name, capture in inputs.items()
                if capture.get("state") not in {None, "valid"}
            ]
            results.append({
                "pod": pod, "state": "insufficient_data", "evaluations": [],
                "reason": "; ".join(source_reasons) or "No four-metric gate observations are available for this pod in the selected range.",
            })
            continue

        first_pod = timestamps[0]
        # A pod is only evaluated from its first observed evidence; earlier pods
        # in the warm-up preserve hold state at the visible boundary.
        eval_start = first_pod
        eval_start = math.ceil(eval_start / step) * step
        candidate_start: int | None = None
        previous_candidate = False
        previous_timestamp: int | None = None
        visible: list[dict[str, Any]] = []

        for timestamp in range(eval_start, last_visible + 1, step):
            raw_values = {
                key: maps[key].get(pod, {}).get(timestamp)
                for key in INPUT_METRICS
            }
            evidence = {
                "ttft": {"value": raw_values["ttft"], "observations": raw_values["ttft_observations"], "age_seconds": raw_values["ttft_age"]},
                "tpot": {"value": raw_values["tpot"], "observations": raw_values["tpot_observations"], "age_seconds": raw_values["tpot_age"]},
                "waiting": {"value": raw_values["waiting"], "age_seconds": raw_values["waiting_age"]},
                "kv": {"value": raw_values["kv"], "age_seconds": raw_values["kv_age"]},
            }
            reasons: list[str] = []
            valid = True
            for name, capture in inputs.items():
                if capture.get("state") not in {None, "valid"}:
                    valid = False
                    reason = capture.get("reason") or capture.get("state")
                    reasons.append(f"{name.upper()} input: {reason}")
            for key, minimum in (("ttft", int(policy["ttft_min_observations"])), ("tpot", int(policy["tpot_min_observations"]))):
                item = evidence[key]
                if item["value"] is None:
                    valid = False
                    reasons.append(f"{key.upper()} has no histogram value")
                if item["observations"] is None or item["observations"] < minimum:
                    valid = False
                    reasons.append(f"{key.upper()} observations are below the {minimum} sample floor")
                if item["age_seconds"] is None or item["age_seconds"] > policy["max_sample_age_seconds"]:
                    valid = False
                    reasons.append(f"{key.upper()} evidence is missing or stale")
                if item["value"] is not None and item["value"] < 0:
                    valid = False
                    reasons.append(f"{key.upper()} value is outside its valid range")

            for key in ("waiting", "kv"):
                item = evidence[key]
                if item["value"] is None:
                    valid = False
                    reasons.append(f"{key.upper()} has no gauge value")
                if item["age_seconds"] is None or item["age_seconds"] > policy["max_sample_age_seconds"]:
                    valid = False
                    reasons.append(f"{key.upper()} evidence is missing or stale")
                if item["value"] is not None and item["value"] < 0:
                    valid = False
                    reasons.append(f"{key.upper()} value is outside its valid range")
            if evidence["kv"]["value"] is not None and evidence["kv"]["value"] > 1:
                valid = False
                reasons.append("KV occupancy is above 1.0")

            latency_breach: bool | None = None
            capacity_pressure: bool | None = None
            candidate: bool | None = None
            confirmed_at: int | None = None
            remaining = None
            if valid:
                latency_breach = (
                    evidence["ttft"]["value"] > policy["ttft_target_seconds"]
                    or evidence["tpot"]["value"] > policy["tpot_target_seconds_per_token"]
                )
                capacity_pressure = (
                    evidence["waiting"]["value"] > policy["waiting_target_requests"]
                    or evidence["kv"]["value"] > policy["kv_target_fraction"]
                )
                candidate = latency_breach and capacity_pressure

            contiguous = previous_timestamp is not None and timestamp - previous_timestamp == step
            if not valid:
                state = "insufficient_data"
                candidate_start = None
                previous_candidate = False
            elif candidate:
                if not contiguous or not previous_candidate:
                    candidate_start = timestamp
                elapsed = timestamp - (candidate_start if candidate_start is not None else timestamp)
                if elapsed >= int(policy["hold_seconds"]):
                    state = "scale_out_review"
                    confirmed_at = timestamp
                    remaining = 0
                else:
                    state = "review_pending"
                    remaining = max(0, int(policy["hold_seconds"]) - elapsed)
                previous_candidate = True
            else:
                if latency_breach and not capacity_pressure:
                    state = "latency_investigation"
                elif capacity_pressure and not latency_breach:
                    state = "pressure_warning"
                else:
                    state = "within_targets"
                candidate_start = None
                previous_candidate = False

            row = {
                "timestamp": timestamp, "state": state, "valid": valid,
                "inputs": evidence, "latency_breach": latency_breach,
                "capacity_pressure": capacity_pressure, "candidate": candidate,
                "candidate_start": candidate_start,
                "confirmed_at": confirmed_at,
                "remaining_hold_seconds": remaining,
                "reasons": reasons,
            }
            if timestamp >= first_visible:
                visible.append(row)
            previous_timestamp = timestamp

        latest = visible[-1] if visible else None
        results.append({
            "pod": pod,
            "state": latest["state"] if latest else "insufficient_data",
            "latest": latest,
            "evaluations": visible,
            "reason": None if latest is None else "; ".join(latest["reasons"]) or None,
        })

    return {
        "deployment": deployment,
        "model_name": model_name,
        "expected_pod_count": len(expected_pods),
        "observed_pod_count": len(observed_pods),
        "pods": results,
    }
