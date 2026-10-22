import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import discovery


def test_discovery_succeeds_when_monitoring_and_kubernetes_metadata_succeed(monkeypatch):
    monitoring_client = SimpleNamespace(series=AsyncMock(return_value=[{
        "namespace": "llm", "model_name": "model-a", "llm_isvc_name": "model-a",
        "pod": "model-a-pod", "job": "vllm-engine", "instance": "10.0.0.1:8080",
        "engine": "0", "prometheus": "platform",
    }]))
    kubernetes_client = SimpleNamespace(workload_metadata=AsyncMock(return_value={
        "pods": [], "deployments": [], "pod_to_deployment": {"model-a-pod": "model-a-kserve"},
        "pod_labels": {},
    }))
    monkeypatch.setattr(discovery, "settings", SimpleNamespace(allowed_namespaces=("llm",), max_discovery_series=2000))
    monkeypatch.setattr(discovery, "monitoring", monitoring_client)
    monkeypatch.setattr(discovery, "kubernetes", kubernetes_client)

    result = asyncio.run(discovery.discover_workloads(
        "llm", start=datetime(2026, 10, 22, tzinfo=UTC), end=datetime(2026, 10, 22, 1, tzinfo=UTC),
    ))

    assert result["state"] == "available"
    assert result["models"][0]["deployment"] == "model-a-kserve"
    assert result["models"][0]["deployment_resolved"] is True
    assert result["warnings"] == []
