"""Read-only Kubernetes discovery and SubjectAccessReview client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote
from typing import Any

import httpx

from .identity import Actor
from .settings import settings


class KubernetesClient:
    def _headers(self) -> dict[str, str]:
        try:
            token = Path(settings.kubernetes_token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("The in-cluster service account token is unavailable.") from exc
        if not token:
            raise RuntimeError("The in-cluster service account token is empty.")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _verify(self) -> bool | str:
        ca = Path(settings.kubernetes_ca_file)
        return str(ca) if ca.is_file() else True

    async def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("Kubernetes API paths must be absolute.")
        try:
            async with httpx.AsyncClient(verify=self._verify(), timeout=10, follow_redirects=False) as client:
                response = await client.request(
                    method,
                    settings.kubernetes_url.rstrip("/") + path,
                    headers=self._headers(),
                    json=json_body,
                )
        except httpx.RequestError as exc:
            raise RuntimeError("Kubernetes API is unavailable.") from exc
        if response.status_code in {401, 403}:
            raise RuntimeError("The dashboard service account cannot read the required Kubernetes API.")
        if response.status_code >= 400:
            raise RuntimeError(f"Kubernetes API returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("Kubernetes API returned invalid JSON.") from exc

    async def subject_access(self, actor: Actor, namespace: str, *, verb: str, resource: str) -> bool:
        body = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SubjectAccessReview",
            "spec": {
                "user": actor.user,
                "groups": list(actor.groups),
                "resourceAttributes": {"namespace": namespace, "verb": verb, "resource": resource},
            },
        }
        result = await self._request("POST", "/apis/authorization.k8s.io/v1/subjectaccessreviews", json_body=body)
        status = result.get("status", {})
        if status.get("evaluationError"):
            raise RuntimeError("SubjectAccessReview could not evaluate namespace access.")
        return bool(status.get("allowed"))

    async def _list(self, path: str, *, limit: int = 500) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        continuation = ""
        while True:
            query = f"?limit={limit}"
            if continuation:
                query += "&continue=" + quote(continuation, safe="")
            payload = await self._request("GET", path + query)
            items.extend(payload.get("items", []))
            if len(items) > limit * 4:
                raise RuntimeError("Kubernetes discovery exceeded the configured item bound.")
            continuation = payload.get("metadata", {}).get("continue", "")
            if not continuation:
                return items

    async def workload_metadata(self, namespace: str) -> dict[str, Any]:
        safe_ns = quote(namespace, safe="")
        pods, replicasets, deployments = await asyncio.gather(
            self._list(f"/api/v1/namespaces/{safe_ns}/pods"),
            self._list(f"/apis/apps/v1/namespaces/{safe_ns}/replicasets"),
            self._list(f"/apis/apps/v1/namespaces/{safe_ns}/deployments"),
        )
        rs_to_deployment: dict[str, str] = {}
        for rs in replicasets:
            rs_name = rs.get("metadata", {}).get("name")
            owners = rs.get("metadata", {}).get("ownerReferences", [])
            owner = next((item for item in owners if item.get("kind") == "Deployment"), None)
            if rs_name and owner:
                rs_to_deployment[rs_name] = owner.get("name", "")
        known_deployments = {item.get("metadata", {}).get("name") for item in deployments}
        pod_to_deployment: dict[str, str] = {}
        pod_labels: dict[str, dict[str, str]] = {}
        for pod in pods:
            metadata = pod.get("metadata", {})
            pod_name = metadata.get("name")
            owners = metadata.get("ownerReferences", [])
            rs_owner = next((item for item in owners if item.get("kind") == "ReplicaSet"), None)
            deployment = rs_to_deployment.get(rs_owner.get("name", "")) if rs_owner else None
            if pod_name:
                if deployment in known_deployments:
                    pod_to_deployment[pod_name] = deployment
                pod_labels[pod_name] = metadata.get("labels", {})
        return {
            "pods": pods,
            "deployments": deployments,
            "pod_to_deployment": pod_to_deployment,
            "pod_labels": pod_labels,
        }


kubernetes = KubernetesClient()
