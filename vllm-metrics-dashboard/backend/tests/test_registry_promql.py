from datetime import UTC, datetime

import pytest

from app.promql import compile_query, selector
from app.registry import BY_ID, METRICS, TOP10_IDS


def sample_scope():
    return {
        "namespace": "llm",
        "model_name": "gpt-oss-20b",
        "deployment": "gpt-oss-20b-kserve",
        "deployment_resolved": True,
        "serving_label": "llm_isvc_name",
        "serving_value": "gpt-oss-20b",
        "pod_names": ["gpt-oss-20b-kserve-7fbbd9c9f8-abcde"],
        "job_values": ["llm/kserve-llm-isvc-vllm-engine-default"],
        "instance_values": ["10.0.0.1:8080"],
        "engine_values": ["0"],
    }


def test_registry_contains_all_41_entries_and_top_ten_order():
    assert len(METRICS) == 41
    assert TOP10_IDS == (4, 7, 9, 10, 1, 11, 14, 2, 21, 20)
    assert all(metric_id in BY_ID for metric_id in TOP10_IDS)


def test_histogram_aggregates_buckets_before_quantile_and_uses_raw_family():
    query = compile_query(BY_ID[4], sample_scope(), window="5m", percentile=95)
    assert query.startswith("histogram_quantile(0.95, sum by (le, namespace, model_name, pod, instance)")
    assert "rate(vllm:time_to_first_token_seconds_bucket{" in query
    assert "vllm_" not in query and "kserve_vllm:" not in query


def test_service_histogram_pools_buckets_and_can_query_full_selected_range():
    query = compile_query(
        BY_ID[29], sample_scope(), view_scope="service", selected_range_seconds=3600,
        summary=True, percentile=95,
    )
    assert "sum by (le, namespace, model_name)" in query
    assert "increase(vllm:time_to_first_token_seconds_bucket" in query
    assert "[3600s]" in query


def test_counter_uses_rate_before_aggregation_and_scoped_job():
    query = compile_query(BY_ID[21], sample_scope(), selected_filters={"job": "llm/kserve-llm-isvc-vllm-engine-default"})
    assert "sum by (namespace, model_name, pod, instance) (rate(vllm:generation_tokens_total{" in query
    assert 'job="llm/kserve-llm-isvc-vllm-engine-default"' in query


def test_promql_literal_escapes_quotes_and_selector_rejects_untrusted_label():
    scope = sample_scope()
    scope["model_name"] = 'bad"},up{job="x'
    query = selector("vllm:num_requests_waiting", scope)
    assert 'model_name="bad\\\"},up{job=\\\"x"' in query
    with pytest.raises(ValueError, match="untrusted label"):
        selector("vllm:num_requests_waiting", sample_scope(), extra=(("__name__", "=", "up"),))


def test_optional_http_metric_fails_closed_without_handler_mapping():
    with pytest.raises(ValueError, match="configure"):
        compile_query(BY_ID[27], sample_scope())
