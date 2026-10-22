"""PromQL compiler. Only trusted metric/label names enter generated queries."""

from __future__ import annotations

import json
import re
from typing import Any

from .registry import MetricSpec

VALID_WINDOWS = {"1m", "2m", "5m", "10m", "15m", "30m", "1h", "24h"}
VALID_PERCENTILES = {50, 90, 95, 99}
POD_GROUP = ("namespace", "model_name", "pod", "instance")
SERVICE_GROUP = ("namespace", "model_name")
TRUSTED_LABEL_NAMES = {
    "namespace": "namespace",
    "model": "model_name",
    "pod": "pod",
    "instance": "instance",
    "job": "job",
    "engine": "engine",
    "finished_reason": "finished_reason",
    "prometheus": "prometheus",
}
TRUSTED_LABEL_VALUES = set(TRUSTED_LABEL_NAMES.values()) | {
    "llm_isvc_name",
    "inference_service",
    "deployment",
    "method",
    "http_method",
    "handler",
    "path",
    "route",
    "status",
    "status_code",
    "code",
}


class QueryUnavailable(ValueError):
    """The selected metric needs an unavailable optional capability."""


def promql_string(value: str) -> str:
    """Encode a label value as a PromQL string literal."""
    return json.dumps(value, ensure_ascii=False)


def _matcher(label: str, operator: str, value: str) -> str:
    if label not in TRUSTED_LABEL_VALUES:
        raise ValueError(f"untrusted label name: {label}")
    if operator not in {"=", "=~"}:
        raise ValueError("unsupported matcher")
    return f"{label}{operator}{promql_string(value)}"


def _safe_pod_regex(pods: list[str]) -> str:
    if not pods:
        raise QueryUnavailable("No verified pod ownership mapping is available for this scope.")
    if len(pods) > 200:
        raise QueryUnavailable("This scope has more than 200 pods; narrow the selected workload.")
    for pod in pods:
        if not pod or len(pod) > 253 or not re.fullmatch(r"[A-Za-z0-9_.-]+", pod):
            raise QueryUnavailable("A discovered pod name is invalid for a safe PromQL selector.")
    alternatives = "|".join(re.escape(pod) for pod in sorted(set(pods)))
    return f"^(?:{alternatives})$"


def selector(
    metric: str,
    scope: dict[str, Any],
    *,
    optional: dict[str, str | None] | None = None,
    include_model: bool = True,
    include_pods: bool = True,
    extra: tuple[tuple[str, str, str], ...] = (),
) -> str:
    """Compile a complete namespace/workload selector from server-resolved scope."""
    namespace = str(scope.get("namespace", ""))
    model_name = str(scope.get("model_name", ""))
    if not namespace:
        raise ValueError("An authorized namespace is required.")
    matchers = [_matcher("namespace", "=", namespace)]

    if include_model:
        if not model_name:
            raise ValueError("An exact served-model identity is required.")
        matchers.append(_matcher("model_name", "=", model_name))

    # This label is added only when it was observed on the selected series.
    serving_label = scope.get("serving_label")
    serving_value = scope.get("serving_value")
    if serving_label and serving_value:
        if serving_label not in {"llm_isvc_name", "inference_service", "deployment"}:
            raise ValueError("The serving identity label is not in the configured adapter.")
        matchers.append(_matcher(str(serving_label), "=", str(serving_value)))

    if include_pods:
        pods = list(scope.get("pod_names", []))
        pod_pattern = scope.get("pod_regex") or _safe_pod_regex(pods)
        try:
            compiled = re.compile(str(pod_pattern))
        except re.error as exc:
            raise ValueError("The discovered pod selector is invalid.") from exc
        if not str(pod_pattern).startswith("^") or not str(pod_pattern).endswith("$"):
            raise ValueError("Pod selectors must be anchored at both ends.")
        if pods and any(not compiled.fullmatch(pod) for pod in pods):
            raise ValueError("The pod selector includes a pod outside the resolved workload.")
        matchers.append(_matcher("pod", "=~", str(pod_pattern)))

    for key in ("job", "instance", "pod", "engine", "prometheus"):
        value = (optional or {}).get(key)
        if value:
            label = TRUSTED_LABEL_NAMES[key]
            allowed = (
                scope.get("pod_names", []) if key == "pod"
                else scope.get("source_values", []) if key == "prometheus"
                else scope.get(f"{key}_values", [])
            )
            if value not in allowed:
                raise ValueError(f"The selected {key} is outside the resolved workload.")
            # A selected pod is already covered by the verified pod regex.
            if key == "pod":
                matchers.append(_matcher(label, "=", value))
            else:
                matchers.append(_matcher(label, "=", value))

    for label, operator, value in extra:
        matchers.append(_matcher(label, operator, value))
    return f"{metric}{{{', '.join(matchers)}}}"


