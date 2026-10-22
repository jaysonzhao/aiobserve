"""Typed vLLM metric catalog shared by query, UI, and export code."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MetricSpec:
    id: int
    name: str
    category: str
    kind: str
    family: str
    metric: str | None
    unit: str
    aggregation: str
    window: str
    scope: str
    description: str
    percentile: int | None = None
    sources: tuple[str, ...] = ()
    derived: str | None = None
    capability: str | None = None
    metric_aliases: tuple[str, ...] = ()
    evaluation_window: str | None = None
    evaluation_windows: int | None = None

    def public(self, state: str = "unknown", reason: str | None = None) -> dict[str, Any]:
        result = asdict(self)
        result["sources"] = list(self.sources)
        result["metric_aliases"] = list(self.metric_aliases)
        result["state"] = state
        result["reason"] = reason
        return result


def _m(
    metric_id: int,
    category: str,
    name: str,
    kind: str,
    family: str,
    metric: str | None,
    unit: str,
    aggregation: str,
    window: str,
    scope: str,
    description: str,
    *,
    percentile: int | None = None,
    sources: tuple[str, ...] = (),
    derived: str | None = None,
    capability: str | None = None,
    metric_aliases: tuple[str, ...] = (),
    evaluation_window: str | None = None,
    evaluation_windows: int | None = None,
) -> MetricSpec:
    return MetricSpec(
        metric_id,
        name,
        category,
        kind,
        family,
        metric,
        unit,
        aggregation,
        window,
        scope,
        description,
        percentile,
        sources,
        derived,
        capability,
        metric_aliases,
        evaluation_window,
        evaluation_windows,
    )


METRICS: tuple[MetricSpec, ...] = (
    _m(1, "Queue", "P95 queue duration", "histogram", "vllm:", "request_queue_time_seconds", "seconds", "histogram_quantile", "5m", "pod", "Time from request acceptance until scheduling begins.", percentile=95, evaluation_window="15m", evaluation_windows=3),
    _m(2, "KV", "Preemptions in 15m", "counter_increase", "vllm:", "num_preemptions_total", "events", "sum increase", "15m", "pod", "KV-cache preemptions over the rolling 15-minute window.", metric_aliases=("num_preemptions",), evaluation_window="15m", evaluation_windows=3),
    _m(3, "Latency", "P50 TTFT", "histogram", "vllm:", "time_to_first_token_seconds", "seconds", "histogram_quantile", "5m", "pod", "Median engine time to first generated token.", percentile=50),
    _m(4, "Latency", "P95 TTFT", "histogram", "vllm:", "time_to_first_token_seconds", "seconds", "histogram_quantile", "5m", "pod", "P95 engine time to first generated token.", percentile=95),
    _m(5, "Latency", "P99 TTFT", "histogram", "vllm:", "time_to_first_token_seconds", "seconds", "histogram_quantile", "5m", "pod", "P99 engine time to first generated token.", percentile=99),
    _m(6, "Latency", "P95 prefill duration", "histogram", "vllm:", "request_prefill_time_seconds", "seconds", "histogram_quantile", "5m", "pod", "Request prefill duration.", percentile=95),
    _m(7, "Latency", "P95 request TPOT", "histogram", "vllm:", "request_time_per_output_token_seconds", "seconds/token", "histogram_quantile", "5m", "pod", "Request-level time per output token; kept separate from ITL.", percentile=95),
    _m(8, "Latency", "P95 ITL", "histogram", "vllm:", "inter_token_latency_seconds", "seconds/event", "histogram_quantile", "5m", "pod", "Inter-token latency distribution.", percentile=95),
    _m(9, "Latency", "P95 E2E duration", "histogram", "vllm:", "e2e_request_latency_seconds", "seconds", "histogram_quantile", "5m", "pod", "Engine-side end-to-end request duration, not client or gateway latency.", percentile=95),
    _m(10, "Queue", "Waiting requests", "gauge", "vllm:", "num_requests_waiting", "requests", "sum", "instant", "pod", "Current waiting request gauge."),
    _m(11, "Queue", "Running requests", "gauge", "vllm:", "num_requests_running", "requests", "sum", "instant", "pod", "Current running request gauge."),
    _m(12, "Queue", "Waiting/running", "derived", "vllm:", None, "ratio", "waiting / clamp_min(running, 1)", "instant", "pod", "Waiting-to-running ratio; zero running requests uses a denominator floor of one.", sources=("num_requests_waiting", "num_requests_running"), derived="waiting_running_ratio"),
    _m(13, "Queue", "Peak waiting upper bound", "derived", "vllm:", None, "requests", "sum of per-series max_over_time", "5m", "pod", "Conservative upper bound when engines peak at different times.", sources=("num_requests_waiting",), derived="peak_waiting_upper_bound"),
    _m(14, "KV", "KV occupancy", "gauge", "vllm:", "kv_cache_usage_perc", "fraction (0-1)", "max", "instant", "pod", "vLLM KV-cache occupancy fraction; 0.90 means 90%."),
    _m(15, "KV", "Peak KV occupancy", "derived", "vllm:", None, "fraction (0-1)", "max_over_time, then max", "5m", "pod", "Peak sampled KV-cache occupancy over the rolling window.", sources=("kv_cache_usage_perc",), derived="peak_kv_occupancy"),
    _m(16, "KV", "KV occupancy percent", "derived", "vllm:", None, "percent", "100 * max", "instant", "pod", "Display alias for KV occupancy in percent.", sources=("kv_cache_usage_perc",), derived="kv_occupancy_percent"),
    _m(17, "KV", "Hourly preemptions", "counter_increase", "vllm:", "num_preemptions_total", "events", "sum increase", "1h", "pod", "KV-cache preemptions over the rolling 1-hour window.", metric_aliases=("num_preemptions",)),
    _m(18, "KV", "Preemptions/minute", "counter_rate", "vllm:", "num_preemptions_total", "events/minute", "60 * sum rate", "5m", "pod", "Rate of KV-cache preemptions.", metric_aliases=("num_preemptions",)),
    _m(19, "Traffic", "Normal completions/s", "counter_rate", "vllm:", "request_success_total", "requests/s", "sum rate", "5m", "pod", "Successful requests with finished_reason stop or length only."),
    _m(20, "Traffic", "Input tokens/s", "counter_rate", "vllm:", "prompt_tokens_total", "tokens/s", "sum rate", "5m", "pod", "Prompt tokens processed per second."),
    _m(21, "Traffic", "Output tokens/s", "counter_rate", "vllm:", "generation_tokens_total", "tokens/s", "sum rate", "5m", "pod", "Generated output tokens per second."),
    _m(22, "Traffic", "Combined token accounting/s", "derived", "vllm:", None, "tokens/s", "input rate + output rate", "5m", "pod", "Usage accounting only; input and output tokens are not a uniform compute unit.", sources=("prompt_tokens_total", "generation_tokens_total"), derived="combined_token_rate"),
    _m(23, "Shape", "P95 prompt length", "histogram", "vllm:", "request_prompt_tokens", "tokens/request", "histogram_quantile", "5m", "pod", "Prompt token count per request.", percentile=95),
    _m(24, "Shape", "P95 output length", "histogram", "vllm:", "request_generation_tokens", "tokens/request", "histogram_quantile", "5m", "pod", "Generated token count per request.", percentile=95),
    _m(25, "Shape", "P95 requested output ceiling", "histogram", "vllm:", "request_max_num_generation_tokens", "tokens/request", "histogram_quantile", "5m", "pod", "Requested maximum generation tokens per request.", percentile=95),
    _m(26, "Reliability", "Admission rejections", "counter_increase", "vllm:", "admission_rejections_total", "events", "sum increase", "5m", "pod", "Conditional metric; use only when the runtime exports it.", capability="admission_rejections"),
    _m(27, "Reliability", "HTTP 5xx/s", "http_rate", "http_", "http_requests_total", "requests/s", "sum rate", "5m", "handler/pod", "Conditional POST inference handler 5xx rate.", capability="http_requests"),
    _m(28, "Traffic", "Completion outcomes/s", "outcomes", "vllm:", "request_success_total", "requests/s", "sum rate by finished_reason", "5m", "outcome/pod", "All request outcomes, grouped by finished_reason."),
    _m(29, "Latency", "Service P95 TTFT", "histogram", "vllm:", "time_to_first_token_seconds", "seconds", "histogram_quantile", "5m", "service", "P95 TTFT pooled from histogram buckets across the selected deployment.", percentile=95),
    _m(30, "Latency", "Service P99 E2E", "histogram", "vllm:", "e2e_request_latency_seconds", "seconds", "histogram_quantile", "5m", "service", "P99 E2E pooled from histogram buckets across the selected deployment.", percentile=99),
    _m(31, "Latency", "Service P99 request TPOT", "histogram", "vllm:", "request_time_per_output_token_seconds", "seconds/token", "histogram_quantile", "5m", "service", "P99 request-level TPOT pooled from histogram buckets.", percentile=99),
    _m(32, "Queue", "Five-minute mean waiting", "derived", "vllm:", None, "requests", "sum avg_over_time", "5m", "pod", "Five-minute mean waiting requests by pod/instance.", sources=("num_requests_waiting",), derived="mean_waiting"),
    _m(33, "KV", "Prefix-cache token hit ratio", "derived", "vllm:", None, "fraction", "sum rate hits / sum rate queries", "5m", "pod", "Token hit ratio; unavailable when the query rate is zero.", sources=("prefix_cache_hits_total", "prefix_cache_queries_total"), derived="prefix_cache_hit_ratio"),
    _m(34, "Volume", "Rolling 24h input", "counter_increase", "vllm:", "prompt_tokens_total", "estimated tokens", "sum increase", "24h", "service", "Rolling 24-hour processed prompt tokens; not a calendar-day total."),
    _m(35, "Volume", "Rolling 24h output", "counter_increase", "vllm:", "generation_tokens_total", "estimated tokens", "sum increase", "24h", "service", "Rolling 24-hour generated tokens; not a calendar-day total."),
    _m(36, "Traffic", "Service output tokens/s", "counter_rate", "vllm:", "generation_tokens_total", "tokens/s", "sum rate", "5m", "service", "Generated tokens per second across selected pods."),
    _m(37, "Reliability", "Instrumented HTTP requests/s", "http_rate", "http_", "http_requests_total", "requests/s", "sum rate", "5m", "pod", "Conditional HTTP request rate; requires verified method/handler labels.", capability="http_requests"),
    _m(38, "Reliability", "HTTP 429 or 503/s", "http_rate", "http_", "http_requests_total", "requests/s", "sum rate", "5m", "status/pod", "Conditional 429/503 inference rate with an exact numeric status label.", capability="http_requests"),
    _m(39, "Health", "TTFT observations", "counter_increase", "vllm:", "time_to_first_token_seconds_count", "observations", "sum increase", "5m", "pod", "TTFT histogram observation count for the selected rolling window."),
    _m(40, "Replicas", "Available replicas", "kube_gauge", "kube_", "kube_deployment_status_replicas_available", "replicas", "max by namespace/deployment", "instant", "deployment", "Conditional current available replicas from kube-state-metrics.", capability="kube_deployment_metrics"),
    _m(41, "Replicas", "Desired replicas", "kube_gauge", "kube_", "kube_deployment_spec_replicas", "replicas", "max by namespace/deployment", "instant", "deployment", "Conditional desired replicas from kube-state-metrics.", capability="kube_deployment_metrics"),
)

BY_ID = {metric.id: metric for metric in METRICS}
TOP10_IDS: tuple[int, ...] = (4, 7, 9, 10, 1, 11, 14, 2, 21, 20)
TOP10 = tuple(BY_ID[metric_id] for metric_id in TOP10_IDS)
SUPPORT_IDS: tuple[int, ...] = (19, 23, 24, 28, 32, 34, 35, 39, 40, 41)
DEFAULT_QUERY_IDS: tuple[int, ...] = tuple(dict.fromkeys((*TOP10_IDS, *SUPPORT_IDS)))
HISTOGRAM_COUNT_METRICS: dict[str, str] = {
    "request_queue_time_seconds": "request_queue_time_seconds_count",
    "time_to_first_token_seconds": "time_to_first_token_seconds_count",
    "request_prefill_time_seconds": "request_prefill_time_seconds_count",
    "request_time_per_output_token_seconds": "request_time_per_output_token_seconds_count",
    "inter_token_latency_seconds": "inter_token_latency_seconds_count",
    "e2e_request_latency_seconds": "e2e_request_latency_seconds_count",
    "request_prompt_tokens": "request_prompt_tokens_count",
    "request_generation_tokens": "request_generation_tokens_count",
    "request_max_num_generation_tokens": "request_max_num_generation_tokens_count",
}


def catalog() -> list[dict[str, Any]]:
    return [metric.public() for metric in METRICS]