def _group(scope: str) -> str:
    labels = SERVICE_GROUP if scope == "service" else POD_GROUP
    return ", ".join(labels)


def _range(window: str) -> str:
    if window not in VALID_WINDOWS:
        raise ValueError(f"Unsupported PromQL range: {window}")
    return window


def compile_query(
    spec: MetricSpec,
    scope: dict[str, Any],
    *,
    view_scope: str = "pod",
    window: str = "5m",
    percentile: int | None = None,
    selected_range_seconds: int | None = None,
    summary: bool = False,
    http_handler_regex: str = "",
    http_method_label: str = "method",
    http_handler_label: str = "handler",
    http_status_label: str = "status_code",
    selected_filters: dict[str, str | None] | None = None,
) -> str:
    """Build one instant or range expression for a catalog entry."""
    if view_scope not in {"pod", "service"}:
        raise ValueError("Scope must be 'pod' or 'service'.")
    window = _range(window)
    groups = _group(view_scope)
    prefix = "vllm:"
    labels = dict(TRUSTED_LABEL_NAMES)

    def raw(base: str, *, include_model: bool = True, include_pods: bool = True, extras: tuple[tuple[str, str, str], ...] = ()) -> str:
        return selector(prefix + base, scope, optional=selected_filters, include_model=include_model, include_pods=include_pods, extra=extras)

    if spec.kind == "histogram":
        assert spec.metric
        quantile = percentile if percentile in VALID_PERCENTILES else spec.percentile or 95
        bucket = raw(spec.metric + "_bucket")
        range_text = f"{selected_range_seconds}s" if summary and selected_range_seconds else window
        op = "increase" if summary else "rate"
        return (
            f"histogram_quantile({quantile / 100:.2f}, "
            f"sum by (le, {groups}) ({op}({bucket}[{range_text}])))"
        )

    if spec.kind in {"counter_rate", "counter_increase"}:
        assert spec.metric
        metric = raw(spec.metric, extras=(("finished_reason", "=~", "stop|length"),) if spec.id == 19 else ())
        op = "increase" if spec.kind == "counter_increase" or summary else "rate"
        selected_window = window
        if spec.window != "5m" and not summary:
            selected_window = spec.window
        if summary and selected_range_seconds:
            selected_window = f"{selected_range_seconds}s"
        expression = f"sum by ({groups}) ({op}({metric}[{selected_window}]))"
        if spec.id == 18 and not summary:
            return f"60 * ({expression})"
        return expression

    if spec.kind == "gauge":
        assert spec.metric
        aggregate = "max" if spec.id == 14 else "sum"
        return f"{aggregate} by ({groups}) ({raw(spec.metric)})"

    if spec.kind == "derived":
        if spec.derived == "waiting_running_ratio":
            waiting = f"sum by ({groups}) ({raw('num_requests_waiting')})"
            running = f"sum by ({groups}) ({raw('num_requests_running')})"
            return f"({waiting}) / clamp_min(({running}), 1)"
        if spec.derived == "peak_waiting_upper_bound":
            base = raw("num_requests_waiting")
            return f"sum by ({groups}) (max_over_time({base}[{window}]))"
        if spec.derived == "peak_kv_occupancy":
            base = raw("kv_cache_usage_perc")
            return f"max by ({groups}) (max_over_time({base}[{window}]))"
        if spec.derived == "kv_occupancy_percent":
            return f"100 * max by ({groups}) ({raw('kv_cache_usage_perc')})"
        if spec.derived == "combined_token_rate":
            input_rate = f"sum by ({groups}) (rate({raw('prompt_tokens_total')}[{window}]))"
            output_rate = f"sum by ({groups}) (rate({raw('generation_tokens_total')}[{window}]))"
            return f"({input_rate}) + ({output_rate})"
        if spec.derived == "mean_waiting":
            return f"sum by ({groups}) (avg_over_time({raw('num_requests_waiting')}[5m]))"
        if spec.derived == "prefix_cache_hit_ratio":
            hits = f"sum by ({groups}) (rate({raw('prefix_cache_hits_total')}[{window}]))"
            queries = f"sum by ({groups}) (rate({raw('prefix_cache_queries_total')}[{window}]))"
            return f"({hits}) / ({queries})"
        raise ValueError(f"Unknown derived metric: {spec.derived}")

    if spec.kind == "outcomes":
        assert spec.metric
        outcome_label = labels["finished_reason"]
        matcher = (outcome_label, "=~", "stop|length")
        metric = raw(spec.metric, extras=(matcher,))
        return f"sum by ({groups}, {outcome_label}) (rate({metric}[{window}]))"

    if spec.kind == "http_rate":
        if not http_handler_regex:
            raise QueryUnavailable("HTTP metrics are not enabled: configure the verified inference-handler label selector.")
        if http_status_label not in {"status", "status_code", "code"}:
            raise ValueError("Untrusted HTTP status label name.")
        if http_handler_label not in {"handler", "path", "route"} or http_method_label not in {"method", "http_method"}:
            raise ValueError("Untrusted HTTP label name.")
        try:
            re.compile(http_handler_regex)
        except re.error as exc:
            raise ValueError("The configured HTTP handler regex is invalid.") from exc
        matchers: list[tuple[str, str, str]] = [
            (http_method_label, "=", "POST"),
            (http_handler_label, "=~", http_handler_regex),
        ]
        if spec.id == 27:
            matchers.append((http_status_label, "=~", "5[0-9][0-9]"))
        elif spec.id == 38:
            matchers.append((http_status_label, "=~", "429|503"))
        metric = selector("http_requests_total", scope, include_model=False, include_pods=True, extra=tuple(matchers))
        group_labels = f"namespace, pod, instance, {http_handler_label}, {http_status_label}"
        if spec.id == 37:
            group_labels = "namespace, pod, instance"
        return f"sum by ({group_labels}) (rate({metric}[{window}]))"

    if spec.kind == "kube_gauge":
        assert spec.metric
        deployment = str(scope.get("deployment", ""))
        if not deployment or not scope.get("deployment_resolved", False):
            raise QueryUnavailable("Kubernetes Deployment ownership is unresolved for this historical workload.")
        metric = selector(
            spec.metric,
            {"namespace": scope["namespace"], "deployment": deployment},
            include_model=False,
            include_pods=False,
            extra=(("deployment", "=", deployment),),
        )
        return f"max by (namespace, deployment) ({metric})"

    raise ValueError(f"Unsupported metric kind: {spec.kind}")


def metric_selector_for_capability(spec: MetricSpec, scope: dict[str, Any]) -> str:
    """Return a bounded selector for testing whether the family has series."""
    if spec.kind == "http_rate":
        return selector("http_requests_total", scope, include_model=False, include_pods=False)
    if spec.kind == "kube_gauge":
        deployment = str(scope.get("deployment", ""))
        if not deployment:
            raise QueryUnavailable("Deployment identity is not resolved.")
        return selector(spec.metric or "", {"namespace": scope["namespace"]}, include_model=False, include_pods=False, extra=(("deployment", "=", deployment),))
    metric = spec.metric
    if not metric and spec.sources:
        metric = spec.sources[0]
    if not metric:
        metric = "num_requests_waiting"
    if spec.kind == "histogram":
        metric += "_bucket"
    return selector("vllm:" + metric, scope)
